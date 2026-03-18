from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient


def _wait_for_result(client: TestClient, task_id: str, timeout: float = 1.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get(f"/api/tasks/{task_id}/result")
        if response.status_code == 200:
            return response.json()
        time.sleep(0.02)
    raise AssertionError(f"Timed out waiting for result for task {task_id}")


@pytest.fixture
def client(monkeypatch, tmp_path):
    from src.api.server import create_app
    from src.api import routes
    from src.blueprint.engine import BlueprintRunResult, RunStatus
    from src.blueprint.nodes.deterministic import NodeContext
    from src.orchestration.task_runner import TaskRunner

    state_path = tmp_path / "api-state.json"
    metrics_path = tmp_path / "metrics.json"
    routes.configure_runtime_state(str(state_path), str(metrics_path))
    routes._tasks.clear()
    routes._run_statuses.clear()
    routes._run_results.clear()
    routes._run_logs.clear()
    routes._background_tasks.clear()

    async def fake_run(self, request):
        if getattr(self, "_status_callback", None) is not None:
            self._status_callback("sandbox=docker-pooled (production, network=off)")
        context = NodeContext(task_description=request.description, repo_path=request.repo)
        context.extra["hydration_trace"] = [
            {"resolver": "context_links", "section": "linked_context", "item_count": 1}
        ]
        context.extra["rule_trace"] = [
            {"source": ".minion/rules.yaml", "scope": "**", "global": True}
        ]
        context.extra["rule_scope_targets"] = [f"{request.repo}/src/app.py"]
        context.ci_rounds = 2
        context.extra["ci_run_id"] = "321"
        context.extra["ci_conclusion"] = "success"
        context.extra["ci_summary"] = "CI run 321 job summary:\n- unit-tests: success"
        context.extra["sandbox_mode"] = "production"
        context.extra["sandbox_backend"] = "docker-pooled"
        context.extra["sandbox_network_enabled"] = False
        context.extra["sandbox_host_fallback"] = False
        context.extra["security_events"] = [
            {"type": "blocked_tool_call", "tool_name": "run_command", "arguments": {"command": "rm -rf /tmp/x"}}
        ]
        return BlueprintRunResult(
            run_id="run-1",
            blueprint_name=request.blueprint,
            status=RunStatus.completed,
            context=context,
        )

    monkeypatch.setattr(TaskRunner, "run", fake_run)
    app = create_app()
    with TestClient(app) as client:
        yield client


def test_server_lifespan_cleans_up_shared_sandbox_managers(monkeypatch):
    from src.api.server import create_app

    cleaned = {"called": False}

    async def fake_cleanup() -> None:
        cleaned["called"] = True

    monkeypatch.setattr("src.api.server.cleanup_shared_sandbox_managers", fake_cleanup)

    with TestClient(create_app()) as client:
        response = client.get("/api/health")
        assert response.status_code == 200

    assert cleaned["called"] is True


def test_health_check(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["server_role"] == "all"
    assert data["dispatcher_enabled"] is True
    assert data["dispatcher_running"] is True
    assert data["worker_id"]


def test_worker_health_check(client):
    response = client.get("/api/worker/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["dispatcher_enabled"] is True
    assert data["dispatcher_running"] is True
    assert "active_leases" in data
    assert "leased_task_ids" in data


def test_create_task(client):
    response = client.post("/api/tasks", json={
        "description": "Fix the bug in payments",
        "repo": "test/repo",
        "priority": "high",
    })
    assert response.status_code == 201
    data = response.json()
    assert "id" in data
    assert data["status"] == "pending"
    assert data["description"] == "Fix the bug in payments"
    assert data["priority"] == "high"
    assert data["source"] == "api"


def test_list_tasks(client):
    # Create a task first
    client.post("/api/tasks", json={
        "description": "Task 1",
        "repo": "test/repo",
    })
    response = client.get("/api/tasks")
    assert response.status_code == 200
    tasks = response.json()
    assert isinstance(tasks, list)
    assert len(tasks) >= 1


def test_get_task(client):
    create_resp = client.post("/api/tasks", json={
        "description": "Specific task",
        "repo": "test/repo",
    })
    task_id = create_resp.json()["id"]

    response = client.get(f"/api/tasks/{task_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == task_id


def test_get_nonexistent_task(client):
    response = client.get("/api/tasks/nonexistent-id")
    assert response.status_code == 404


def test_list_blueprints(client):
    response = client.get("/api/blueprints")
    assert response.status_code == 200
    data = response.json()
    assert any(bp["name"] == "default" for bp in data)


def test_create_task_rejects_unknown_blueprint(client):
    response = client.post("/api/tasks", json={
        "description": "Unknown blueprint task",
        "repo": "test/repo",
        "blueprint": "does-not-exist",
    })
    assert response.status_code == 400


def test_task_status_exposes_operational_metadata(client):
    create_response = client.post("/api/tasks", json={
        "description": "Track metadata",
        "repo": "test/repo",
        "priority": "high",
    })
    task_id = create_response.json()["id"]
    _wait_for_result(client, task_id)

    response = client.get(f"/api/tasks/{task_id}/status")
    assert response.status_code == 200
    data = response.json()
    assert data["source"] == "api"
    assert data["priority"] == "high"
    assert data["created_by"] == ""
    assert data["hydration_trace"] == [
        {"resolver": "context_links", "section": "linked_context", "item_count": 1}
    ]
    assert data["rule_trace"] == [
        {"source": ".minion/rules.yaml", "scope": "**", "global": True}
    ]
    assert data["rule_scope_targets"] == ["test/repo/src/app.py"]
    assert data["ci_rounds"] == 2
    assert data["ci_run_id"] == "321"
    assert data["ci_conclusion"] == "success"
    assert "unit-tests" in data["ci_summary"]
    assert data["sandbox_mode"] == "production"
    assert data["sandbox_backend"] == "docker-pooled"
    assert data["sandbox_network_enabled"] is False
    assert data["sandbox_host_fallback"] is False
    assert data["security_events"] == [
        {"type": "blocked_tool_call", "tool_name": "run_command", "arguments": {"command": "rm -rf /tmp/x"}}
    ]


def test_task_result_exposes_hydration_trace(client):
    create_response = client.post("/api/tasks", json={
        "description": "Track result trace",
        "repo": "test/repo",
    })
    task_id = create_response.json()["id"]

    data = _wait_for_result(client, task_id)
    assert data["hydration_trace"] == [
        {"resolver": "context_links", "section": "linked_context", "item_count": 1}
    ]
    assert data["rule_trace"] == [
        {"source": ".minion/rules.yaml", "scope": "**", "global": True}
    ]
    assert data["rule_scope_targets"] == ["test/repo/src/app.py"]
    assert data["ci_rounds"] == 2
    assert data["ci_run_id"] == "321"
    assert data["ci_conclusion"] == "success"
    assert "unit-tests" in data["ci_summary"]
    assert data["sandbox_mode"] == "production"
    assert data["sandbox_backend"] == "docker-pooled"
    assert data["sandbox_network_enabled"] is False
    assert data["sandbox_host_fallback"] is False
    assert data["security_events"] == [
        {"type": "blocked_tool_call", "tool_name": "run_command", "arguments": {"command": "rm -rf /tmp/x"}}
    ]


def test_list_runs_supports_history_and_search(client):
    response = client.post("/api/tasks", json={
        "description": "Persisted payments fix",
        "repo": "test/repo",
        "priority": "high",
    })
    task_id = response.json()["id"]
    _wait_for_result(client, task_id)

    response = client.get("/api/runs?q=payments")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["success"] is True
    assert data[0]["source"] == "api"
    assert data[0]["priority"] == "high"

    filtered = client.get("/api/runs?priority=high&source=api&ci_conclusion=success")
    assert filtered.status_code == 200
    filtered_data = filtered.json()
    assert len(filtered_data) == 1
    assert filtered_data[0]["task_id"] == task_id


def test_task_log_history_returns_persisted_lines(client):
    response = client.post("/api/tasks", json={
        "description": "Log visibility task",
        "repo": "test/repo",
    })
    task_id = response.json()["id"]
    _wait_for_result(client, task_id)

    log_response = client.get(f"/api/tasks/{task_id}/logs/history")
    assert log_response.status_code == 200
    payload = log_response.json()
    assert payload["task_id"] == task_id
    assert isinstance(payload["lines"], list)


def test_dashboard_stats_include_queue_and_failure_breakdown(client):
    response = client.post("/api/tasks", json={
        "description": "Dashboard task",
        "repo": "test/repo",
        "priority": "high",
    })
    task_id = response.json()["id"]
    _wait_for_result(client, task_id)

    dashboard = client.get("/dashboard/api/stats")
    assert dashboard.status_code == 200
    data = dashboard.json()
    assert "queued_tasks" in data
    assert "failed_tasks" in data
    assert data["priorities"]["high"] >= 1
    assert data["ci_conclusions"]["success"] >= 1
    assert data["worker"]["dispatcher_enabled"] is True


def test_run_summary_exposes_aggregates_and_recent_failures(client):
    response = client.post("/api/tasks", json={
        "description": "Summary task",
        "repo": "test/repo",
        "priority": "high",
    })
    task_id = response.json()["id"]
    _wait_for_result(client, task_id)

    summary = client.get("/api/runs/summary")
    assert summary.status_code == 200
    data = summary.json()
    assert data["total_runs"] >= 1
    assert data["priorities"]["high"] >= 1
    assert data["sources"]["api"] >= 1
    assert "completed" in data["statuses"] or "failed" in data["statuses"]
    assert "avg_duration" in data
    assert "avg_tokens" in data


def test_global_log_search_finds_persisted_lines(client):
    response = client.post("/api/tasks", json={
        "description": "Searchable logs task",
        "repo": "test/repo",
    })
    task_id = response.json()["id"]
    _wait_for_result(client, task_id)

    search = client.get("/api/logs/search?q=sandbox")
    assert search.status_code == 200
    payload = search.json()
    assert payload["count"] >= 1
    assert any(match["task_id"] == task_id for match in payload["matches"])


def test_runtime_state_survives_restart(monkeypatch, tmp_path):
    from src.api.server import create_app
    from src.api import routes
    from src.blueprint.engine import BlueprintRunResult, RunStatus
    from src.blueprint.nodes.deterministic import NodeContext
    from src.orchestration.task_runner import TaskRunner

    state_path = tmp_path / "api-state.json"
    metrics_path = tmp_path / "metrics.json"
    monkeypatch.setenv("MINIONS_API_STATE_PATH", str(state_path))
    monkeypatch.setenv("MINIONS_METRICS_PATH", str(metrics_path))

    async def fake_run(self, request):
        context = NodeContext(task_description=request.description, repo_path=request.repo)
        return BlueprintRunResult(
            run_id="run-restart",
            blueprint_name=request.blueprint,
            status=RunStatus.completed,
            context=context,
        )

    monkeypatch.setattr(TaskRunner, "run", fake_run)
    routes.configure_runtime_state(str(state_path), str(metrics_path))

    with TestClient(create_app()) as client:
        response = client.post("/api/tasks", json={
            "description": "Restart-safe task",
            "repo": "test/repo",
        })
        assert response.status_code == 201
        task_id = response.json()["id"]
        _wait_for_result(client, task_id)

    routes.configure_runtime_state(str(state_path), str(metrics_path))
    routes._tasks.clear()
    routes._run_statuses.clear()
    routes._run_results.clear()
    routes._run_logs.clear()
    routes._background_tasks.clear()

    with TestClient(create_app()) as client:
        task_response = client.get(f"/api/tasks/{task_id}")
        assert task_response.status_code == 200
        assert task_response.json()["description"] == "Restart-safe task"
        status_response = client.get(f"/api/tasks/{task_id}/status")
        assert status_response.status_code == 200
        result_response = client.get(f"/api/tasks/{task_id}/result")
        assert result_response.status_code == 200
        assert result_response.json()["task_id"] == task_id
        assert result_response.json()["run_id"] == status_response.json()["run_id"]


def test_task_dispatcher_prioritizes_high_priority(monkeypatch, tmp_path):
    from src.api.server import create_app
    from src.api import routes
    from src.blueprint.engine import BlueprintRunResult, RunStatus
    from src.blueprint.nodes.deterministic import NodeContext
    from src.orchestration.task_runner import TaskRunner

    state_path = tmp_path / "api-state.json"
    metrics_path = tmp_path / "metrics.json"
    monkeypatch.setenv("MINIONS_API_STATE_PATH", str(state_path))
    monkeypatch.setenv("MINIONS_METRICS_PATH", str(metrics_path))
    monkeypatch.setenv("MINIONS_MAX_CONCURRENT_TASKS", "1")

    started: list[str] = []

    async def fake_run(self, request):
        started.append(request.description)
        await __import__("asyncio").sleep(0.05)
        context = NodeContext(task_description=request.description, repo_path=request.repo)
        return BlueprintRunResult(
            run_id=f"run-{request.description}",
            blueprint_name=request.blueprint,
            status=RunStatus.completed,
            context=context,
        )

    monkeypatch.setattr(TaskRunner, "run", fake_run)
    routes.configure_runtime_state(str(state_path), str(metrics_path))
    routes._tasks.clear()
    routes._run_statuses.clear()
    routes._run_results.clear()
    routes._run_logs.clear()
    routes._background_tasks.clear()

    with TestClient(create_app()) as client:
        low = client.post("/api/tasks", json={
            "description": "low-priority task",
            "repo": "test/repo",
            "priority": "low",
        })
        high = client.post("/api/tasks", json={
            "description": "high-priority task",
            "repo": "test/repo",
            "priority": "high",
        })
        low_id = low.json()["id"]
        high_id = high.json()["id"]
        _wait_for_result(client, high_id, timeout=2.0)
        _wait_for_result(client, low_id, timeout=2.0)

    assert started[0] == "high-priority task"


def test_running_task_exposes_lease_metadata(monkeypatch, tmp_path):
    import asyncio

    from src.api.server import create_app
    from src.api import routes
    from src.blueprint.engine import BlueprintRunResult, RunStatus
    from src.blueprint.nodes.deterministic import NodeContext
    from src.orchestration.task_runner import TaskRunner

    state_path = tmp_path / "api-state.json"
    metrics_path = tmp_path / "metrics.json"
    monkeypatch.setenv("MINIONS_API_STATE_PATH", str(state_path))
    monkeypatch.setenv("MINIONS_METRICS_PATH", str(metrics_path))
    monkeypatch.setenv("MINIONS_WORKER_ID", "worker-test")
    monkeypatch.setenv("MINIONS_TASK_LEASE_SECONDS", "120")

    gate = asyncio.Event()

    async def fake_run(self, request):
        await gate.wait()
        context = NodeContext(task_description=request.description, repo_path=request.repo)
        return BlueprintRunResult(
            run_id="run-lease",
            blueprint_name=request.blueprint,
            status=RunStatus.completed,
            context=context,
        )

    monkeypatch.setattr(TaskRunner, "run", fake_run)
    routes.configure_runtime_state(str(state_path), str(metrics_path))
    routes._tasks.clear()
    routes._run_statuses.clear()
    routes._run_results.clear()
    routes._run_logs.clear()
    routes._background_tasks.clear()

    with TestClient(create_app()) as client:
        response = client.post("/api/tasks", json={
            "description": "Lease visibility task",
            "repo": "test/repo",
        })
        task_id = response.json()["id"]

        deadline = time.time() + 2.0
        leased = None
        while time.time() < deadline:
            status_response = client.get(f"/api/tasks/{task_id}/status")
            data = status_response.json()
            if data["status"] == "running" and data["lease_owner"] == "worker-test":
                leased = data
                break
            time.sleep(0.02)

        assert leased is not None
        assert leased["lease_expires_at"] is not None

        gate.set()
        _wait_for_result(client, task_id, timeout=2.0)


def test_running_task_renews_lease(monkeypatch, tmp_path):
    import asyncio

    from src.api.server import create_app
    from src.api import routes
    from src.blueprint.engine import BlueprintRunResult, RunStatus
    from src.blueprint.nodes.deterministic import NodeContext
    from src.orchestration.task_runner import TaskRunner

    state_path = tmp_path / "api-state.json"
    metrics_path = tmp_path / "metrics.json"
    monkeypatch.setenv("MINIONS_API_STATE_PATH", str(state_path))
    monkeypatch.setenv("MINIONS_METRICS_PATH", str(metrics_path))
    monkeypatch.setenv("MINIONS_WORKER_ID", "worker-renew")
    monkeypatch.setenv("MINIONS_TASK_LEASE_SECONDS", "30")
    monkeypatch.setenv("MINIONS_LEASE_RENEW_INTERVAL_SECONDS", "1")

    gate = asyncio.Event()

    async def fake_run(self, request):
        await gate.wait()
        context = NodeContext(task_description=request.description, repo_path=request.repo)
        return BlueprintRunResult(
            run_id="run-renew",
            blueprint_name=request.blueprint,
            status=RunStatus.completed,
            context=context,
        )

    monkeypatch.setattr(TaskRunner, "run", fake_run)
    routes.configure_runtime_state(str(state_path), str(metrics_path))
    routes._tasks.clear()
    routes._run_statuses.clear()
    routes._run_results.clear()
    routes._run_logs.clear()
    routes._background_tasks.clear()

    with TestClient(create_app()) as client:
        response = client.post("/api/tasks", json={
            "description": "Lease renewal task",
            "repo": "test/repo",
        })
        task_id = response.json()["id"]

        first_expiry = None
        renewed_expiry = None
        deadline = time.time() + 2.0
        while time.time() < deadline:
            status = client.get(f"/api/tasks/{task_id}/status").json()
            if status["status"] == "running" and status["lease_expires_at"]:
                if first_expiry is None:
                    first_expiry = status["lease_expires_at"]
                elif status["lease_expires_at"] != first_expiry:
                    renewed_expiry = status["lease_expires_at"]
                    break
            time.sleep(0.05)

        assert first_expiry is not None
        assert renewed_expiry is not None

        gate.set()
        _wait_for_result(client, task_id, timeout=2.0)


def test_state_store_requeues_expired_leases(tmp_path):
    from src.api.models import RunStatus, Task
    from src.api.state import ApiStateSnapshot, ApiStateStore

    state_path = tmp_path / "api-state.json"
    store = ApiStateStore(str(state_path))
    task = Task(description="expired lease", repo="test/repo", status="running")
    status = RunStatus(
        task_id=task.id,
        run_id="run-expired",
        status="running",
        current_step="implement",
        steps_completed=1,
        total_steps=3,
        started_at=datetime.now(timezone.utc),
        lease_owner="worker-a",
        lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    store.persist(ApiStateSnapshot(
        tasks={task.id: task},
        run_statuses={task.id: status},
        run_results={},
        run_logs={},
    ))

    requeued = store.requeue_expired_leases()
    snapshot = store.load(recover_inflight=False)

    assert requeued == [task.id]
    assert snapshot.tasks[task.id].status == "pending"
    assert snapshot.run_statuses[task.id].status == "pending"
    assert snapshot.run_statuses[task.id].lease_owner is None
    assert snapshot.run_statuses[task.id].lease_expires_at is None
    assert any("re-queued" in line for line in snapshot.run_logs[task.id])


def test_sqlite_state_store_persists_and_claims(tmp_path):
    from src.api.models import RunStatus, Task
    from src.api.state import ApiStateSnapshot, ApiStateStore

    state_path = tmp_path / "api-state.db"
    store = ApiStateStore(str(state_path))
    task = Task(description="sqlite task", repo="test/repo", status="pending", priority="high")
    status = RunStatus(
        task_id=task.id,
        run_id="run-sqlite",
        status="pending",
        current_step="queued",
        steps_completed=0,
        total_steps=0,
        started_at=datetime.now(timezone.utc),
    )
    store.persist(ApiStateSnapshot(
        tasks={task.id: task},
        run_statuses={task.id: status},
        run_results={},
        run_logs={task.id: ["queued"]},
    ))

    claimed = store.claim_pending_tasks(worker_id="sqlite-worker", max_count=1, lease_seconds=60)
    snapshot = store.load(recover_inflight=False)

    assert claimed == [task.id]
    assert snapshot.tasks[task.id].status == "running"
    assert snapshot.run_statuses[task.id].lease_owner == "sqlite-worker"
    assert snapshot.run_logs[task.id] == ["queued"]

    renewed = store.renew_task_lease(task_id=task.id, worker_id="sqlite-worker", lease_seconds=120)
    renewed_snapshot = store.load(recover_inflight=False)
    assert renewed is True
    assert renewed_snapshot.run_statuses[task.id].lease_expires_at is not None

    renewed_snapshot.run_statuses[task.id].lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    store.persist(renewed_snapshot)
    requeued = store.requeue_expired_leases()
    final_snapshot = store.load(recover_inflight=False)
    assert requeued == [task.id]
    assert final_snapshot.tasks[task.id].status == "pending"


def test_api_role_disables_dispatcher(monkeypatch, tmp_path):
    from src.api.server import create_app
    from src.api import routes

    state_path = tmp_path / "api-state.json"
    metrics_path = tmp_path / "metrics.json"
    monkeypatch.setenv("MINIONS_API_STATE_PATH", str(state_path))
    monkeypatch.setenv("MINIONS_METRICS_PATH", str(metrics_path))
    monkeypatch.setenv("MINIONS_SERVER_ROLE", "api")

    routes.configure_runtime_state(str(state_path), str(metrics_path))
    routes._tasks.clear()
    routes._run_statuses.clear()
    routes._run_results.clear()
    routes._run_logs.clear()
    routes._background_tasks.clear()

    with TestClient(create_app()) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        health_data = health.json()
        assert health_data["server_role"] == "api"
        assert health_data["dispatcher_enabled"] is False
        assert health_data["dispatcher_running"] is False

        created = client.post("/api/tasks", json={
            "description": "api-only queued task",
            "repo": "test/repo",
        })
        task_id = created.json()["id"]

        task = client.get(f"/api/tasks/{task_id}")
        status = client.get(f"/api/tasks/{task_id}/status")
        result = client.get(f"/api/tasks/{task_id}/result")

        assert task.status_code == 200
        assert task.json()["status"] == "pending"
        assert status.status_code == 200
        assert status.json()["status"] == "pending"
        assert result.status_code == 404
