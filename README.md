# Minions

Open-source autonomous coding agents inspired by Stripe's approach to AI-assisted development.

## Documentation

- [Developer Manual](/Users/waleedarafa/minions/docs/developer-manual.md)
- [Trigger-to-Deployment Example](/Users/waleedarafa/minions/docs/examples/trigger-to-deployment.md)
- [Helpdesk Feature Exercise](/Users/waleedarafa/minions/docs/examples/helpdesk-bulk-reassignment.md)
- [Inventory API Feature Exercise](/Users/waleedarafa/minions/docs/examples/inventory-bulk-restock.md)
- [Helpdesk Portal Example Project](/Users/waleedarafa/minions/examples/helpdesk_portal/README.md)
- [Inventory API Example Project](/Users/waleedarafa/minions/examples/inventory_api/README.md)

## Quick Start

```bash
# Install
pip install -e ".[dev]"

# Configure your API key
export ANTHROPIC_API_KEY="sk-ant-..."

# Run a task
minion run "Fix the failing tests in src/auth/"

# Start the combined API + worker process
minion serve
```

## Architecture

Minions is structured around four core concepts:

- **Agent Runtime** -- an LLM-in-the-loop executor that calls tools until the task is done.
- **Blueprints** -- declarative YAML workflows that mix deterministic steps (shell commands, linters) with agentic steps (LLM-driven reasoning).
- **Tools** -- a pluggable registry of capabilities the agent can invoke (read/edit files, run commands, search code, etc.).
- **Sandbox** -- Docker-based isolation so agents operate in a safe, reproducible environment.

Supporting systems include rule discovery, automated feedback (lint + test), and an HTTP API for programmatic access.

```
src/
  agent/        # LLM client, tool executor, context manager, runtime loop
  blueprint/    # Blueprint loader, validator, step execution engine
  tools/        # Tool registry, built-in tools (file I/O, shell, search)
  rules/        # Rule discovery (.minion/rules.yaml, .cursorrules, CLAUDE.md)
  feedback/     # Lint runner, pytest parser, autofix pattern matching
  sandbox/      # Docker container pool, sandbox configuration
  api/          # FastAPI HTTP interface
  cli/          # Typer CLI entry point
  integrations/ # Slack, GitHub, etc.
  monitoring/   # Logging, metrics, tracing
  output/       # Formatters (terminal, JSON, markdown)
```

## CLI Usage

```bash
# Run an autonomous task against the current repo
minion run "Add input validation to the signup endpoint"

# Run with a specific blueprint
minion run --blueprint fix_and_verify "Fix all linting errors"

# Use a different model
minion run --model gpt-4o "Refactor the database module"

# Start the combined API + worker process
minion serve

# Start an API-only process
minion serve-api

# Start a worker-capable process
minion worker

# List available blueprints
minion blueprints list
```

## Deployment Modes

Minions now supports explicit process roles on top of the durable task queue:

- `minion serve`
  Runs the API and dispatcher in one process. This is the simplest local setup.
- `minion serve-api`
  Runs the HTTP API only. Tasks remain queued until a worker-capable process claims them.
- `minion worker`
  Runs a worker-capable process against the same durable state backend.

The role is also reflected in `/api/health` through:

- `server_role`
- `dispatcher_enabled`
- `dispatcher_running`

### Example: split API and worker processes

```bash
# Shared durable state
export MINIONS_API_STATE_PATH=".minion/state/api-state.db"
export MINIONS_API_STATE_BACKEND="sqlite"

# Terminal 1: API only
minion serve-api --host 0.0.0.0 --port 8000

# Terminal 2: worker process
minion worker --host 127.0.0.1 --port 8000
```

## API Endpoints

| Method | Path                           | Description |
|--------|--------------------------------|-------------|
| GET    | `/api/health`                  | Health check and role/dispatcher state |
| GET    | `/api/worker/health`           | Worker-specific lease and dispatcher health |
| POST   | `/api/tasks`                   | Create a new task |
| GET    | `/api/tasks`                   | List all tasks |
| GET    | `/api/tasks/{id}`              | Get task details |
| GET    | `/api/tasks/{id}/status`       | Get live run status |
| GET    | `/api/tasks/{id}/result`       | Get final run result |
| GET    | `/api/tasks/{id}/logs/history` | Read persisted logs |
| GET    | `/api/runs`                    | Query run history |
| GET    | `/api/runs/summary`            | Aggregate run analytics |
| GET    | `/api/logs/search`             | Search persisted logs |
| GET    | `/api/blueprints`              | List available blueprints |

### Example: Create a task

```bash
curl -X POST http://localhost:8000/api/tasks \
  -H "Content-Type: application/json" \
  -d '{"description": "Fix the failing tests in src/auth/", "repo": "."}'
```

### Worker health

Use the worker health endpoint to inspect whether a process is actually capable of claiming and executing queued work:

```bash
curl http://localhost:8000/api/worker/health
```

Example response:

```json
{
  "status": "ok",
  "worker_id": "api-worker-1234",
  "server_role": "worker",
  "dispatcher_enabled": true,
  "dispatcher_running": true,
  "lease_seconds": 300,
  "lease_renew_interval_seconds": 30,
  "active_leases": 1,
  "leased_task_ids": ["task-abc"],
  "tasks_pending": 4
}
```

`/api/health` is still the general process-level health endpoint. `/api/worker/health` is the better signal when you need to confirm that a process can actually drain the queue and whether it is currently holding task leases.

## Blueprints

Blueprints are YAML files that define multi-step workflows.

```yaml
name: fix-lint
description: Fix linting errors in a repository
steps:
  - name: run-linter
    kind: deterministic
    config:
      command: "ruff check ."

  - name: apply-fixes
    kind: agentic
    config:
      prompt: "Fix the errors found by the linter."

  - name: optional-format
    kind: deterministic
    condition: needs_formatting
    config:
      command: "ruff format ."
```

**Step kinds:**

- `deterministic` -- runs a shell command or predefined operation.
- `agentic` -- hands control to the LLM agent with a prompt and tool access.

**Conditions:** Steps with a `condition` key are skipped unless that condition evaluates to true in the current execution context.

Place blueprint files in the `blueprints/` directory at the project root.

## Configuration

Minions reads configuration from environment variables and config files.

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | — | Anthropic API key |
| `OPENAI_API_KEY` | — | OpenAI API key (optional) |
| `MINION_MODEL` | `claude-sonnet-4-20250514` | Model to use |
| `MINION_MAX_TOKENS` | `128000` | Context window budget |
| `MINION_TEMPERATURE` | `0.0` | Sampling temperature |
| `MINION_SANDBOX` | `true` | Enable Docker sandbox |
| `MINION_LOG_LEVEL` | `info` | Log level (debug/info/warning) |
| `MINIONS_SERVER_ROLE` | `all` | Process role: `all`, `api`, or `worker` |
| `MINIONS_API_STATE_PATH` | `.minion/state/api_state.json` | Durable task/run state path |
| `MINIONS_API_STATE_BACKEND` | auto | Force durable state backend: `json` or `sqlite` |
| `MINIONS_METRICS_PATH` | unset | Persisted metrics file path |
| `MINIONS_MAX_CONCURRENT_TASKS` | `2` | Dispatcher concurrency limit |
| `MINIONS_TASK_LEASE_SECONDS` | `300` | Lease duration for claimed tasks |
| `MINIONS_LEASE_RENEW_INTERVAL_SECONDS` | `30` | Lease heartbeat interval |
| `MINIONS_WORKER_ID` | generated | Stable worker identity for claims/leases |

### Durable state backend

Use JSON for simple local development, or SQLite for safer multi-process coordination:

```bash
# JSON-backed state
export MINIONS_API_STATE_PATH=".minion/state/api-state.json"

# SQLite-backed state
export MINIONS_API_STATE_PATH=".minion/state/api-state.db"
export MINIONS_API_STATE_BACKEND="sqlite"
```

SQLite is the better default when you want separate API and worker processes sharing one queue.

### Rule files

Minions discovers project-specific rules from these files (searched upward from the working directory):

- `.minion/rules.yaml` -- YAML list of rules with optional scope patterns.
- `.cursorrules` -- Plain text, one rule per line (comments with `#`).
- `CLAUDE.md` -- Markdown treated as a single rule block.

Deeper (more specific) rule files take precedence over shallower ones.

## Development

```bash
# Clone the repository
git clone https://github.com/your-org/minions.git
cd minions

# Create a virtual environment
python -m venv .venv
source .venv/bin/activate

# Install in development mode
pip install -e ".[dev]"

# Run tests
pytest

# Run tests with coverage
pytest --cov=src

# Lint
ruff check .
ruff format --check .
```

### Docker

```bash
# Build the sandbox image
docker build -t minions-sandbox .

# Start split API + worker services
docker compose up

# Scale worker processes if needed
docker compose up --scale minion-worker=2

# Run tests inside the API container
docker compose run minion-api pytest
```

The compose file now uses the durable SQLite queue by default and runs:

- `minion-api`: API-only process
- `minion-worker`: worker-capable process sharing the same queue state

## License

MIT
