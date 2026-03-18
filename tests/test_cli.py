from __future__ import annotations

from typer.testing import CliRunner

from src.cli.main import YOLO_MAX_ITERATIONS, _resolve_max_iterations_override, app
from src.orchestration.task_runner import get_blueprint

runner = CliRunner()


def test_resolve_max_iterations_override_uses_blueprint_defaults_by_default():
    assert _resolve_max_iterations_override(None, yolo=False) is None


def test_resolve_max_iterations_override_respects_explicit_value():
    assert _resolve_max_iterations_override(80, yolo=False) == 80


def test_resolve_max_iterations_override_enables_yolo_floor():
    assert _resolve_max_iterations_override(None, yolo=True) == YOLO_MAX_ITERATIONS
    assert _resolve_max_iterations_override(20, yolo=True) == YOLO_MAX_ITERATIONS
    assert _resolve_max_iterations_override(400, yolo=True) == 400


def test_default_blueprint_is_registered():
    blueprint = get_blueprint("default")
    assert blueprint is not None


def test_serve_api_uses_api_role(monkeypatch):
    captured = {}

    def fake_start(host: str, port: int, role: str | None = None) -> None:
        captured["host"] = host
        captured["port"] = port
        captured["role"] = role

    monkeypatch.setattr("src.api.server.start", fake_start)

    result = runner.invoke(app, ["serve-api", "--host", "127.0.0.1", "--port", "9001"])

    assert result.exit_code == 0
    assert captured == {"host": "127.0.0.1", "port": 9001, "role": "api"}


def test_worker_command_uses_worker_role(monkeypatch):
    captured = {}

    def fake_start(host: str, port: int, role: str | None = None) -> None:
        captured["host"] = host
        captured["port"] = port
        captured["role"] = role

    monkeypatch.setattr("src.api.server.start", fake_start)

    result = runner.invoke(app, ["worker", "--host", "127.0.0.1", "--port", "9002"])

    assert result.exit_code == 0
    assert captured == {"host": "127.0.0.1", "port": 9002, "role": "worker"}
