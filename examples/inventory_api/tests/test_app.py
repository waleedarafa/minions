from fastapi.testclient import TestClient

from inventory_api.app import app


client = TestClient(app)


def test_health_endpoint() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_list_items_exposes_inventory_flags() -> None:
    response = client.get("/items")
    assert response.status_code == 200
    payload = response.json()
    assert len(payload) >= 3
    assert payload[0]["sku"] == "SKU-1"
    assert "in_stock" in payload[0]
    assert "low_stock" in payload[0]


def test_get_item_returns_404_for_unknown_sku() -> None:
    response = client.get("/items/UNKNOWN")
    assert response.status_code == 404
    assert "Unknown SKU" in response.json()["detail"]


def test_restock_item_updates_quantity() -> None:
    before = client.get("/items/SKU-2").json()
    response = client.post("/items/SKU-2/restock", json={"quantity": 5})

    assert response.status_code == 200
    payload = response.json()
    assert payload["sku"] == "SKU-2"
    assert payload["quantity"] == before["quantity"] + 5
    assert payload["in_stock"] is True


def test_restock_rejects_non_positive_quantity() -> None:
    response = client.post("/items/SKU-1/restock", json={"quantity": 0})
    assert response.status_code == 400
    assert "must be positive" in response.json()["detail"]


def test_bulk_restock_updates_multiple_skus_in_request_order() -> None:
    before_sku_2 = client.get("/items/SKU-2").json()
    before_sku_3 = client.get("/items/SKU-3").json()

    response = client.post(
        "/restock/bulk",
        json={
            "items": [
                {"sku": "SKU-3", "quantity": 4},
                {"sku": "SKU-2", "quantity": 2},
            ]
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert [item["sku"] for item in payload] == ["SKU-3", "SKU-2"]
    assert payload[0]["quantity"] == before_sku_3["quantity"] + 4
    assert payload[1]["quantity"] == before_sku_2["quantity"] + 2


def test_bulk_restock_rejects_duplicate_skus() -> None:
    response = client.post(
        "/restock/bulk",
        json={
            "items": [
                {"sku": "SKU-1", "quantity": 1},
                {"sku": "SKU-1", "quantity": 2},
            ]
        },
    )
    assert response.status_code == 400
    assert "Duplicate SKU" in response.json()["detail"]


def test_bulk_restock_rejects_unknown_skus() -> None:
    response = client.post(
        "/restock/bulk",
        json={"items": [{"sku": "SKU-404", "quantity": 2}]},
    )
    assert response.status_code == 404
    assert "Unknown SKU" in response.json()["detail"]


def test_bulk_restock_rejects_non_positive_quantities() -> None:
    response = client.post(
        "/restock/bulk",
        json={"items": [{"sku": "SKU-1", "quantity": 0}]},
    )
    assert response.status_code == 400
    assert "must be positive" in response.json()["detail"]
