from __future__ import annotations

from dataclasses import replace

from .models import CustomerTier, Ticket, TicketPriority, TicketStatus

_PRIORITY_WEIGHT = {
    "low": 1,
    "normal": 2,
    "high": 3,
    "urgent": 4,
}

_TIER_WEIGHT = {
    "standard": 1,
    "premium": 2,
    "enterprise": 3,
}

_SLA_LIMIT_HOURS = {
    "urgent": 2,
    "high": 8,
    "normal": 24,
    "low": 72,
}

_ALLOWED_TRANSITIONS: dict[TicketStatus, set[TicketStatus]] = {
    "open": {"in_progress", "waiting", "resolved"},
    "in_progress": {"waiting", "resolved"},
    "waiting": {"in_progress", "resolved"},
    "resolved": set(),
}


class HelpdeskService:
    def __init__(self) -> None:
        self._tickets: dict[str, Ticket] = {}
        self._counter = 0

    def create_ticket(
        self,
        title: str,
        *,
        priority: TicketPriority = "normal",
        customer_tier: CustomerTier = "standard",
        created_hour: int = 0,
    ) -> Ticket:
        self._counter += 1
        ticket = Ticket(
            id=f"T-{self._counter:04d}",
            title=title,
            priority=priority,
            customer_tier=customer_tier,
            created_hour=created_hour,
            last_updated_hour=created_hour,
        )
        self._tickets[ticket.id] = ticket
        return ticket

    def get_ticket(self, ticket_id: str) -> Ticket:
        try:
            return self._tickets[ticket_id]
        except KeyError as exc:
            raise KeyError(f"Unknown ticket: {ticket_id}") from exc

    def assign_ticket(self, ticket_id: str, assignee: str, *, current_hour: int) -> Ticket:
        if not assignee.strip():
            raise ValueError("Assignee must be a non-empty string")
        ticket = self.get_ticket(ticket_id)
        if not ticket.is_open:
            raise ValueError(f"Cannot reassign resolved ticket: {ticket_id}")
        updated = replace(ticket, assignee=assignee.strip(), last_updated_hour=current_hour)
        self._tickets[ticket_id] = updated
        return updated

    def bulk_reassign_tickets(
        self,
        ticket_ids: list[str],
        assignee: str,
        *,
        current_hour: int,
    ) -> list[Ticket]:
        if not assignee.strip():
            raise ValueError("Assignee must be a non-empty string")

        updated_tickets: list[Ticket] = []
        normalized_assignee = assignee.strip()
        for ticket_id in ticket_ids:
            ticket = self.get_ticket(ticket_id)
            if not ticket.is_open:
                raise ValueError(f"Cannot reassign resolved ticket: {ticket_id}")
            updated = replace(
                ticket,
                assignee=normalized_assignee,
                last_updated_hour=current_hour,
            )
            updated_tickets.append(updated)

        for ticket in updated_tickets:
            self._tickets[ticket.id] = ticket
        return updated_tickets

    def update_status(
        self,
        ticket_id: str,
        status: TicketStatus,
        *,
        current_hour: int,
    ) -> Ticket:
        ticket = self.get_ticket(ticket_id)
        if status not in _ALLOWED_TRANSITIONS[ticket.status]:
            raise ValueError(f"Cannot transition ticket from {ticket.status} to {status}")
        updated = replace(ticket, status=status, last_updated_hour=current_hour)
        self._tickets[ticket_id] = updated
        return updated

    def queue_snapshot(self, assignee: str | None = None) -> list[Ticket]:
        open_tickets = [ticket for ticket in self._tickets.values() if ticket.is_open]
        
        if assignee is not None:
            # Normalize assignee by stripping whitespace, consistent with assign_ticket behavior
            normalized_assignee = assignee.strip()
            # Filter for tickets assigned to the specified user
            # Empty string after stripping should not match any tickets (including unassigned ones)
            if normalized_assignee:
                open_tickets = [ticket for ticket in open_tickets if ticket.assignee == normalized_assignee]
            else:
                # If assignee is empty string or whitespace-only, return empty list
                open_tickets = []
        
        return sorted(open_tickets, key=self._queue_sort_key)

    def workload_summary(self) -> dict[str, dict[str, int]]:
        summary: dict[str, dict[str, int]] = {}
        for ticket in self._tickets.values():
            if ticket.assignee is None or not ticket.is_open:
                continue
            bucket = summary.setdefault(ticket.assignee, {"total": 0, "urgent": 0, "high": 0})
            bucket["total"] += 1
            if ticket.priority == "urgent":
                bucket["urgent"] += 1
            if ticket.priority == "high":
                bucket["high"] += 1
        return summary

    def sla_breaches(self, *, current_hour: int) -> list[Ticket]:
        breaches: list[Ticket] = []
        for ticket in self._tickets.values():
            if not ticket.is_open:
                continue
            age = current_hour - ticket.created_hour
            if age > _SLA_LIMIT_HOURS[ticket.priority]:
                breaches.append(ticket)
        return sorted(breaches, key=self._queue_sort_key)

    @staticmethod
    def _queue_sort_key(ticket: Ticket) -> tuple[int, int, int, str]:
        return (
            -_PRIORITY_WEIGHT[ticket.priority],
            -_TIER_WEIGHT[ticket.customer_tier],
            ticket.created_hour,
            ticket.id,
        )
