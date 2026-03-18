# Helpdesk Feature Exercise

This example shows how to use Minions against the [helpdesk portal example project](/Users/waleedarafa/minions/examples/helpdesk_portal/README.md) for a concrete feature task.

The feature is intentionally realistic but bounded:

- add bulk reassignment to the helpdesk service
- validate input
- update workload accounting
- add focused tests

## Feature Goal

Add a new service method with this behavior:

- input: a list of ticket ids, a target assignee, and `current_hour`
- every referenced ticket must exist
- assignee must be non-empty
- resolved tickets must not be reassigned
- return the updated tickets in the same order as the input ids

## Acceptance Criteria

The final change should include:

1. a new `bulk_reassign_tickets(...)` API in [service.py](/Users/waleedarafa/minions/examples/helpdesk_portal/src/helpdesk_portal/service.py)
2. tests covering:
   - happy path
   - unknown ticket ids
   - blank assignee rejection
   - resolved ticket rejection
3. no regressions in the existing queue and workload behavior

## Local Validation

From the repo root:

```bash
PYTHONPATH=examples/helpdesk_portal/src pytest -q examples/helpdesk_portal/tests
```

## Run With Minions CLI

```bash
minion run \
  --repo examples/helpdesk_portal \
  --blueprint default \
  "Add bulk_reassign_tickets(ticket_ids, assignee, current_hour) to the helpdesk service. Reject blank assignees, reject unknown ticket ids, reject resolved tickets, preserve input order, and update tests."
```

## Run Through the API

The same task payload is checked into the repo at [bulk_reassignment_task.json](/Users/waleedarafa/minions/examples/helpdesk_portal/tasks/bulk_reassignment_task.json).

```bash
curl -X POST http://localhost:8000/api/tasks \
  -H "Content-Type: application/json" \
  --data @examples/helpdesk_portal/tasks/bulk_reassignment_task.json
```

## What to Inspect

After the worker completes the task, inspect:

- `GET /api/tasks/{id}/result`
- `GET /api/tasks/{id}/logs/history`
- `GET /api/runs?q=bulk_reassign_tickets`

The agent should primarily touch:

- [service.py](/Users/waleedarafa/minions/examples/helpdesk_portal/src/helpdesk_portal/service.py)
- [test_service.py](/Users/waleedarafa/minions/examples/helpdesk_portal/tests/test_service.py)

## Why This Is a Good Framework Exercise

This task exercises:

- scoped rules for service code and tests
- deterministic lint/test nodes
- a small but meaningful multi-file change
- clear acceptance criteria for human review
