from __future__ import annotations

from dataclasses import dataclass


class DuplicateSkuError(Exception):
    """Raised when attempting to create an item with a SKU that already exists."""
    pass


@dataclass(slots=True)
class InventoryItem:
    sku: str
    name: str
    quantity: int
    reorder_point: int

    @property
    def in_stock(self) -> bool:
        return self.quantity > 0

    @property
    def low_stock(self) -> bool:
        return self.quantity <= self.reorder_point


class InventoryService:
    def __init__(self) -> None:
        self._items: dict[str, InventoryItem] = {
            "SKU-1": InventoryItem("SKU-1", "Keyboard", 12, 5),
            "SKU-2": InventoryItem("SKU-2", "Mouse", 0, 4),
            "SKU-3": InventoryItem("SKU-3", "Monitor", 3, 2),
        }

    def list_items(self) -> list[InventoryItem]:
        return sorted(self._items.values(), key=lambda item: item.sku)

    def get_item(self, sku: str) -> InventoryItem:
        try:
            return self._items[sku]
        except KeyError as exc:
            raise KeyError(f"Unknown SKU: {sku}") from exc

    def restock(self, sku: str, quantity: int) -> InventoryItem:
        if quantity <= 0:
            raise ValueError("Restock quantity must be positive")
        item = self.get_item(sku)
        updated = InventoryItem(
            sku=item.sku,
            name=item.name,
            quantity=item.quantity + quantity,
            reorder_point=item.reorder_point,
        )
        self._items[sku] = updated
        return updated

    def bulk_restock(self, items: list[dict[str, int]]) -> list[InventoryItem]:
        seen_skus: set[str] = set()
        updates: list[tuple[str, InventoryItem]] = []

        for entry in items:
            sku = entry["sku"]
            quantity = entry["quantity"]
            if sku in seen_skus:
                raise ValueError(f"Duplicate SKU in bulk restock request: {sku}")
            seen_skus.add(sku)
            if quantity <= 0:
                raise ValueError("Restock quantity must be positive")

            item = self.get_item(sku)
            updated = InventoryItem(
                sku=item.sku,
                name=item.name,
                quantity=item.quantity + quantity,
                reorder_point=item.reorder_point,
            )
            updates.append((sku, updated))

        for sku, updated in updates:
            self._items[sku] = updated
        return [updated for _, updated in updates]

    def create_item(
        self,
        sku: str,
        name: str,
        quantity: int = 0,
        reorder_point: int = 0,
    ) -> InventoryItem:
        if not sku or not sku.strip():
            raise ValueError("SKU cannot be empty")
        if not name or not name.strip():
            raise ValueError("Name cannot be empty")
        if quantity < 0:
            raise ValueError("Quantity cannot be negative")
        if reorder_point < 0:
            raise ValueError("Reorder point cannot be negative")
        if sku in self._items:
            raise DuplicateSkuError(f"SKU already exists: {sku}")

        item = InventoryItem(
            sku=sku,
            name=name,
            quantity=quantity,
            reorder_point=reorder_point,
        )
        self._items[sku] = item
        return item


service = InventoryService()
