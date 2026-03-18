# Inventory API Feature Exercise

This example shows how to use Minions against the [inventory API example project](/Users/waleedarafa/minions/examples/inventory_api/README.md) for a concrete HTTP feature.

The feature is:

- add a bulk restock endpoint
- validate request payloads
- keep route logic thin
- update the API tests

## Feature Goal

Add a new endpoint with this behavior:

- route: `POST /restock/bulk`
- input: a list of `{sku, quantity}` items
- reject duplicate SKUs in one request
- reject non-positive quantities
- return the updated inventory rows in request order
- return `404` if any referenced SKU is unknown

## Acceptance Criteria

The final change should include:

1. a service-level bulk restock method in [service.py](/Users/waleedarafa/minions/examples/inventory_api/src/inventory_api/service.py)
2. a new route in [app.py](/Users/waleedarafa/minions/examples/inventory_api/src/inventory_api/app.py)
3. focused route tests in [test_app.py](/Users/waleedarafa/minions/examples/inventory_api/tests/test_app.py)

## Local Validation

From the repo root:

```bash
PYTHONPATH=examples/inventory_api/src pytest -q examples/inventory_api/tests
```

## Run With Minions CLI

```bash
minion run \
  --repo examples/inventory_api \
  --blueprint default \
  "Add POST /restock/bulk to the inventory API. Accept a list of {sku, quantity}, reject duplicate SKUs, reject non-positive quantities, return 404 for unknown SKUs, keep route logic thin, and update the API tests."
```

## Run Through the API

The task payload is checked into the repo at [bulk_restock_task.json](/Users/waleedarafa/minions/examples/inventory_api/tasks/bulk_restock_task.json).

```bash
curl -X POST http://localhost:8000/api/tasks \
  -H "Content-Type: application/json" \
  --data @examples/inventory_api/tasks/bulk_restock_task.json
```

## What to Inspect

After the worker completes the task, inspect:

- `GET /api/tasks/{id}/result`
- `GET /api/tasks/{id}/logs/history`
- `GET /api/runs?q=/restock/bulk`

The agent should primarily touch:

- [service.py](/Users/waleedarafa/minions/examples/inventory_api/src/inventory_api/service.py)
- [app.py](/Users/waleedarafa/minions/examples/inventory_api/src/inventory_api/app.py)
- [test_app.py](/Users/waleedarafa/minions/examples/inventory_api/tests/test_app.py)

## Why This Is a Good Framework Exercise

This task exercises:

- scoped rules over API and tests
- route plus service edits
- deterministic test feedback
- a clear HTTP contract for human review
