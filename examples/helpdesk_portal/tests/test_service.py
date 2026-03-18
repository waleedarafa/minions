from helpdesk_portal import HelpdeskService


def test_queue_snapshot_prioritizes_priority_then_customer_tier() -> None:
    service = HelpdeskService()
    low = service.create_ticket("Low standard", priority="low", customer_tier="standard")
    premium_high = service.create_ticket(
        "High premium",
        priority="high",
        customer_tier="premium",
    )
    enterprise_high = service.create_ticket(
        "High enterprise",
        priority="high",
        customer_tier="enterprise",
    )
    urgent = service.create_ticket("Urgent standard", priority="urgent", customer_tier="standard")

    queue = service.queue_snapshot()

    assert [ticket.id for ticket in queue] == [
        urgent.id,
        enterprise_high.id,
        premium_high.id,
        low.id,
    ]


def test_assign_ticket_updates_assignee_and_workload_summary() -> None:
    service = HelpdeskService()
    urgent = service.create_ticket("Urgent customer", priority="urgent")
    high = service.create_ticket("High customer", priority="high")

    service.assign_ticket(urgent.id, "alice", current_hour=1)
    service.assign_ticket(high.id, "alice", current_hour=2)

    assert service.workload_summary() == {
        "alice": {
            "total": 2,
            "urgent": 1,
            "high": 1,
        }
    }


def test_bulk_reassign_tickets_preserves_input_order_and_updates_workload() -> None:
    service = HelpdeskService()
    first = service.create_ticket("First urgent", priority="urgent")
    second = service.create_ticket("Second high", priority="high")
    third = service.create_ticket("Third normal")

    updated = service.bulk_reassign_tickets(
        [second.id, first.id, third.id],
        "  alice  ",
        current_hour=4,
    )

    assert [ticket.id for ticket in updated] == [second.id, first.id, third.id]
    assert all(ticket.assignee == "alice" for ticket in updated)
    assert service.workload_summary() == {
        "alice": {
            "total": 3,
            "urgent": 1,
            "high": 1,
        }
    }


def test_bulk_reassign_rejects_unknown_ticket_ids() -> None:
    service = HelpdeskService()
    ticket = service.create_ticket("Known ticket")

    try:
        service.bulk_reassign_tickets([ticket.id, "T-9999"], "alice", current_hour=3)
    except KeyError as exc:
        assert "Unknown ticket" in str(exc)
    else:
        raise AssertionError("Expected unknown ticket id to fail bulk reassignment")


def test_bulk_reassign_rejects_blank_assignee() -> None:
    service = HelpdeskService()
    ticket = service.create_ticket("Known ticket")

    try:
        service.bulk_reassign_tickets([ticket.id], "   ", current_hour=3)
    except ValueError as exc:
        assert "non-empty" in str(exc)
    else:
        raise AssertionError("Expected blank assignee to fail bulk reassignment")


def test_bulk_reassign_rejects_resolved_tickets() -> None:
    service = HelpdeskService()
    open_ticket = service.create_ticket("Open ticket")
    resolved_ticket = service.create_ticket("Resolved ticket")
    service.update_status(resolved_ticket.id, "resolved", current_hour=2)

    try:
        service.bulk_reassign_tickets(
            [open_ticket.id, resolved_ticket.id],
            "alice",
            current_hour=3,
        )
    except ValueError as exc:
        assert resolved_ticket.id in str(exc)
    else:
        raise AssertionError("Expected resolved ticket to fail bulk reassignment")


def test_sla_breaches_ignore_resolved_tickets() -> None:
    service = HelpdeskService()
    overdue = service.create_ticket(
        "Overdue urgent issue",
        priority="urgent",
        created_hour=0,
    )
    resolved = service.create_ticket(
        "Resolved issue",
        priority="urgent",
        created_hour=0,
    )

    service.update_status(resolved.id, "resolved", current_hour=1)
    breaches = service.sla_breaches(current_hour=3)

    assert [ticket.id for ticket in breaches] == [overdue.id]


def test_invalid_status_transition_is_rejected() -> None:
    service = HelpdeskService()
    ticket = service.create_ticket("Waiting flow")

    service.update_status(ticket.id, "waiting", current_hour=1)

    try:
        service.update_status(ticket.id, "open", current_hour=2)
    except ValueError as exc:
        assert "Cannot transition" in str(exc)
    else:
        raise AssertionError("Expected invalid status transition to fail")


def test_unknown_ticket_lookup_raises_clear_error() -> None:
    service = HelpdeskService()

    try:
        service.get_ticket("T-9999")
    except KeyError as exc:
        assert "Unknown ticket" in str(exc)
    else:
        raise AssertionError("Expected unknown ticket lookup to fail")
