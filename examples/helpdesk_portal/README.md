# Helpdesk Portal Example

This is a small standalone project intended to be a realistic Minions target.

It gives you:

- a non-trivial but compact Python codebase
- local tests
- project-scoped Minions rules
- concrete task prompts you can run through the framework

## Project Layout

```text
examples/helpdesk_portal/
  .minion/rules.yaml
  pyproject.toml
  src/helpdesk_portal/
  tests/
```

## What the Project Does

The project models a small helpdesk triage service with:

- ticket creation
- assignment
- status transitions
- SLA breach detection
- workload summaries
- queue prioritization

## Run the Local Tests

```bash
cd examples/helpdesk_portal
PYTHONPATH=src pytest -q
```

## Use It With Minions

Run the framework against the example project from the repo root:

```bash
minion run \
  --repo examples/helpdesk_portal \
  "Add a bulk reassignment API to the helpdesk service and update the tests."
```

Or trigger it through the HTTP API:

```bash
curl -X POST http://localhost:8000/api/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "description": "Add a bulk reassignment API to the helpdesk service and update the tests.",
    "repo": "examples/helpdesk_portal",
    "blueprint": "default",
    "priority": "high",
    "rules_override": [
      "Keep the public service API small and test-first."
    ]
  }'
```

The complete feature exercise and checked-in task payload live at:

- [helpdesk-bulk-reassignment.md](/Users/waleedarafa/minions/docs/examples/helpdesk-bulk-reassignment.md)
- [bulk_reassignment_task.json](/Users/waleedarafa/minions/examples/helpdesk_portal/tasks/bulk_reassignment_task.json)

## Suggested Tasks

These are good framework exercises for this example project:

1. Add bulk reassignment with validation and tests.
2. Add filtering to `queue_snapshot()` by team or assignee.
3. Add serialization helpers for exporting open-ticket reports.
4. Add agent-level utilization limits to `workload_summary()`.

## Notes

- The project uses plain Python and pytest on purpose.
- The scoped rules in `.minion/rules.yaml` are there to exercise Minions rule resolution.
- The code is intentionally simple enough to understand quickly, but not so trivial that the agent has nothing to reason about.
