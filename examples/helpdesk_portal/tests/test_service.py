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


def test_queue_snapshot_filters_by_assignee() -> None:
    service = HelpdeskService()
    
    # Create tickets with different assignees and priorities
    alice_urgent = service.create_ticket("Alice urgent", priority="urgent")
    alice_normal = service.create_ticket("Alice normal", priority="normal")
    bob_high = service.create_ticket("Bob high", priority="high")
    service.create_ticket("Unassigned urgent", priority="urgent")  # remains unassigned
    
    # Assign tickets
    service.assign_ticket(alice_urgent.id, "alice", current_hour=1)
    service.assign_ticket(alice_normal.id, "alice", current_hour=2)
    service.assign_ticket(bob_high.id, "bob", current_hour=3)
    
    # Test filtering by alice - should return alice's tickets sorted by priority
    alice_queue = service.queue_snapshot(assignee="alice")
    assert len(alice_queue) == 2
    assert [ticket.id for ticket in alice_queue] == [alice_urgent.id, alice_normal.id]
    
    # Test filtering by bob - should return only bob's ticket
    bob_queue = service.queue_snapshot(assignee="bob")
    assert len(bob_queue) == 1
    assert bob_queue[0].id == bob_high.id
    
    # Test filtering by non-existent assignee - should return empty list
    empty_queue = service.queue_snapshot(assignee="charlie")
    assert len(empty_queue) == 0


def test_queue_snapshot_preserves_default_behavior_when_assignee_none() -> None:
    service = HelpdeskService()
    
    # Create the same tickets as in the original test
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
    
    # Assign some tickets to verify unassigned tickets are still included
    service.assign_ticket(premium_high.id, "alice", current_hour=1)
    
    # Test that default behavior (assignee=None) returns all open tickets
    queue_default = service.queue_snapshot()
    queue_explicit_none = service.queue_snapshot(assignee=None)
    
    # Both should return the same results
    assert len(queue_default) == 4
    assert len(queue_explicit_none) == 4
    assert [ticket.id for ticket in queue_default] == [ticket.id for ticket in queue_explicit_none]
    
    # Verify sorting is still correct
    assert [ticket.id for ticket in queue_default] == [
        urgent.id,
        enterprise_high.id,
        premium_high.id,
        low.id,
    ]


def test_queue_snapshot_assignee_filtering_preserves_sorting() -> None:
    service = HelpdeskService()
    
    # Create tickets for alice with different priorities and customer tiers
    alice_low_standard = service.create_ticket(
        "Alice low standard", 
        priority="low", 
        customer_tier="standard",
        created_hour=1
    )
    alice_high_premium = service.create_ticket(
        "Alice high premium", 
        priority="high", 
        customer_tier="premium",
        created_hour=2
    )
    alice_high_enterprise = service.create_ticket(
        "Alice high enterprise", 
        priority="high", 
        customer_tier="enterprise",
        created_hour=3
    )
    alice_urgent_standard = service.create_ticket(
        "Alice urgent standard", 
        priority="urgent", 
        customer_tier="standard",
        created_hour=4
    )
    
    # Assign all tickets to alice
    service.assign_ticket(alice_low_standard.id, "alice", current_hour=5)
    service.assign_ticket(alice_high_premium.id, "alice", current_hour=6)
    service.assign_ticket(alice_high_enterprise.id, "alice", current_hour=7)
    service.assign_ticket(alice_urgent_standard.id, "alice", current_hour=8)
    
    # Get alice's queue
    alice_queue = service.queue_snapshot(assignee="alice")
    
    # Verify sorting: urgent first, then high (enterprise before premium), then low
    assert [ticket.id for ticket in alice_queue] == [
        alice_urgent_standard.id,
        alice_high_enterprise.id,
        alice_high_premium.id,
        alice_low_standard.id,
    ]


def test_queue_snapshot_assignee_whitespace_handling() -> None:
    service = HelpdeskService()
    
    # Create tickets
    alice_ticket = service.create_ticket("Alice ticket")
    bob_ticket = service.create_ticket("Bob ticket")
    
    # Assign tickets (assign_ticket already strips whitespace)
    service.assign_ticket(alice_ticket.id, "  alice  ", current_hour=1)
    service.assign_ticket(bob_ticket.id, "bob", current_hour=2)
    
    # Test that queue_snapshot also handles whitespace consistently
    alice_queue_with_spaces = service.queue_snapshot(assignee="  alice  ")
    alice_queue_no_spaces = service.queue_snapshot(assignee="alice")
    
    # Both should return alice's ticket
    assert len(alice_queue_with_spaces) == 1
    assert len(alice_queue_no_spaces) == 1
    assert alice_queue_with_spaces[0].id == alice_ticket.id
    assert alice_queue_no_spaces[0].id == alice_ticket.id


def test_queue_snapshot_empty_assignee_returns_empty_list() -> None:
    service = HelpdeskService()
    
    # Create tickets with various assignees
    alice_ticket = service.create_ticket("Alice ticket")
    service.create_ticket("Unassigned ticket")  # remains unassigned
    
    service.assign_ticket(alice_ticket.id, "alice", current_hour=1)
    
    # Test that empty string and whitespace-only assignee return empty list
    empty_queue = service.queue_snapshot(assignee="")
    whitespace_queue = service.queue_snapshot(assignee="   ")
    
    assert len(empty_queue) == 0
    assert len(whitespace_queue) == 0


def test_queue_snapshot_excludes_resolved_tickets_with_assignee_filter() -> None:
    service = HelpdeskService()
    
    # Create tickets for alice
    alice_open = service.create_ticket("Alice open")
    alice_resolved = service.create_ticket("Alice resolved")
    
    # Assign both tickets to alice
    service.assign_ticket(alice_open.id, "alice", current_hour=1)
    service.assign_ticket(alice_resolved.id, "alice", current_hour=2)
    
    # Resolve one ticket
    service.update_status(alice_resolved.id, "resolved", current_hour=3)
    
    # Alice's queue should only include the open ticket
    alice_queue = service.queue_snapshot(assignee="alice")
    assert len(alice_queue) == 1
    assert alice_queue[0].id == alice_open.id
