from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .service import service


class RestockRequest(BaseModel):
    quantity: int


class BulkRestockItem(BaseModel):
    sku: str
    quantity: int


class BulkRestockRequest(BaseModel):
    items: list[BulkRestockItem]


app = FastAPI(title="Inventory API Example")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/items")
def list_items() -> list[dict[str, object]]:
    return [
        {
            "sku": item.sku,
            "name": item.name,
            "quantity": item.quantity,
            "reorder_point": item.reorder_point,
            "in_stock": item.in_stock,
            "low_stock": item.low_stock,
        }
        for item in service.list_items()
    ]


@app.get("/items/{sku}")
def get_item(sku: str) -> dict[str, object]:
    try:
        item = service.get_item(sku)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "sku": item.sku,
        "name": item.name,
        "quantity": item.quantity,
        "reorder_point": item.reorder_point,
        "in_stock": item.in_stock,
        "low_stock": item.low_stock,
    }


@app.post("/items/{sku}/restock")
def restock_item(sku: str, payload: RestockRequest) -> dict[str, object]:
    try:
        item = service.restock(sku, payload.quantity)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "sku": item.sku,
        "name": item.name,
        "quantity": item.quantity,
        "reorder_point": item.reorder_point,
        "in_stock": item.in_stock,
        "low_stock": item.low_stock,
    }


@app.post("/restock/bulk")
def bulk_restock(payload: BulkRestockRequest) -> list[dict[str, object]]:
    try:
        items = service.bulk_restock(
            [{"sku": item.sku, "quantity": item.quantity} for item in payload.items]
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return [
        {
            "sku": item.sku,
            "name": item.name,
            "quantity": item.quantity,
            "reorder_point": item.reorder_point,
            "in_stock": item.in_stock,
            "low_stock": item.low_stock,
        }
        for item in items
    ]
