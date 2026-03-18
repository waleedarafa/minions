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


def test_create_item_with_all_fields() -> None:
    response = client.post(
        "/items",
        json={
            "sku": "SKU-NEW-1",
            "name": "New Item",
            "quantity": 10,
            "reorder_point": 5,
        },
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["sku"] == "SKU-NEW-1"
    assert payload["name"] == "New Item"
    assert payload["quantity"] == 10
    assert payload["reorder_point"] == 5
    assert payload["in_stock"] is True
    assert payload["low_stock"] is False


def test_create_item_with_default_values() -> None:
    response = client.post(
        "/items",
        json={
            "sku": "SKU-NEW-2",
            "name": "Another Item",
        },
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["sku"] == "SKU-NEW-2"
    assert payload["name"] == "Another Item"
    assert payload["quantity"] == 0
    assert payload["reorder_point"] == 0
    assert payload["in_stock"] is False
    assert payload["low_stock"] is True


def test_create_item_with_zero_quantity_and_reorder_point() -> None:
    response = client.post(
        "/items",
        json={
            "sku": "SKU-NEW-3",
            "name": "Zero Item",
            "quantity": 0,
            "reorder_point": 0,
        },
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["quantity"] == 0
    assert payload["reorder_point"] == 0


def test_create_item_rejects_empty_sku() -> None:
    response = client.post(
        "/items",
        json={
            "sku": "",
            "name": "Item",
        },
    )
    assert response.status_code == 400
    assert "SKU cannot be empty" in response.json()["detail"]


def test_create_item_rejects_empty_name() -> None:
    response = client.post(
        "/items",
        json={
            "sku": "SKU-NEW-4",
            "name": "",
        },
    )
    assert response.status_code == 400
    assert "Name cannot be empty" in response.json()["detail"]


def test_create_item_rejects_negative_quantity() -> None:
    response = client.post(
        "/items",
        json={
            "sku": "SKU-NEW-5",
            "name": "Item",
            "quantity": -1,
        },
    )
    assert response.status_code == 400
    assert "Quantity cannot be negative" in response.json()["detail"]


def test_create_item_rejects_negative_reorder_point() -> None:
    response = client.post(
        "/items",
        json={
            "sku": "SKU-NEW-6",
            "name": "Item",
            "reorder_point": -1,
        },
    )
    assert response.status_code == 400
    assert "Reorder point cannot be negative" in response.json()["detail"]


def test_create_item_rejects_duplicate_sku() -> None:
    response = client.post(
        "/items",
        json={
            "sku": "SKU-1",
            "name": "Duplicate Item",
        },
    )
    assert response.status_code == 409
    assert "SKU already exists" in response.json()["detail"]


def test_create_item_persists_to_inventory() -> None:
    # Create a new item
    create_response = client.post(
        "/items",
        json={
            "sku": "SKU-NEW-7",
            "name": "Persistent Item",
            "quantity": 15,
            "reorder_point": 3,
        },
    )
    assert create_response.status_code == 201

    # Verify it can be retrieved
    get_response = client.get("/items/SKU-NEW-7")
    assert get_response.status_code == 200
    payload = get_response.json()
    assert payload["sku"] == "SKU-NEW-7"
    assert payload["name"] == "Persistent Item"
    assert payload["quantity"] == 15
    assert payload["reorder_point"] == 3
