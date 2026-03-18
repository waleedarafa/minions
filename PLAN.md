# Minions: Open-Source Implementation of Stripe's One-Shot End-to-End Coding Agents

## Context

Stripe built "Minions" — unattended AI coding agents that autonomously receive tasks, implement code changes, run tests, and deliver finished pull requests for human review. They ship 1,300+ merged PRs/week with zero human-written code. This plan defines the requirements and technical specs to build an open-source implementation of the same framework.

---

## 1. High-Level Architecture

```
User (Slack/CLI/Web/API)
        │
        ▼
   Task Router ──► Devbox Provisioner ──► Isolated Sandbox
        │                                       │
        ▼                                       ▼
  Blueprint Engine ◄──────────────────► Agent Runtime (LLM)
        │                                       │
        ├─► Deterministic Nodes                 ├─► MCP Tool Server
        │    (lint, git, push, PR)              │    (docs, search, tickets)
        │                                       │
        └─► Agentic Nodes                      └─► Rule File Loader
             (implement, fix CI)                     (scoped context)
        │
        ▼
   Feedback Loop (Lint → CI → Autofix → Retry → Human)
        │
        ▼
   PR Creation & Human Review
```

---

## 2. Core Components & Requirements

### 2.1 Sandbox / Devbox System

**Purpose:** Isolated, reproducible execution environments for each agent run, with a path from pooled containers to true hot devboxes.

| Requirement | Detail |
|---|---|
| Isolation | Each agent runs in its own container/VM with no production access, no internet egress |
| Fast provisioning | Target <10s readiness for production devboxes via pre-warmed pool of environments |
| Pre-loaded state | Repository cloned, dependencies installed, caches warmed, services running |
| Parallelism | Multiple agents run concurrently on independent tasks |
| Clean state | Each run starts from a pristine, predictable baseline |
| Teardown | Auto-cleanup after run completes or times out |
| Human/agent parity | Agents should run on the same prepared development substrate humans use whenever feasible |

**Implementation approach:**
- Use Docker containers as the initial sandbox implementation and compatibility layer
- Add a higher-level devbox abstraction for production unattended runs
- Maintain a pool of pre-built images/devboxes with repo + deps baked in
- Support configurable base images per project
- Optional: support Firecracker microVMs for stronger isolation
- Mount repo as a volume or clone fresh per run
- Do not rely on runtime package installation for core verification tools; the environment must be pre-baked and reproducible
- Default production posture: no arbitrary internet egress, no fallback to unrestricted host execution for unattended runs
- Local-development fallback may exist, but it must be an explicit non-production mode with degraded guarantees
- Production devboxes should be "hot and ready": checked out to a recent default branch, with build/lint/typecheck caches already warm and any always-on background services already started
- Multiple logically separate runs for one user should map to separate working environments rather than sharing mutable state

**Key files to create:**
- `src/sandbox/manager.py` — Pool manager, provisioning, teardown
- `src/sandbox/dockerfile.template` — Base sandbox image
- `src/sandbox/config.py` — Sandbox configuration (resources, timeouts, networking)
- `src/devbox/manager.py` — Higher-level devbox pool, warm-up, and allocation
- `src/devbox/provisioner.py` — Repo checkout, cache warm-up, and service bootstrap

### 2.2 Blueprint Engine (Orchestration)

**Purpose:** Hybrid state machine that interleaves deterministic code nodes with agentic (LLM-driven) subtasks.

**Core concept:** A blueprint is a DAG/state machine with two node types:
1. **Deterministic nodes** — Hardcoded operations (lint, git push, PR creation) that always execute the same way. Guaranteed completion, no LLM tokens wasted.
2. **Agentic nodes** — LLM-driven steps (implement feature, fix CI failures) that allow creative problem-solving.

**Blueprint definition (YAML):**
```yaml
name: default_coding_task
max_ci_rounds: 2
steps:
  - name: hydrate_context
    type: deterministic
    action: load_rules_and_tools

  - name: gather_context
    type: agentic
    prompt: "Analyze the task and gather relevant context using available tools"
    tools: [search, read_file, documentation]

  - name: implement
    type: agentic
    prompt: "Implement the requested changes"
    tools: [edit_file, create_file, delete_file, search, read_file]

  - name: local_lint
    type: deterministic
    action: run_linters
    timeout: 60s

  - name: push_and_ci
    type: deterministic
    action: git_push_and_trigger_ci

  - name: handle_ci_failures
    type: agentic
    prompt: "Analyze CI failures and fix them"
    tools: [edit_file, read_file, search, ci_logs]
    max_iterations: 1
    condition: ci_failed

  - name: apply_autofixes
    type: deterministic
    action: apply_autofixes
    condition: autofixes_available

  - name: second_ci_round
    type: deterministic
    action: git_push_and_trigger_ci
    condition: changes_after_fix

  - name: create_pr
    type: deterministic
    action: create_pull_request
    template: default_pr_template
```

**Requirements:**
- Blueprint registry — load/validate YAML blueprint definitions
- State machine executor — traverse nodes, manage state transitions
- Conditional execution — nodes can have conditions (e.g., `ci_failed`)
- Custom blueprints — teams can author task-specific blueprints (e.g., migrations)
- Hard limits — max CI rounds (default: 2), max tokens, max wall-clock time
- Partial success handling — even failed runs produce usable output
- Deterministic context hydration stage before the main agent loop (resolve links, fetch tickets/docs, pre-load likely context)
- Deterministic verification stages must actually wire to CI polling, autofix application, and second-round retry behavior
- Blueprint names used by CLI/API defaults must exactly match registered blueprint names
- Task-level overrides (branch base, extra tools, rules overrides, iteration budgets, priority) must be propagated into execution context
- No human-supervision assumptions inside the loop — unattended runs should not depend on confirmation prompts or interactive steering

**Key files to create:**
- `src/blueprint/engine.py` — State machine executor
- `src/blueprint/schema.py` — Blueprint YAML schema & validation
- `src/blueprint/nodes/deterministic.py` — Built-in deterministic actions
- `src/blueprint/nodes/agentic.py` — LLM-driven node executor
- `blueprints/default.yaml` — Default coding task blueprint
- `blueprints/migration.yaml` — Example migration blueprint

### 2.3 Agent Runtime (LLM Integration)

**Purpose:** Execute agentic nodes by driving an LLM with tools, context, and task instructions.

**Requirements:**
| Requirement | Detail |
|---|---|
| Multi-model support | Claude (primary), GPT-4, open-source models via OpenAI-compatible API |
| Tool use | Function calling / tool use for all code operations |
| Streaming | Stream agent output for real-time monitoring |
| Token budgets | Per-node and per-run token limits |
| Conversation management | Maintain context within a blueprint run, reset between nodes if needed |
| Retry logic | Retry on transient API errors with exponential backoff |
| Unattended operation | No human-in-the-loop; agent must self-recover or fail gracefully |

**Key files to create:**
- `src/agent/runtime.py` — Core agent loop (tool use cycle)
- `src/agent/llm_client.py` — Multi-provider LLM client (Claude, OpenAI, etc.)
- `src/agent/tools.py` — Tool registry and execution
- `src/agent/context.py` — Context window management

### 2.4 Tool System (MCP-Compatible)

**Purpose:** Provide agents with curated tools for code operations, context gathering, and external integrations.

**Design principle:** "Careful subset selection outperforms tool abundance." Agents get curated tool subsets per task, not everything.

**Built-in tool categories:**

| Category | Tools |
|---|---|
| File operations | `read_file`, `edit_file`, `create_file`, `delete_file`, `list_files` |
| Search | `grep_search`, `glob_search`, `semantic_search` |
| Git | `git_diff`, `git_log`, `git_blame`, `git_status`, `git_branch` |
| Execution | `run_command`, `run_tests`, `run_linter` |
| CI/CD | `get_ci_status`, `get_ci_logs`, `get_test_results` |
| Documentation | `search_docs`, `read_doc` |
| External | MCP-compatible tool server integration |

**MCP Integration:**
- Connect to external MCP servers for additional tools
- Support Stripe's "Toolshed" pattern — a central MCP server hosting many tools
- Tool filtering — configure which tools are available per blueprint/step
- Security controls — prevent destructive actions in sandbox
- Context hydration — deterministically invoke relevant MCP tools for links/tickets/docs before agentic steps begin
- Task-level tool curation — merge blueprint defaults with request-specific allowlists rather than exposing the full registry
- Shared human/agent context sources — prefer the same docs, ticketing, search, and code intelligence systems human engineers already use
- Centralized capability layer — adding a tool to the shared MCP/tool platform should make it broadly discoverable, while each run still receives only a curated subset
- Prefer grouped or themed tool bundles for per-user/task expansion, rather than ad hoc unrestricted tool exposure

**Key files to create:**
- `src/tools/registry.py` — Tool registration, discovery, filtering
- `src/tools/builtin/` — Built-in tool implementations
- `src/tools/mcp_client.py` — MCP protocol client for external tool servers
- `src/tools/security.py` — Tool permission/sandboxing layer

### 2.5 Rule File System (Scoped Context)

**Purpose:** Provide agents with relevant coding guidelines and context, scoped by directory/file pattern.

**Design principles:**
- Global rules applied "very judiciously" to preserve context window
- Rules scoped to specific subdirectories and file patterns
- Agents automatically pick up only contextually relevant rules
- Compatible with Cursor `.cursorrules` and Claude Code `CLAUDE.md` formats
- The live execution path must resolve scoped rules for the files or directories under consideration, not concatenate every discovered rule globally
- Request-level `rules_override` should layer on top of scoped rules without bypassing them

**Rule file format:**
```yaml
# .minion/rules.yaml
scope: "src/payments/**"
rules:
  - "Always use Sorbet type annotations"
  - "Payment amounts must use BigDecimal, never float"
  - "All payment mutations require idempotency keys"
```

**Also supports:**
- `.cursorrules` files (Cursor format)
- `CLAUDE.md` files (Claude Code format)
- Hierarchical resolution — deeper directory rules override/extend parent rules

**Key files to create:**
- `src/rules/loader.py` — Rule file discovery, parsing, scoping
- `src/rules/resolver.py` — Context-aware rule resolution based on active files

### 2.6 Feedback Loop System

**Purpose:** Multi-layer validation pipeline that catches errors early and provides structured feedback to the agent.

**Layers (executed in order):**

```
1. Pre-push Local Lint (~5 seconds)
   ├─ Heuristic lint selection (only relevant linters)
   ├─ Cached results for speed
   └─ Fix or report to agent

2. CI Round 1
   ├─ Push branch, trigger selective test execution
   ├─ Apply autofixes for known failure patterns
   └─ Return unfixed failures to agent for retry

3. Agent Fix Attempt (Agentic Node)
   ├─ Agent analyzes failures and attempts fixes
   └─ One attempt only

4. CI Round 2 (Final)
   ├─ Push fixes, re-run failed tests
   └─ Unresolved failures → human review

5. Hard Stop
   └─ Max 2 CI rounds enforced; no infinite loops
```

**Requirements:**
- Lint daemon with heuristic rule selection
- CI integration (GitHub Actions, CircleCI, Jenkins, etc.)
- Autofix registry — map known failure patterns to automatic fixes
- Test result parser — extract actionable failure info from CI logs
- Bounded iteration — hard limit on retry rounds (configurable, default: 2)
- "Diminishing returns" principle — LLMs degrade on repeated retries of same problem
- Shift-left enforcement — run the fastest relevant local checks before any remote CI push
- Selective CI feedback — fetch the specific failed jobs/logs/tests rather than dumping raw CI output blindly
- Autofix wiring — known autofixes must be part of the default feedback path, not a disconnected helper library
- At most one agentic fix attempt after CI Round 1, followed by one final CI round and then hard stop

**Key files to create:**
- `src/feedback/lint.py` — Local lint runner with heuristic selection
- `src/feedback/ci.py` — CI integration (trigger, poll, parse results)
- `src/feedback/autofix.py` — Autofix pattern registry and application
- `src/feedback/parser.py` — Test failure log parser

### 2.7 Task Ingestion & Interfaces

**Purpose:** Accept tasks from multiple entry points.

**Supported interfaces:**

| Interface | Detail |
|---|---|
| CLI | `minion run "Fix the flaky test in payments/test_charge.py"` |
| REST API | POST `/api/tasks` with task description + metadata |
| Slack Bot | React to messages/commands in configured channels |
| Web UI | Simple dashboard for submitting tasks and monitoring runs |
| Webhooks | Accept automated triggers (e.g., CI detects flaky test → auto-create task) |
| Embedded buttons | API for embedding "Fix with Minion" buttons in internal tools |

**Interface requirements:**
- All entry points must converge on the same execution engine; Slack/API/webhooks/CLI must not have separate "stub" or degraded orchestration paths
- Slack and webhook triggers should preserve thread context, linked references, base branch, and origin metadata where available
- API-created tasks must be durable and resumable; in-memory-only task state is acceptable only for local development
- The Web UI should support both submission and observability of the exact same underlying runs

**Task schema:**
```json
{
  "id": "uuid",
  "description": "Fix the flaky test in payments/test_charge.py",
  "repo": "org/repo",
  "branch_base": "main",
  "context_links": ["https://ticket.internal/123"],
  "blueprint": "default",
  "tools": ["search", "read_file", "edit_file"],
  "rules_override": [],
  "priority": "normal",
  "created_by": "user@company.com",
  "created_at": "2026-03-18T00:00:00Z",
  "status": "pending"
}
```

**Execution requirements for task metadata:**
- `branch_base` must control branch creation, push targets, and PR base branch
- `context_links` must feed deterministic context hydration before agentic nodes begin
- `tools` must constrain or augment the tool subset available to the run
- `rules_override` must be merged into the scoped rule set for the run
- `priority` should influence queueing/scheduling, not just be stored
- `created_by` and entry-point metadata should be preserved through logs, PR output, and monitoring

**Key files to create:**
- `src/api/server.py` — FastAPI REST server
- `src/api/models.py` — Task & run data models
- `src/api/routes.py` — API endpoints
- `src/cli/main.py` — CLI entry point (Click/Typer)
- `src/integrations/slack.py` — Slack bot integration
- `src/integrations/webhooks.py` — Webhook receiver

### 2.8 PR Creation & Output

**Purpose:** Produce clean, review-ready pull requests.

**Requirements:**
- Create branch with descriptive name
- Follow configurable PR templates
- Include task context, what was changed, and why
- Link back to originating task/ticket
- Add appropriate labels and reviewers
- Support GitHub, GitLab, Bitbucket APIs
- Use the task's requested base branch, not a hardcoded default
- Produce useful partial-success PRs when verification is incomplete, clearly marking unresolved issues
- Reuse the same PR templating and git helper layer across CLI/API/Slack-triggered runs

**Key files to create:**
- `src/output/pr.py` — PR creation logic
- `src/output/templates/` — PR template files
- `src/output/git.py` — Git operations (branch, commit, push)

### 2.9 Monitoring & Observability

**Purpose:** Track agent runs, costs, success rates, and enable debugging.

**Requirements:**
- Run logging — full transcript of agent actions, tool calls, LLM responses
- Metrics — success rate, tokens used, wall-clock time, CI rounds needed
- Cost tracking — per-run and aggregate LLM API costs
- Real-time streaming — live output during agent execution
- Run history — searchable archive of past runs with outcomes
- Alerting — notify on failures, budget exceeded, etc.

**Key files to create:**
- `src/monitoring/logger.py` — Structured run logging
- `src/monitoring/metrics.py` — Metrics collection and reporting
- `src/monitoring/dashboard.py` — Simple web dashboard

---

## 3. Project Structure

```
minions/
├── blueprints/                  # Blueprint YAML definitions
│   ├── default.yaml
│   ├── migration.yaml
│   └── flaky_test_fix.yaml
├── src/
│   ├── __init__.py
│   ├── sandbox/
│   │   ├── __init__.py
│   │   ├── manager.py           # Sandbox pool & lifecycle
│   │   ├── config.py            # Sandbox configuration
│   │   └── docker.py            # Docker-based sandbox implementation
│   ├── blueprint/
│   │   ├── __init__.py
│   │   ├── engine.py            # State machine executor
│   │   ├── schema.py            # Blueprint validation
│   │   └── nodes/
│   │       ├── deterministic.py # Built-in deterministic actions
│   │       └── agentic.py       # LLM-driven node executor
│   ├── agent/
│   │   ├── __init__.py
│   │   ├── runtime.py           # Core agent loop
│   │   ├── llm_client.py        # Multi-provider LLM client
│   │   ├── tools.py             # Tool registry & execution
│   │   └── context.py           # Context window management
│   ├── tools/
│   │   ├── __init__.py
│   │   ├── registry.py          # Tool discovery & filtering
│   │   ├── mcp_client.py        # MCP protocol client
│   │   ├── security.py          # Permission layer
│   │   └── builtin/
│   │       ├── file_ops.py
│   │       ├── search.py
│   │       ├── git.py
│   │       ├── execution.py
│   │       └── ci.py
│   ├── rules/
│   │   ├── __init__.py
│   │   ├── loader.py            # Rule file discovery & parsing
│   │   └── resolver.py          # Scoped rule resolution
│   ├── feedback/
│   │   ├── __init__.py
│   │   ├── lint.py              # Local lint runner
│   │   ├── ci.py                # CI integration
│   │   ├── autofix.py           # Autofix registry
│   │   └── parser.py            # Test failure parser
│   ├── api/
│   │   ├── __init__.py
│   │   ├── server.py            # FastAPI app
│   │   ├── models.py            # Data models
│   │   └── routes.py            # API endpoints
│   ├── cli/
│   │   ├── __init__.py
│   │   └── main.py              # CLI entry point
│   ├── integrations/
│   │   ├── __init__.py
│   │   ├── slack.py             # Slack bot
│   │   ├── github.py            # GitHub API client
│   │   └── webhooks.py          # Webhook receiver
│   ├── output/
│   │   ├── __init__.py
│   │   ├── pr.py                # PR creation
│   │   ├── git.py               # Git operations
│   │   └── templates/
│   │       └── default.md       # Default PR template
│   └── monitoring/
│       ├── __init__.py
│       ├── logger.py            # Run logging
│       ├── metrics.py           # Metrics collection
│       └── dashboard.py         # Web dashboard
├── tests/
│   ├── test_blueprint_engine.py
│   ├── test_agent_runtime.py
│   ├── test_sandbox.py
│   ├── test_tools.py
│   ├── test_rules.py
│   ├── test_feedback.py
│   └── test_api.py
├── pyproject.toml
├── Dockerfile                   # Sandbox base image
├── docker-compose.yaml          # Local dev setup
└── README.md
```

---

## 4. Tech Stack

| Component | Technology |
|---|---|
| Language | Python 3.12+ |
| LLM Client | Anthropic SDK (Claude), OpenAI SDK, LiteLLM for multi-provider |
| API Framework | FastAPI + Uvicorn |
| CLI | Typer |
| Sandbox | Docker SDK for Python / docker-compose |
| Task Queue | Redis + Celery (or arq for lightweight) |
| Database | SQLite (dev) / PostgreSQL (prod) for run history |
| Git | GitPython + subprocess for git operations |
| CI Integration | GitHub Actions API (primary), extensible for others |
| MCP Client | Official MCP Python SDK |
| Config | Pydantic Settings |
| Testing | pytest |
| Logging | structlog |

---

## 5. Key Design Principles (from Stripe)

1. **Environment First** — Solid dev infrastructure, test coverage, and feedback loops matter more than model selection.
2. **Determinism Where It Counts** — Hardcode critical paths (lint, git, PR creation) to prevent agent drift.
3. **Context Discipline** — Scoped rules and curated tool subsets outperform overwhelming agents with everything.
4. **Bounded Iteration** — Hard limits on retries (max 2 CI rounds). LLMs show diminishing returns on repeated attempts.
5. **Partial Success is Valuable** — Even failed runs produce usable starting points for engineers.
6. **Shift Feedback Left** — Catch errors locally before burning CI time and LLM tokens.
7. **One Engine, Many Entry Points** — Every interface should invoke the same real execution path, not a simplified stub.
8. **Pre-Hydrate Context** — Resolve linked docs, tickets, CI failures, and likely context deterministically before the agent starts exploring.
9. **Reuse Human Tooling** — Agents should rely on the same environments, rules, verification tools, and context systems that human engineers use.

---

## 6. Implementation Phases

### Phase 1: Core Engine (MVP)
- Agent runtime with Claude API integration
- Blueprint engine with deterministic + agentic nodes
- Built-in file/search/git tools
- CLI interface (`minion run "task description"`)
- Local execution (no sandbox isolation yet)
- Basic run logging
- Blueprint naming and default selection contract made consistent across CLI/API/docs

### Phase 2: Sandbox & Feedback
- Docker-based sandbox provisioning
- Local lint integration
- CI integration (GitHub Actions)
- Autofix system
- Bounded retry logic (max 2 CI rounds)
- Deterministic CI polling + log ingestion + autofix application wired into default blueprint
- Production-grade sandbox mode with no arbitrary egress and no silent fallback to host execution

### Phase 3: Context & Tools
- Rule file system (scoped rules, .cursorrules, CLAUDE.md compatibility)
- MCP client for external tool servers
- Tool filtering per blueprint/step
- PR creation with templates
- Deterministic pre-hydration from `context_links`, tickets, docs, and CI references
- Task-level tool/rule overrides propagated through the execution context

### Phase 4: Interfaces & Integrations
- REST API server
- Slack bot integration
- Web dashboard
- Webhook receivers
- GitHub/GitLab PR integration
- All interfaces routed through the same durable task runner and blueprint engine
- Entry-point context preservation (thread context, links, base branch, creator metadata)

### Phase 5: Scale & Polish
- Sandbox pool with pre-warming
- Metrics & cost tracking
- Multi-model support
- Custom blueprint authoring
- Run history & search

---

## 7. Verification Plan

| Test | How |
|---|---|
| Unit tests | `pytest tests/` — test each component in isolation |
| Blueprint execution | Run default blueprint against a sample repo with known issues |
| Sandbox isolation | Verify container cannot access host network/filesystem |
| Lint feedback | Introduce lint errors, verify agent receives and fixes them |
| CI integration | Push to test repo, verify CI triggers and results are parsed |
| Bounded retry | Introduce unfixable test, verify agent stops after 2 CI rounds |
| PR output | Verify PR is created with correct template, labels, branch name |
| End-to-end | Submit task via CLI → agent runs → PR created → tests pass |
| MCP tools | Connect to mock MCP server, verify tool discovery and execution |
| Rule scoping | Place rules in subdirectories, verify only relevant rules are loaded |
| Entry-point parity | Submit the same task via CLI/API/Slack/webhook and verify all use the same execution path and produce equivalent runs |
| Context hydration | Provide links/ticket references and verify deterministic hydration injects the fetched context before agentic steps |
| Task metadata propagation | Verify `branch_base`, `context_links`, `tools`, `rules_override`, and `priority` all affect execution as specified |

---

## 8. Prioritized Implementation Backlog

This backlog is ordered by dependency and impact. Earlier items unblock later ones and should generally be completed first.

### P0. Fix Core Contract Mismatches

**Why first:** The documented and default execution path must be internally consistent before broader framework work can be trusted.

**Deliverables:**
- Make blueprint names consistent across YAML, CLI defaults, API defaults, webhook defaults, and README examples
- Ensure the default blueprint resolves successfully from every entry point
- Remove or clearly mark any placeholder/stub execution paths that masquerade as real runs

**Exit criteria:**
- `minion run "<task>"` uses the intended default blueprint without extra flags
- API task creation with default settings targets the same blueprint contract
- Documentation examples match the actual registered blueprint names

### P1. Unify Task Execution Across Entry Points

**Why now:** This is the main architectural gap. Slack/API/webhooks/CLI must all invoke the same real engine.

**Deliverables:**
- Introduce a shared task runner/service that owns sandbox setup, blueprint loading, runtime creation, execution, logging, and teardown
- Refactor CLI to call the shared task runner instead of building the stack inline
- Refactor API background tasks, Slack integration, and webhooks to call the same shared task runner
- Replace simulated API task progression with real blueprint execution

**Primary files:**
- `src/cli/main.py`
- `src/api/routes.py`
- `src/integrations/slack.py`
- `src/integrations/webhooks.py`
- new shared runtime/orchestration module (for example `src/task_runner.py` or `src/orchestration/service.py`)

**Exit criteria:**
- The same task submitted by CLI or API produces the same underlying execution behavior
- Slack and webhook triggers no longer depend on stubbed API-only logic
- Logs, task status, and results are emitted by one shared code path

### P2. Propagate Task Metadata End-to-End

**Why now:** Context, branch behavior, and tool/rule selection depend on this metadata being honored.

**Deliverables:**
- Thread `branch_base`, `context_links`, `tools`, `rules_override`, `priority`, and `created_by` through task creation, execution context, and output
- Use `branch_base` for branching, push, and PR base branch selection
- Use `priority` in queueing/scheduling decisions
- Preserve origin metadata for logs, monitoring, and PR output

**Primary files:**
- `src/api/models.py`
- task runner/orchestration layer
- `src/blueprint/nodes/deterministic.py`
- `src/output/pr.py`
- scheduling/task queue components

**Exit criteria:**
- Every field in `TaskCreate` has a defined runtime effect or is removed from the contract
- PR creation honors `branch_base`
- Monitoring/logging can attribute runs to source and creator

### P3. Add Deterministic Context Hydration

**Why now:** Stripe’s Part 2 guidance strongly emphasizes front-loading context before the agent loop.

**Deliverables:**
- Add a deterministic hydration stage before agentic nodes begin
- Resolve `context_links` into concrete context using MCP tools and built-in integrations
- Support common hydration sources: tickets, docs, CI failure links, repository links
- Store hydrated context in execution state so later nodes can consume it without rediscovery

**Primary files:**
- `src/tools/mcp_client.py`
- `src/tools/registry.py`
- new hydration module (for example `src/context/hydration.py`)
- `src/blueprint/nodes/deterministic.py`
- `blueprints/default.yaml`

**Exit criteria:**
- A task with links/tickets deterministically fetches them before the first agentic node
- Agentic nodes receive hydrated context without needing to rediscover obvious references
- Hydration failures degrade gracefully and are recorded

### P4. Wire Scoped Rules Into the Live Execution Path

**Why now:** This is one of the highest-leverage reliability improvements for large codebases.

**Deliverables:**
- Replace global rule concatenation in the execution path with `RuleResolver`
- Resolve rules based on files/directories being considered during planning and implementation
- Merge `rules_override` on top of scoped rules
- Preserve support for `.minion/rules.yaml`, `.cursorrules`, and `CLAUDE.md`

**Primary files:**
- `src/rules/loader.py`
- `src/rules/resolver.py`
- `src/blueprint/nodes/agentic.py`
- task runner/orchestration layer

**Exit criteria:**
- Agentic nodes only receive relevant scoped rules by default
- Global rule injection is minimal and intentional
- Request-level overrides are visible in prompts without replacing scoped rules

### P5. Implement the Full Feedback Loop

**Why now:** This is the heart of the “one-shot” quality model and unlocks reliable unattended operation.

**Deliverables:**
- Make local verification use heuristic lint/test selection where appropriate
- Add deterministic push + CI polling stages to the default blueprint
- Feed CI failures into the autofix registry before invoking the agent
- Allow one agentic fix attempt after CI Round 1
- Run one final CI round, then stop with partial success if still failing
- Parse CI output into structured failures for agent consumption

**Primary files:**
- `src/feedback/lint.py`
- `src/feedback/ci.py`
- `src/feedback/autofix.py`
- `src/feedback/parser.py`
- `src/blueprint/nodes/deterministic.py`
- `blueprints/default.yaml`

**Exit criteria:**
- Default blueprint enforces the intended local-check -> CI -> autofix -> agent-fix -> final-CI flow
- Max CI rounds is enforced in the real path, not just declared in schema
- Known autofixes are part of the standard run loop

### P6. Harden the Sandbox / Devbox Story

**Why now:** Once the real loop exists, unattended safety and reproducibility become the next bottleneck.

**Deliverables:**
P6a Sandbox hardening:
- Pre-bake verification tools and common dependencies into sandbox images
- Disable arbitrary internet egress in production sandbox mode
- Distinguish explicit development fallback mode from unattended production mode
- Expand sandbox pooling/pre-warming so runs start from predictable prepared environments
- Define resource/time/network policies centrally
P6b Hot devbox parity:
- Introduce a first-class devbox abstraction above raw containers
- Maintain a warm pool of ready-to-assign environments with recent default-branch checkouts
- Pre-warm repo state, build/lint/typecheck caches, and long-lived background services needed for local verification
- Support multiple concurrent isolated environments per user/task without shared mutable state

**Primary files:**
- `src/sandbox/manager.py`
- `src/sandbox/runner.py`
- `src/sandbox/docker.py`
- `src/sandbox/config.py`
- `src/devbox/manager.py`
- `src/devbox/provisioner.py`
- `Dockerfile`
- `docker-compose.yaml`

**Exit criteria:**
P6a:
- Production-mode runs do not depend on runtime package installation
- Host fallback is opt-in and clearly labeled as degraded
- Sandbox startup is reproducible and policy-driven
P6b:
- Production unattended runs can start from a genuinely pre-warmed environment rather than only a cold container
- Environment readiness covers repo checkout, caches, and background services, not just image availability
- The same environment model can plausibly serve both human and unattended agent development flows

### P7. Strengthen Tool Selection and Security

**Why now:** Once context and execution are real, tool governance becomes necessary to keep agents reliable and safe.

**Deliverables:**
- Merge blueprint tool lists with task-level tool allowlists/overrides
- Prevent accidental exposure of all MCP tools to all runs
- Enforce security policy checks before command/file tools execute
- Improve command execution safety and async correctness in execution tools

**Primary files:**
- `src/tools/registry.py`
- `src/tools/security.py`
- `src/tools/builtin/execution.py`
- `src/agent/tools.py`

**Exit criteria:**
- Tool availability is explicitly derivable from blueprint + task metadata
- Security checks are enforced in the live execution path
- Command execution is reliable in async contexts

### P8. Improve PR Creation and Human Handoff

**Why now:** At this point the run loop can produce meaningful outputs; PR polish and review ergonomics matter.

**Deliverables:**
- Route deterministic PR creation through shared templating/output helpers
- Include task origin, hydrated context, change summary, and unresolved issues in PR bodies
- Support labels/reviewers and partial-success PR language
- Link PRs back to source tasks/tickets

**Primary files:**
- `src/output/pr.py`
- `src/output/templates/default.md`
- `src/blueprint/nodes/deterministic.py`
- `src/integrations/github.py`

**Exit criteria:**
- PRs are review-ready and consistent across entry points
- Partial-success runs still produce useful, clearly labeled PRs when appropriate

### P9. Add Durability, Observability, and Scale Features

**Why last:** These matter for productionization, but they should sit on top of a correct execution model.

**Deliverables:**
- Persist tasks, run state, logs, and results beyond process memory
- Add run history, search, and dashboards
- Track tokens, durations, CI rounds, success rate, and cost
- Add queueing/backpressure and multi-run scheduling based on priority

**Primary files:**
- `src/api/routes.py`
- `src/monitoring/logger.py`
- `src/monitoring/metrics.py`
- `src/monitoring/dashboard.py`
- queue/database integration modules

**Exit criteria:**
- Runs survive process restarts
- Historical runs and outcomes are queryable
- Operators can observe cost, reliability, and bottlenecks

### Suggested Milestones

1. **Milestone A: Real Single-Path Execution**
   Complete P0-P2.

2. **Milestone B: Context-Aware Reliable Runs**
   Complete P3-P5.

3. **Milestone C: Safe Unattended Operation**
   Complete P6-P7.

4. **Milestone D: Reviewable Outputs**
   Complete P8.

5. **Milestone E: Durable, Scalable Operations**
   Complete P9.

---

## 9. Near-Term Execution Checklist (P0-P2)

This section translates the first three backlog priorities into concrete implementation tasks. The intent is to make the next coding steps unambiguous and testable.

### P0 Checklist: Fix Core Contract Mismatches

#### Task P0.1: Normalize blueprint naming

**Goal:** Make the default blueprint contract consistent across code, docs, and task creation surfaces.

**Implementation tasks:**
- Choose the canonical default blueprint name:
  - either rename `blueprints/default.yaml` from `default_coding_task` to `default`
  - or update every caller/default/example to use `default_coding_task`
- Update CLI defaults in `src/cli/main.py`
- Update API/task model defaults in `src/api/models.py`
- Update webhook defaults in `src/integrations/webhooks.py`
- Update any hardcoded blueprint catalogues or references in `src/api/routes.py`
- Update README examples and blueprint documentation

**Files to edit:**
- `blueprints/default.yaml`
- `src/cli/main.py`
- `src/api/models.py`
- `src/integrations/webhooks.py`
- `src/api/routes.py`
- `README.md`

**Tests to add/update:**
- Add a CLI/unit test that the default blueprint resolves successfully
- Add an API test that creating a default task uses a valid registered blueprint

**Done when:**
- There is exactly one canonical default blueprint name
- No docs/examples reference nonexistent blueprints

#### Task P0.2: Clearly separate real execution from placeholder behavior

**Goal:** Prevent simulated task execution from being mistaken for the real engine.

**Implementation tasks:**
- Remove or quarantine the stubbed API `_execute_task` path
- If temporary stubbing must remain, make it explicit via naming and comments and keep it unreachable from normal task creation
- Audit README/API descriptions so they do not claim end-to-end execution if the path is still simulated

**Files to edit:**
- `src/api/routes.py`
- `README.md`

**Tests to add/update:**
- Add a regression test that API task execution goes through the shared execution service once introduced

**Done when:**
- There is no default code path where a task appears to run successfully without the real engine

### P1 Checklist: Unify Task Execution Across Entry Points

#### Task P1.1: Introduce a shared task runner service

**Goal:** One orchestration path owns blueprint loading, runtime setup, sandbox lifecycle, rule loading, execution, and result assembly.

**Implementation tasks:**
- Create a new shared orchestration module:
  - recommended path: `src/orchestration/task_runner.py`
- Move the following responsibilities out of CLI inline code:
  - builtin tool working-dir setup
  - MCP connection/discovery
  - LLM/runtime construction
  - blueprint registry loading
  - rule loading
  - sandbox start/stop
  - blueprint execution
  - final result assembly
- Define a structured request model for this runner, separate from transport-specific request models if helpful
- Define a structured result object that can feed CLI output and API persistence/status

**Files to create:**
- `src/orchestration/__init__.py`
- `src/orchestration/task_runner.py`

**Files to edit:**
- `src/cli/main.py`
- possibly `src/api/models.py` if a shared internal execution model is added

**Tests to add/update:**
- Add unit tests for the task runner covering:
  - successful blueprint resolution
  - sandbox startup fallback/behavior by mode
  - propagation of task metadata into `NodeContext`

**Done when:**
- CLI no longer manually assembles the whole execution stack inline
- One importable service can run a task end-to-end

#### Task P1.2: Route API through the shared task runner

**Goal:** API-created tasks use the same execution engine as CLI-created runs.

**Implementation tasks:**
- Replace the placeholder background execution path in `src/api/routes.py`
- Create real run records/status transitions around the shared task runner
- Capture step progress, terminal status, errors, duration, and PR URL
- Keep API responses asynchronous if desired, but the background job must invoke the real runner

**Files to edit:**
- `src/api/routes.py`
- `src/api/models.py`
- `src/api/server.py`

**Tests to add/update:**
- API test that created tasks transition through real status states
- API test that failure in blueprint execution marks the task/run as failed rather than silently “completing”

**Done when:**
- API tasks no longer use fake step names/sleeps
- Run state reflects real blueprint execution

#### Task P1.3: Route Slack and webhook integrations through the same service

**Goal:** Non-CLI entry points only construct task requests; they do not implement execution logic themselves.

**Implementation tasks:**
- Refactor `src/integrations/slack.py` so it creates a task request and hands it to the shared orchestration/task layer
- Refactor `src/integrations/webhooks.py` similarly
- Preserve per-entry-point metadata such as source, creator/channel, branch, and linked context

**Files to edit:**
- `src/integrations/slack.py`
- `src/integrations/webhooks.py`
- orchestration/task runner layer

**Tests to add/update:**
- Integration/unit test that Slack task creation invokes the same backend task creation path
- Integration/unit test that webhook-triggered tasks preserve base branch and source metadata

**Done when:**
- Slack/webhooks do not bypass or duplicate orchestration behavior

### P2 Checklist: Propagate Task Metadata End-to-End

#### Task P2.1: Define runtime semantics for every task field

**Goal:** Eliminate “stored but ignored” task fields.

**Implementation tasks:**
- Audit `TaskCreate` fields in `src/api/models.py`
- For each field, either:
  - implement runtime behavior, or
  - remove it from the public contract
- Document the semantics inline in code and in README/API docs

**Fields to resolve:**
- `branch_base`
- `context_links`
- `blueprint`
- `tools`
- `rules_override`
- `priority`
- `created_by`

**Files to edit:**
- `src/api/models.py`
- `README.md`
- orchestration/task runner layer

**Done when:**
- No task model field is inert or ambiguous

#### Task P2.2: Propagate branch metadata into git and PR flows

**Goal:** Base branch selection and branch creation must reflect task input.

**Implementation tasks:**
- Add `branch_base` to `NodeContext` or equivalent execution context
- Use it in:
  - branch creation logic if needed
  - push/PR creation logic
  - any CI polling that depends on branch/ref
- Remove hardcoded `main` assumptions from deterministic PR logic

**Files to edit:**
- `src/blueprint/nodes/deterministic.py`
- `src/output/pr.py`
- orchestration/task runner layer

**Tests to add/update:**
- Test that PR creation uses the task-provided base branch

**Done when:**
- `branch_base` has observable effect on execution output

#### Task P2.3: Propagate tool and rules overrides

**Goal:** Task-level metadata can constrain or augment the execution context.

**Implementation tasks:**
- Thread task-requested tool overrides into agentic node tool selection
- Thread `rules_override` into the effective rules passed to agentic steps
- Decide and document merge behavior:
  - additive
  - restrictive allowlist
  - override precedence

**Files to edit:**
- `src/blueprint/nodes/agentic.py`
- `src/tools/registry.py`
- `src/rules/resolver.py`
- orchestration/task runner layer

**Tests to add/update:**
- Test that task-level tool restrictions affect actual available tools
- Test that rules overrides appear in effective prompt context

**Done when:**
- Task metadata visibly changes tool/rule availability in a run

#### Task P2.4: Propagate priority and source metadata into scheduling/logging

**Goal:** Priority and source are useful operationally, not just informationally.

**Implementation tasks:**
- Add source metadata to task/run records:
  - CLI
  - API
  - Slack
  - webhook
- Add `priority` handling to queue submission or execution ordering if queueing exists
- Include source/creator metadata in logs and monitoring payloads

**Files to edit:**
- `src/api/models.py`
- `src/api/routes.py`
- `src/monitoring/logger.py`
- orchestration/task runner layer

**Tests to add/update:**
- Test that source metadata is preserved from ingestion to run record
- Test that priority is preserved and exposed in run state, or actively used if scheduling is implemented

**Done when:**
- Operational metadata survives ingestion and can be observed downstream

### Recommended Implementation Order Inside P0-P2

1. Complete P0.1 blueprint naming normalization.
2. Create the shared runner in P1.1.
3. Move CLI onto the shared runner.
4. Replace API stub execution via P1.2.
5. Refactor Slack and webhooks via P1.3.
6. Add branch metadata propagation via P2.2.
7. Add tool/rules propagation via P2.3.
8. Finish metadata cleanup and observability via P2.1 and P2.4.

### Recommended Test Additions for the First Work Package

- `tests/test_cli.py`
  - default blueprint resolution
  - CLI invocation routes through shared runner

- `tests/test_api.py`
  - real task execution status transitions
  - invalid/default blueprint regression coverage
  - task metadata persistence and exposure

- `tests/test_blueprint_engine.py`
  - effective context includes propagated branch/rules/tool overrides

- new `tests/test_task_runner.py`
  - shared orchestration behavior
  - MCP connection lifecycle
  - sandbox lifecycle
  - result/error mapping

- new `tests/test_integrations.py`
  - Slack and webhook parity with shared task creation/execution path
