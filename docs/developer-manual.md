# Minions Developer Manual

## Purpose

This manual is for engineers extending or operating the Minions framework itself.
It focuses on how the runtime is wired, where key behavior lives, and how to safely add new capabilities without breaking unattended execution.

## Mental Model

Minions is an unattended coding framework built from a small set of composable layers:

1. Entry points accept work.
   CLI, API, Slack, and webhook integrations normalize requests into a shared orchestration request.
2. Orchestration prepares the run.
   The task runner resolves the blueprint, configures sandbox and security policy, scopes tools, hydrates context, and builds rule scope.
3. The blueprint executes.
   Deterministic nodes run code-defined actions. Agentic nodes run the LLM loop with a constrained tool set and task-specific context.
4. Feedback closes the loop.
   Local lint/test, CI polling, autofix application, and bounded repair attempts turn one-shot runs into controlled iterative flows.
5. Outputs and operations are persisted.
   Tasks, statuses, logs, results, queue state, and leases are stored through the API state backend and exposed through APIs and the dashboard.

## Code Map

The highest-signal files for framework work are:

- [src/orchestration/task_runner.py](/Users/waleedarafa/minions/src/orchestration/task_runner.py)
  Shared request normalization, sandbox/tool setup, hydration, rule scoping, and blueprint execution.
- [src/blueprint/engine.py](/Users/waleedarafa/minions/src/blueprint/engine.py)
  State-machine execution for deterministic and agentic nodes.
- [src/blueprint/schema.py](/Users/waleedarafa/minions/src/blueprint/schema.py)
  Blueprint schema, validation, and registry loading.
- [src/blueprint/nodes/deterministic.py](/Users/waleedarafa/minions/src/blueprint/nodes/deterministic.py)
  Built-in deterministic actions such as git, lint, tests, CI, autofix, and PR creation.
- [src/blueprint/nodes/agentic.py](/Users/waleedarafa/minions/src/blueprint/nodes/agentic.py)
  Prompt assembly, scoped tool exposure, hydrated context injection, failure-summary injection, and rule-scope visibility.
- [src/context/hydration.py](/Users/waleedarafa/minions/src/context/hydration.py)
  Deterministic context pre-hydration for links, Jira, GitHub, and metadata hooks.
- [src/rules/resolver.py](/Users/waleedarafa/minions/src/rules/resolver.py)
  Scoped rule discovery and matching.
- [src/tools/security.py](/Users/waleedarafa/minions/src/tools/security.py)
  Tool and command policy enforcement.
- [src/sandbox/config.py](/Users/waleedarafa/minions/src/sandbox/config.py)
  Sandbox policy defaults.
- [src/sandbox/runner.py](/Users/waleedarafa/minions/src/sandbox/runner.py)
  Docker execution, pooled sandboxes, and shared manager lifecycle.
- [src/api/routes.py](/Users/waleedarafa/minions/src/api/routes.py)
  Task queue, leases, durable state integration, run history, and operator endpoints.
- [src/api/state.py](/Users/waleedarafa/minions/src/api/state.py)
  JSON and SQLite durability backends.

## Request Lifecycle

### 1. Trigger

Requests enter through:

- `minion run`
- `POST /api/tasks`
- Slack and webhook integrations that forward into the same shared orchestration path

The request contract is represented by:

- [TaskCreate](/Users/waleedarafa/minions/src/api/models.py)
- [TaskRunRequest](/Users/waleedarafa/minions/src/orchestration/task_runner.py)

Key fields:

- `description`
- `repo`
- `blueprint`
- `branch_base`
- `context_links`
- `tools`
- `rules_override`
- `priority`
- `source`
- `created_by`

### 2. Runtime Preparation

The task runner performs the framework-critical setup before the first blueprint node:

- loads project env from `.env`
- resolves the blueprint
- configures sandbox mode based on source
  `cli` defaults to development mode; API/background execution defaults to production mode
- configures file and command working directories
- installs the run-scoped security policy
- builds the run-scoped tool registry from blueprint tools plus orchestration-required tools
- connects matching MCP servers
- infers initial rule-scope targets
- pre-hydrates external context
- constructs the initial `NodeContext`

### 3. Blueprint Execution

Blueprints mix:

- deterministic nodes
  Example: `run_linters`, `run_tests`, `wait_for_ci`, `git_commit_and_push`
- agentic nodes
  Example: `plan`, `implement`, `fix_failures`, `handle_ci_failures`

The default blueprint lives at [blueprints/default.yaml](/Users/waleedarafa/minions/blueprints/default.yaml).

Execution is handled by [BlueprintEngine](/Users/waleedarafa/minions/src/blueprint/engine.py), which:

- evaluates node conditions
- runs deterministic actions directly
- runs agentic nodes through the runtime
- applies scoped rules per node
- enforces bounded CI behavior
- returns `completed`, `failed`, or `partial`

### 4. Feedback and Repair

The default flow is intentionally bounded:

1. deterministic local lint
2. deterministic local tests
3. one local fix/reverify path if needed
4. commit and push
5. CI round 1
6. deterministic autofix when available
7. one agentic CI-repair attempt if still needed
8. final local reverify
9. final push
10. CI round 2
11. PR handoff, including partial-success PRs when CI remains red after the cap

The relevant modules are:

- [src/feedback/lint.py](/Users/waleedarafa/minions/src/feedback/lint.py)
- [src/feedback/parser.py](/Users/waleedarafa/minions/src/feedback/parser.py)
- [src/feedback/autofix.py](/Users/waleedarafa/minions/src/feedback/autofix.py)
- [src/feedback/ci.py](/Users/waleedarafa/minions/src/feedback/ci.py)

## Blueprints

Blueprint files live in [blueprints/](/Users/waleedarafa/minions/blueprints).

Each node has:

- `name`
- `type`: `deterministic` or `agentic`
- deterministic fields such as `action` and `timeout`
- agentic fields such as `prompt`, `tools`, and `max_iterations`
- optional `condition`

Supported condition names are implemented in [src/blueprint/engine.py](/Users/waleedarafa/minions/src/blueprint/engine.py).
Supported deterministic actions are registered in [src/blueprint/nodes/deterministic.py](/Users/waleedarafa/minions/src/blueprint/nodes/deterministic.py).

### Adding a New Blueprint

1. Add a new YAML file in [blueprints/](/Users/waleedarafa/minions/blueprints).
2. Use only supported node types and deterministic action names.
3. Keep tool lists intentionally small.
4. Prefer deterministic nodes when the behavior is predictable and cheap.
5. Add tests that execute the blueprint or verify its registration.

## Rules and Context

### Rule Resolution

Rule loading is intentionally scoped, not global.

Rules are discovered from:

- `.minion/rules.yaml`
- `.cursorrules`
- `CLAUDE.md`

The runner resolves likely file targets from:

- task descriptions
- GitHub blob/tree links
- prior agent outputs such as plans

Only matching scoped rules are injected. When no targets are known, the system falls back to global rules only.

Observability:

- `rule_scope_targets`
- `rule_trace`
- prompt sections for active rule scope and active rule sources

### Hydration

Hydration happens before the first agentic node.

Built-in hydration supports:

- generic HTTP links
- GitHub issues, PRs, commits, and Actions runs
- Jira ticket metadata
- orchestration metadata hooks for MCP-backed enrichment

Observability:

- `hydrated_context`
- `hydration_trace`

## Tools and Security

### Tool Exposure

Tool availability is resolved per run, not globally.

The final allowlist is:

- all tool names referenced by the chosen blueprint
- plus orchestration-required tools such as CI helpers
- intersected with task-level `tools` when the request provides an allowlist

MCP tools are only registered into the run when their names are allowed for that run.

### Security Policy

Tool calls are checked against a per-run security policy that enforces:

- working-directory-aware path access
- allowed command basenames
- blocked sensitive paths
- network restrictions based on sandbox posture
- blocking of shell chaining and destructive command patterns

Denied operations are preserved in `security_events`.

## Sandbox and Environment Model

Current implementation:

- Docker-backed sandbox runner
- explicit `production`, `development`, and `disabled` modes
- production defaults to pooled Docker sandboxes with network off
- development can opt into host fallback when Docker is unavailable

The current repo is closest to a hardened pooled sandbox model.
The long-term plan still distinguishes this from a true hot devbox model with pre-warmed repos, caches, and background services.

## API, Queueing, and Persistence

The API is both an operator surface and a durable task runtime.

Important endpoints:

- `POST /api/tasks`
- `GET /api/tasks/{id}/status`
- `GET /api/tasks/{id}/result`
- `GET /api/tasks/{id}/logs/history`
- `GET /api/runs`
- `GET /api/runs/summary`
- `GET /api/logs/search`
- `GET /api/health`
- `GET /api/worker/health`

Queue/runtime behavior:

- priority-aware pending queue
- configurable concurrency cap
- lease-based task claiming
- lease renewal
- expired-lease requeue
- durable JSON or SQLite backend
- split `all`, `api`, and `worker` process roles

For multi-process usage, SQLite is the safer default.

## Outputs and Handoff

PR rendering flows through:

- [src/output/pr.py](/Users/waleedarafa/minions/src/output/pr.py)
- [src/output/templates/default.md](/Users/waleedarafa/minions/src/output/templates/default.md)

PRs include:

- task origin
- creator
- priority
- base branch
- linked and hydrated context
- active rule scope
- CI summary
- unresolved issues
- partial-success context when applicable

## Extension Guide

### Add a deterministic action

1. Implement the action in [src/blueprint/nodes/deterministic.py](/Users/waleedarafa/minions/src/blueprint/nodes/deterministic.py).
2. Register it in the action registry.
3. Add blueprint coverage and deterministic-action tests.

### Add a built-in tool

1. Implement the handler under [src/tools/builtin/](/Users/waleedarafa/minions/src/tools/builtin).
2. Register it through [src/tools/registry.py](/Users/waleedarafa/minions/src/tools/registry.py).
3. Decide whether it needs security-policy checks.
4. Add tests for both normal behavior and denied behavior.

### Add an MCP integration

1. Expose it through [src/tools/mcp_client.py](/Users/waleedarafa/minions/src/tools/mcp_client.py).
2. Keep its tool names intentionally scoped.
3. If it should enrich runs before the first agentic node, add a metadata hook path through [src/orchestration/task_runner.py](/Users/waleedarafa/minions/src/orchestration/task_runner.py).

### Add new operational metadata

1. Propagate it through `TaskCreate`, `Task`, `RunStatus`, and `RunResult` as needed.
2. Store it in `NodeContext.extra`.
3. Expose it through API responses and tests.

## Testing Strategy

Use these layers together:

- unit tests for rule resolution, hydration, tool security, sandbox behavior, and deterministic actions
- API integration tests for durable queueing, roles, leases, and observability
- example-backed tests for operator runbooks

The trigger-to-deployment example is documented in [docs/examples/trigger-to-deployment.md](/Users/waleedarafa/minions/docs/examples/trigger-to-deployment.md) and enforced by [tests/test_examples.py](/Users/waleedarafa/minions/tests/test_examples.py).

## Recommended Maintenance Checks

Before merging framework changes:

1. `pytest -q`
2. `ruff check .`
3. verify the default blueprint still reflects the intended unattended loop
4. verify docs examples still match the live API and CLI contracts
