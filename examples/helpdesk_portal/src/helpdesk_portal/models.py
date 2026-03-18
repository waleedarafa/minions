from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

TicketPriority = Literal["low", "normal", "high", "urgent"]
TicketStatus = Literal["open", "in_progress", "waiting", "resolved"]
CustomerTier = Literal["standard", "premium", "enterprise"]


@dataclass(slots=True)
class Ticket:
    id: str
    title: str
    priority: TicketPriority
    customer_tier: CustomerTier
    status: TicketStatus = "open"
    assignee: str | None = None
    created_hour: int = 0
    last_updated_hour: int = 0

    @property
    def is_open(self) -> bool:
        return self.status != "resolved"
