# Inventory API Example

This is a second Minions target project, shaped like a small HTTP service instead of a pure domain library.

It gives you:

- a FastAPI app
- a small in-memory inventory service
- route tests with `TestClient`
- project-scoped rules for API work

## Run the Local Tests

```bash
cd examples/inventory_api
PYTHONPATH=src pytest -q
```

## Use It With Minions

```bash
minion run \
  --repo examples/inventory_api \
  "Add an endpoint to bulk restock multiple SKUs and update the API tests."
```

The complete feature exercise and checked-in task payload live at:

- [inventory-bulk-restock.md](/Users/waleedarafa/minions/docs/examples/inventory-bulk-restock.md)
- [bulk_restock_task.json](/Users/waleedarafa/minions/examples/inventory_api/tasks/bulk_restock_task.json)

## Suggested Tasks

1. Add `POST /restock/bulk`.
2. Add low-stock filtering to the list endpoint.
3. Add a report endpoint for out-of-stock SKUs.
4. Add optimistic validation for duplicate SKUs in bulk requests.
