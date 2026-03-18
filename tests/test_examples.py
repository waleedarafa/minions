from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi.testclient import TestClient


def _wait_for_result(client: TestClient, task_id: str, timeout: float = 2.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get(f"/api/tasks/{task_id}/result")
        if response.status_code == 200:
            return response.json()
        time.sleep(0.02)
    raise AssertionError(f"Timed out waiting for result for task {task_id}")


def _read_task_payload(path: str) -> dict:
    return json.loads(Path(path).read_text())


def _run_split_process_flow(monkeypatch, tmp_path, payload: dict) -> tuple[str, dict]:
    from src.api import routes
    from src.api.server import create_app
    from src.blueprint.engine import BlueprintRunResult, RunStatus
    from src.blueprint.nodes.deterministic import NodeContext
    from src.orchestration.task_runner import TaskRunner

    state_path = tmp_path / "api-state.db"
    metrics_path = tmp_path / "metrics.json"
    monkeypatch.setenv("MINIONS_API_STATE_PATH", str(state_path))
    monkeypatch.setenv("MINIONS_API_STATE_BACKEND", "sqlite")
    monkeypatch.setenv("MINIONS_METRICS_PATH", str(metrics_path))

    async def fake_run(self, request):
        if getattr(self, "_status_callback", None) is not None:
            self._status_callback("sandbox=docker-pooled (production, network=off)")
            self._status_callback("context hydrated")
            self._status_callback("opened pull request: https://example.test/pr/123")
        context = NodeContext(
            task_description=request.description,
            repo_path=request.repo,
            branch_base=request.branch_base,
        )
        context.extra["hydration_trace"] = [
            {"resolver": "github_link", "section": "linked_context", "item_count": 1}
        ]
        context.extra["rule_trace"] = [
            {"source": ".minion/rules.yaml", "scope": "**", "global": True},
            {
                "source": "request.rules_override",
                "scope": "request",
                "global": False,
                "override_count": len(request.rules_override),
            },
        ]
        if "helpdesk_portal" in request.repo:
            rule_target = f"{request.repo}/src/helpdesk_portal/service.py"
        elif "inventory_api" in request.repo:
            rule_target = f"{request.repo}/src/inventory_api/app.py"
        else:
            rule_target = f"{request.repo}/src/api/routes.py"
        context.extra["rule_scope_targets"] = [rule_target]
        context.extra["ci_run_id"] = "ci-123"
        context.extra["ci_conclusion"] = "success"
        context.extra["ci_summary"] = "CI run ci-123 job summary:\n- unit-tests: success"
        context.extra["sandbox_mode"] = "production"
        context.extra["sandbox_backend"] = "docker-pooled"
        context.extra["sandbox_network_enabled"] = False
        context.extra["sandbox_host_fallback"] = False
        context.extra["security_events"] = []
        context.ci_rounds = 1
        context.pr_url = "https://example.test/pr/123"
        return BlueprintRunResult(
            run_id="run-example",
            blueprint_name=request.blueprint,
            status=RunStatus.completed,
            context=context,
            pr_url="https://example.test/pr/123",
            total_tokens=321,
            duration_seconds=0.25,
        )

    monkeypatch.setattr(TaskRunner, "run", fake_run)

    routes.configure_runtime_state(str(state_path), str(metrics_path))
    routes._tasks.clear()
    routes._run_statuses.clear()
    routes._run_results.clear()
    routes._run_logs.clear()
    routes._background_tasks.clear()

    monkeypatch.setenv("MINIONS_SERVER_ROLE", "api")
    with TestClient(create_app()) as api_client:
        health = api_client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["server_role"] == "api"
        assert health.json()["dispatcher_enabled"] is False

        create_response = api_client.post("/api/tasks", json=payload)
        assert create_response.status_code == 201
        task_id = create_response.json()["id"]

        status = api_client.get(f"/api/tasks/{task_id}/status")
        assert status.status_code == 200
        assert status.json()["status"] == "pending"

        result = api_client.get(f"/api/tasks/{task_id}/result")
        assert result.status_code == 404

    monkeypatch.setenv("MINIONS_SERVER_ROLE", "worker")
    monkeypatch.setenv("MINIONS_WORKER_ID", "worker-example")
    with TestClient(create_app()) as worker_client:
        worker_health = worker_client.get("/api/worker/health")
        assert worker_health.status_code == 200
        assert worker_health.json()["server_role"] == "worker"
        assert worker_health.json()["dispatcher_enabled"] is True
        assert worker_health.json()["dispatcher_running"] is True

        result = _wait_for_result(worker_client, task_id)
        assert result["success"] is True
        assert result["pr_url"] == "https://example.test/pr/123"
        assert result["ci_conclusion"] == "success"
        assert result["sandbox_backend"] == "docker-pooled"

    monkeypatch.setenv("MINIONS_SERVER_ROLE", "api")
    with TestClient(create_app()) as api_client:
        task = api_client.get(f"/api/tasks/{task_id}")
        status = api_client.get(f"/api/tasks/{task_id}/status")
        result = api_client.get(f"/api/tasks/{task_id}/result")
        logs = api_client.get(f"/api/tasks/{task_id}/logs/history")
        runs = api_client.get("/api/runs?priority=high&source=api")
        summary = api_client.get("/api/runs/summary")

        assert task.status_code == 200
        assert task.json()["status"] == "completed"

        assert status.status_code == 200
        assert status.json()["status"] == "completed"

        assert result.status_code == 200
        result_payload = result.json()
        assert result_payload["hydration_trace"] == [
            {"resolver": "github_link", "section": "linked_context", "item_count": 1}
        ]
        assert result_payload["rule_trace"][0]["source"] == ".minion/rules.yaml"
        assert result_payload["ci_summary"].startswith("CI run ci-123")

        assert logs.status_code == 200
        assert any("sandbox=docker-pooled" in line for line in logs.json()["lines"])

        assert runs.status_code == 200
        assert len(runs.json()) == 1
        assert runs.json()[0]["task_id"] == task_id

        assert summary.status_code == 200
        assert summary.json()["total_runs"] >= 1
        assert summary.json()["priorities"]["high"] >= 1

    return task_id, result_payload


def test_trigger_to_deployment_split_process_flow(monkeypatch, tmp_path):
    task_id, result_payload = _run_split_process_flow(
        monkeypatch,
        tmp_path,
        {
            "description": "Add validation to src/api/routes.py and update tests",
            "repo": "example/repo",
            "blueprint": "default",
            "branch_base": "main",
            "priority": "high",
            "context_links": [
                "https://github.com/example/repo/blob/main/src/api/routes.py"
            ],
            "rules_override": [
                "Prefer minimal diffs and preserve API compatibility."
            ],
            "tools": [
                "read_file",
                "edit_file",
                "grep_search",
                "glob_search",
                "run_command",
            ],
        },
    )
    assert task_id
    assert result_payload["sandbox_backend"] == "docker-pooled"


def test_helpdesk_feature_task_payload_drives_framework_flow(monkeypatch, tmp_path):
    payload = _read_task_payload(
        "examples/helpdesk_portal/tasks/bulk_reassignment_task.json"
    )
    task_id, result_payload = _run_split_process_flow(monkeypatch, tmp_path, payload)
    assert task_id
    assert result_payload["rule_scope_targets"] == [
        "examples/helpdesk_portal/src/helpdesk_portal/service.py"
    ]


def test_inventory_feature_task_payload_drives_framework_flow(monkeypatch, tmp_path):
    payload = _read_task_payload(
        "examples/inventory_api/tasks/bulk_restock_task.json"
    )
    task_id, result_payload = _run_split_process_flow(monkeypatch, tmp_path, payload)
    assert task_id
    assert result_payload["rule_scope_targets"] == [
        "examples/inventory_api/src/inventory_api/app.py"
    ]
