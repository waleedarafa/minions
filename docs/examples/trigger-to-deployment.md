# Trigger-to-Deployment Example

This example shows the full operational path for Minions in the split-process deployment model:

1. deploy an API-only process
2. deploy a worker process against the same durable state backend
3. trigger a task through the API
4. watch the worker claim and execute it
5. inspect status, result, logs, history, and health

The flow below is backed by [tests/test_examples.py](/Users/waleedarafa/minions/tests/test_examples.py).

## Prerequisites

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Use a shared SQLite state backend for split API and worker processes:

```bash
export MINIONS_API_STATE_PATH=".minion/state/api-state.db"
export MINIONS_API_STATE_BACKEND="sqlite"
export MINIONS_MAX_CONCURRENT_TASKS="2"
```

## Step 1: Start the API

```bash
minion serve-api --host 0.0.0.0 --port 8000
```

Health check:

```bash
curl http://localhost:8000/api/health
```

Expected signals:

- `server_role` is `api`
- `dispatcher_enabled` is `false`
- new tasks will remain queued until a worker process starts

## Step 2: Start a Worker

In a separate terminal:

```bash
export MINIONS_WORKER_ID="worker-local-1"
minion worker --host 127.0.0.1 --port 8000
```

Worker health:

```bash
curl http://localhost:8000/api/worker/health
```

Expected signals:

- `server_role` is `worker` or `all`, depending on your deployment choice
- `dispatcher_enabled` is `true`
- `dispatcher_running` is `true`

## Step 3: Trigger a Task

```bash
curl -X POST http://localhost:8000/api/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "description": "Add validation to src/api/routes.py and update tests",
    "repo": ".",
    "blueprint": "default",
    "branch_base": "main",
    "priority": "high",
    "context_links": [
      "https://github.com/example/repo/blob/main/src/api/routes.py"
    ],
    "rules_override": [
      "Prefer minimal diffs and preserve API compatibility."
    ],
    "tools": [
      "read_file",
      "edit_file",
      "grep_search",
      "glob_search",
      "run_command"
    ]
  }'
```

The response returns a task object with:

- `id`
- `status`
- `priority`
- `source`

Capture the task id:

```bash
TASK_ID="..."
```

## Step 4: Follow Execution

Current status:

```bash
curl http://localhost:8000/api/tasks/$TASK_ID/status
```

Live logs:

```bash
curl http://localhost:8000/api/tasks/$TASK_ID/logs
```

Persisted log history:

```bash
curl http://localhost:8000/api/tasks/$TASK_ID/logs/history
```

When the task finishes:

```bash
curl http://localhost:8000/api/tasks/$TASK_ID/result
```

Important fields to inspect:

- `success`
- `steps`
- `ci_rounds`
- `ci_conclusion`
- `ci_summary`
- `hydration_trace`
- `rule_trace`
- `rule_scope_targets`
- `sandbox_mode`
- `sandbox_backend`
- `security_events`

## Step 5: Inspect the Operational Surfaces

Run history:

```bash
curl "http://localhost:8000/api/runs?priority=high&source=api"
```

Aggregate summary:

```bash
curl http://localhost:8000/api/runs/summary
```

Log search:

```bash
curl "http://localhost:8000/api/logs/search?q=sandbox"
```

Dashboard:

```bash
open http://localhost:8000/dashboard/
```

## Step 6: Deploy the Split Topology with Docker Compose

The repo includes a split API/worker example in [docker-compose.yaml](/Users/waleedarafa/minions/docker-compose.yaml).

Start it:

```bash
docker compose up --build
```

Services:

- `minion-api`
- `minion-worker`

Both share the same SQLite state volume, which lets the API and worker coordinate through the durable queue.

## What This Example Proves

This runbook is not just prose.
The automated example test verifies the same end-to-end flow:

- API-only process accepts and persists a queued task
- worker process loads the same durable state and executes the task
- a later API process can read the persisted result, logs, and run history

See [tests/test_examples.py](/Users/waleedarafa/minions/tests/test_examples.py).
