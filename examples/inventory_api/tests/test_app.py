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


def test_create_item_success() -> None:
    response = client.post(
        "/items",
        json={
            "sku": "NEW-SKU",
            "name": "New Item",
            "quantity": 10,
            "reorder_point": 5,
        },
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["sku"] == "NEW-SKU"
    assert payload["name"] == "New Item"
    assert payload["quantity"] == 10
    assert payload["reorder_point"] == 5
    assert payload["in_stock"] is True
    assert payload["low_stock"] is False


def test_create_item_with_defaults() -> None:
    response = client.post(
        "/items",
        json={
            "sku": "DEFAULT-SKU",
            "name": "Default Item",
        },
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["sku"] == "DEFAULT-SKU"
    assert payload["name"] == "Default Item"
    assert payload["quantity"] == 0
    assert payload["reorder_point"] == 0
    assert payload["in_stock"] is False
    assert payload["low_stock"] is True


def test_create_item_rejects_duplicate_sku() -> None:
    response = client.post(
        "/items",
        json={
            "sku": "SKU-1",  # This SKU already exists
            "name": "Duplicate Item",
        },
    )
    assert response.status_code == 409
    assert "SKU already exists" in response.json()["detail"]


def test_create_item_rejects_empty_name() -> None:
    response = client.post(
        "/items",
        json={
            "sku": "EMPTY-NAME-SKU",
            "name": "",
        },
    )
    assert response.status_code == 400
    assert "name cannot be empty" in response.json()["detail"]


def test_create_item_rejects_whitespace_only_name() -> None:
    response = client.post(
        "/items",
        json={
            "sku": "WHITESPACE-SKU",
            "name": "   ",
        },
    )
    assert response.status_code == 400
    assert "name cannot be empty" in response.json()["detail"]


def test_create_item_rejects_negative_quantity() -> None:
    response = client.post(
        "/items",
        json={
            "sku": "NEG-QTY-SKU",
            "name": "Negative Quantity Item",
            "quantity": -5,
        },
    )
    assert response.status_code == 400
    assert "Quantity cannot be negative" in response.json()["detail"]


def test_create_item_rejects_negative_reorder_point() -> None:
    response = client.post(
        "/items",
        json={
            "sku": "NEG-REORDER-SKU",
            "name": "Negative Reorder Point Item",
            "reorder_point": -3,
        },
    )
    assert response.status_code == 400
    assert "Reorder point cannot be negative" in response.json()["detail"]
