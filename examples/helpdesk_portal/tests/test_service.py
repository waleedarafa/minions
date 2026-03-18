from fastapi.testclient import TestClient

from helpdesk_portal import HelpdeskService
from helpdesk_portal.app import app


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
    
    # Create tickets with different priorities and assignees
    alice_urgent = service.create_ticket("Alice urgent", priority="urgent")
    alice_normal = service.create_ticket("Alice normal", priority="normal")
    bob_high = service.create_ticket("Bob high", priority="high")
    unassigned = service.create_ticket("Unassigned", priority="high")
    
    # Assign tickets
    service.assign_ticket(alice_urgent.id, "alice", current_hour=1)
    service.assign_ticket(alice_normal.id, "alice", current_hour=2)
    service.assign_ticket(bob_high.id, "bob", current_hour=3)
    
    # Test filtering by alice - should return alice's tickets sorted by priority
    alice_queue = service.queue_snapshot(assignee="alice")
    assert len(alice_queue) == 2
    assert [ticket.id for ticket in alice_queue] == [alice_urgent.id, alice_normal.id]
    
    # Test filtering by bob
    bob_queue = service.queue_snapshot(assignee="bob")
    assert len(bob_queue) == 1
    assert bob_queue[0].id == bob_high.id
    
    # Test filtering by non-existent assignee
    empty_queue = service.queue_snapshot(assignee="charlie")
    assert len(empty_queue) == 0
    
    # Test no filtering (default behavior) - should return all open tickets
    all_queue = service.queue_snapshot()
    assert len(all_queue) == 4
    # Should be sorted by priority: urgent, high (bob T-0003 before unassigned T-0004 by id), normal
    expected_order = [alice_urgent.id, bob_high.id, unassigned.id, alice_normal.id]
    assert [ticket.id for ticket in all_queue] == expected_order


def test_queue_snapshot_assignee_filtering_preserves_priority_sorting() -> None:
    service = HelpdeskService()
    
    # Create multiple tickets for same assignee with different priorities and tiers
    low_standard = service.create_ticket("Low standard", priority="low", customer_tier="standard")
    high_premium = service.create_ticket("High premium", priority="high", customer_tier="premium")
    high_enterprise = service.create_ticket("High enterprise", priority="high", customer_tier="enterprise")
    urgent_standard = service.create_ticket("Urgent standard", priority="urgent", customer_tier="standard")
    
    # Assign all to alice
    service.assign_ticket(low_standard.id, "alice", current_hour=1)
    service.assign_ticket(high_premium.id, "alice", current_hour=2)
    service.assign_ticket(high_enterprise.id, "alice", current_hour=3)
    service.assign_ticket(urgent_standard.id, "alice", current_hour=4)
    
    # Filter by alice and verify sorting
    alice_queue = service.queue_snapshot(assignee="alice")
    assert len(alice_queue) == 4
    assert [ticket.id for ticket in alice_queue] == [
        urgent_standard.id,      # urgent first
        high_enterprise.id,      # high + enterprise
        high_premium.id,         # high + premium
        low_standard.id,         # low last
    ]


def test_queue_snapshot_assignee_filtering_edge_cases() -> None:
    service = HelpdeskService()
    
    ticket1 = service.create_ticket("Test ticket 1")
    ticket2 = service.create_ticket("Test ticket 2")
    
    service.assign_ticket(ticket1.id, "alice", current_hour=1)
    service.assign_ticket(ticket2.id, "bob", current_hour=2)
    
    # Test empty string assignee (should return all tickets like None)
    empty_queue = service.queue_snapshot(assignee="")
    assert len(empty_queue) == 2
    
    # Test whitespace-only assignee (should return all tickets like None)
    whitespace_queue = service.queue_snapshot(assignee="   ")
    assert len(whitespace_queue) == 2
    
    # Test None assignee (explicit default behavior)
    none_queue = service.queue_snapshot(assignee=None)
    assert len(none_queue) == 2
    
    # Test exact assignee match (case sensitive)
    alice_queue = service.queue_snapshot(assignee="alice")
    assert len(alice_queue) == 1
    assert alice_queue[0].id == ticket1.id
    
    # Test case sensitivity - "Alice" should not match "alice"
    case_queue = service.queue_snapshot(assignee="Alice")
    assert len(case_queue) == 0


def test_queue_snapshot_assignee_filtering_excludes_resolved_tickets() -> None:
    service = HelpdeskService()
    
    open_ticket = service.create_ticket("Open ticket")
    resolved_ticket = service.create_ticket("Resolved ticket")
    
    # Assign both to alice
    service.assign_ticket(open_ticket.id, "alice", current_hour=1)
    service.assign_ticket(resolved_ticket.id, "alice", current_hour=2)
    
    # Resolve one ticket
    service.update_status(resolved_ticket.id, "resolved", current_hour=3)
    
    # Filter by alice should only return open ticket
    alice_queue = service.queue_snapshot(assignee="alice")
    assert len(alice_queue) == 1
    assert alice_queue[0].id == open_ticket.id
    
    # Unfiltered queue should also only return open ticket
    all_queue = service.queue_snapshot()
    assert len(all_queue) == 1
    assert all_queue[0].id == open_ticket.id


def test_queue_snapshot_assignee_filtering_handles_unassigned_tickets() -> None:
    service = HelpdeskService()
    
    assigned_ticket = service.create_ticket("Assigned ticket")
    service.create_ticket("Unassigned ticket")
    
    service.assign_ticket(assigned_ticket.id, "alice", current_hour=1)
    # unassigned_ticket remains with assignee=None
    
    # Filter by alice should only return assigned ticket
    alice_queue = service.queue_snapshot(assignee="alice")
    assert len(alice_queue) == 1
    assert alice_queue[0].id == assigned_ticket.id
    
    # Filter by non-existent assignee should return empty
    empty_queue = service.queue_snapshot(assignee="bob")
    assert len(empty_queue) == 0
    
    # Unfiltered should return both tickets
    all_queue = service.queue_snapshot()
    assert len(all_queue) == 2


# ---------------------------------------------------------------------------
# API Tests — POST /tickets
# ---------------------------------------------------------------------------
api_client = TestClient(app)


def test_health_endpoint() -> None:
    response = api_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_create_ticket_returns_201_with_defaults() -> None:
    response = api_client.post("/tickets", json={"title": "Login page broken"})
    assert response.status_code == 201
    payload = response.json()
    assert payload["title"] == "Login page broken"
    assert payload["priority"] == "normal"
    assert payload["customer_tier"] == "standard"
    assert payload["status"] == "open"
    assert payload["assignee"] is None
    assert payload["is_open"] is True
    assert payload["id"].startswith("T-")


def test_create_ticket_with_all_fields() -> None:
    response = api_client.post(
        "/tickets",
        json={
            "title": "Critical outage",
            "priority": "urgent",
            "customer_tier": "enterprise",
            "created_hour": 10,
        },
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["priority"] == "urgent"
    assert payload["customer_tier"] == "enterprise"
    assert payload["created_hour"] == 10


def test_create_ticket_rejects_empty_title() -> None:
    response = api_client.post("/tickets", json={"title": ""})
    assert response.status_code == 400
    assert "must not be empty" in response.json()["detail"]


def test_create_ticket_rejects_whitespace_only_title() -> None:
    response = api_client.post("/tickets", json={"title": "   "})
    assert response.status_code == 400
    assert "must not be empty" in response.json()["detail"]


def test_create_ticket_rejects_invalid_priority() -> None:
    response = api_client.post(
        "/tickets",
        json={"title": "Bad priority", "priority": "critical"},
    )
    assert response.status_code == 422


def test_create_ticket_rejects_invalid_customer_tier() -> None:
    response = api_client.post(
        "/tickets",
        json={"title": "Bad tier", "customer_tier": "vip"},
    )
    assert response.status_code == 422


def test_create_ticket_strips_title_whitespace() -> None:
    response = api_client.post("/tickets", json={"title": "  Padded title  "})
    assert response.status_code == 201
    assert response.json()["title"] == "Padded title"
