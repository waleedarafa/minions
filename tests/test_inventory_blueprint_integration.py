"""Integration tests for the inventory_api example against the default blueprint.

These tests exercise the full BlueprintEngine step-execution logic using the
real default.yaml blueprint definition.  All external side-effects (LLM, git,
subprocess, CI, PR creation) are mocked at precise seams so the engine's
condition-checking and step-sequencing logic runs for real.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agent.runtime import AgentResult, AgentRuntime
from src.agent.tools import ToolRegistry
from src.blueprint.engine import BlueprintEngine, RunStatus
from src.blueprint.nodes.deterministic import NodeContext
from src.blueprint.schema import Blueprint
from src.feedback.ci import CIResult
from src.feedback.lint import LintResult

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_BLUEPRINT_PATH = str(_PROJECT_ROOT / "blueprints" / "default.yaml")
_TASK_PAYLOAD_PATH = str(
    _PROJECT_ROOT / "examples" / "inventory_api" / "tasks" / "bulk_restock_task.json"
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_task_payload() -> dict:
    return json.loads(Path(_TASK_PAYLOAD_PATH).read_text())


def _load_blueprint() -> Blueprint:
    return Blueprint.from_file(_BLUEPRINT_PATH)


def _make_agent_result(output: str = "done") -> AgentResult:
    return AgentResult(
        success=True,
        output=output,
        tool_calls_made=[],
        tokens_used={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
        iterations=1,
    )


def _make_runtime() -> MagicMock:
    runtime = MagicMock(spec=AgentRuntime)
    runtime.run = AsyncMock(return_value=_make_agent_result())
    runtime._tools = ToolRegistry()
    runtime.tool_registry = runtime._tools
    return runtime


def _make_context(tmp_path: Path, payload: dict | None = None) -> NodeContext:
    payload = payload or _load_task_payload()
    # Create a dummy test file so _run_tests discovers something
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "test_app.py").write_text("def test_placeholder(): pass\n")
    return NodeContext(
        task_description=payload["description"],
        repo_path=str(tmp_path),
        branch_base=payload.get("branch_base", "main"),
        extra={"task_tools": payload.get("tools", [])},
    )


def _step_names(result) -> list[str]:
    return [step.name for step in result.steps]


def _make_git_mock(push_succeeds: bool = True) -> callable:
    """Return a callable that fakes subprocess.run for git commands."""

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["git", "rev-parse", "--show-toplevel"]:
            cwd = kwargs.get("cwd", "/tmp/repo")
            return SimpleNamespace(returncode=0, stdout=f"{cwd}\n", stderr="")
        if cmd[:2] == ["git", "checkout"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["git", "status", "--porcelain"]:
            return SimpleNamespace(returncode=0, stdout="M src/app.py\n", stderr="")
        if cmd[:2] == ["git", "add"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:2] == ["git", "commit"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:2] == ["git", "push"]:
            if push_succeeds:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            return SimpleNamespace(returncode=1, stdout="", stderr="push failed")
        if cmd[:4] == ["git", "remote", "get-url", "origin"]:
            return SimpleNamespace(
                returncode=0,
                stdout="git@github.com:org/inventory-api.git\n",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return fake_run


def _make_lint_mock(fail_first_n: int = 0) -> callable:
    """Return a mock for run_all_linters that fails the first N calls."""
    state = {"calls": 0}

    def mock_linters(repo_path, timeout=60):
        state["calls"] += 1
        if state["calls"] <= fail_first_n:
            return [
                LintResult(
                    passed=False,
                    output="src/app.py:1:1: F401 imported but unused",
                    linter="ruff",
                    error_count=1,
                )
            ]
        return [LintResult(passed=True, output="OK", linter="ruff")]

    return mock_linters


def _make_test_runner_mock(fail_first_n: int = 0) -> callable:
    """Return a mock for run_local that fails the first N pytest calls."""
    state = {"calls": 0}

    def mock_run_local(cmd, cwd, timeout):
        state["calls"] += 1
        if state["calls"] <= fail_first_n:
            return ("FAILED tests/test_app.py::test_ok - AssertionError", 1)
        return ("5 passed in 0.1s", 0)

    return mock_run_local


def _make_ci_result(
    success: bool = True,
    conclusion: str | None = None,
    logs: str = "",
) -> CIResult:
    return CIResult(
        success=success,
        status="completed",
        conclusion=conclusion or ("success" if success else "failure"),
        run_id="ci-42",
        logs=logs,
        summary=f"CI run ci-42 job summary:\n- unit-tests: {'success' if success else 'failure'}",
    )


def _make_ci_mock(results: list[CIResult]) -> callable:
    """Return an async mock for wait_for_ci returning results in order."""
    state = {"calls": 0}

    async def fake_wait(repo, branch, sha=None, poll_interval=30, timeout=1800):
        idx = min(state["calls"], len(results) - 1)
        state["calls"] += 1
        return results[idx]

    return fake_wait


def _make_pr_mock() -> callable:
    """Return an async mock for create_github_pr."""

    async def fake_pr(**kwargs):
        return {"html_url": "https://github.com/org/inventory-api/pull/42"}

    return fake_pr


class _FakeAutofixRegistry:
    """Fake autofix registry that never finds applicable fixes."""

    def __init__(self, applicable: list | None = None, applied: list | None = None):
        self._applicable = applicable or []
        self._applied = applied or []

    def find_applicable(self, logs: str) -> list:
        return self._applicable

    def apply_all(self, failure_text: str, repo_path: str) -> list[str]:
        return self._applied


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def _patch_externals(monkeypatch, tmp_path):
    """Apply base monkeypatches common to all tests.

    Returns a dict of mocks so individual tests can override behavior.
    """
    git_mock = _make_git_mock(push_succeeds=True)
    lint_mock = _make_lint_mock(fail_first_n=0)
    test_mock = _make_test_runner_mock(fail_first_n=0)
    ci_mock = _make_ci_mock([_make_ci_result(success=True)])
    pr_mock = _make_pr_mock()
    autofix_reg = _FakeAutofixRegistry()

    monkeypatch.setattr("src.blueprint.nodes.deterministic.subprocess.run", git_mock)
    monkeypatch.setattr("src.blueprint.nodes.deterministic.run_all_linters", lint_mock)
    monkeypatch.setattr("src.sandbox.runner.get_runner", lambda: None)
    monkeypatch.setattr("src.sandbox.runner.run_local", test_mock)
    monkeypatch.setattr("src.blueprint.nodes.deterministic.wait_for_ci", ci_mock)
    monkeypatch.setattr("src.blueprint.nodes.deterministic.create_github_pr", pr_mock)
    monkeypatch.setattr(
        "src.blueprint.nodes.deterministic.get_autofix_registry", lambda: autofix_reg
    )

    return {
        "git_mock": git_mock,
        "lint_mock": lint_mock,
        "test_mock": test_mock,
        "ci_mock": ci_mock,
        "pr_mock": pr_mock,
        "autofix_reg": autofix_reg,
        "tmp_path": tmp_path,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestInventoryBlueprintHappyPath:
    """Happy path: all local checks pass, CI passes, PR created."""

    @pytest.mark.asyncio
    async def test_executed_steps(self, _patch_externals):
        tmp_path = _patch_externals["tmp_path"]
        runtime = _make_runtime()
        engine = BlueprintEngine(runtime=runtime)
        bp = _load_blueprint()
        ctx = _make_context(tmp_path)

        result = await engine.execute(bp, ctx)

        executed = _step_names(result)
        assert "hydrate_context" in executed
        assert "create_branch" in executed
        assert "plan" in executed
        assert "implement" in executed
        assert "run_linter" in executed
        assert "verify" in executed
        assert "reset_fix_state" in executed
        assert "commit_and_push" in executed
        assert "first_ci_round" in executed
        assert "open_pr" in executed

    @pytest.mark.asyncio
    async def test_conditional_fix_steps_skipped(self, _patch_externals):
        tmp_path = _patch_externals["tmp_path"]
        runtime = _make_runtime()
        engine = BlueprintEngine(runtime=runtime)
        bp = _load_blueprint()
        ctx = _make_context(tmp_path)

        result = await engine.execute(bp, ctx)

        executed = _step_names(result)
        # These should all be skipped because ci_failed is False
        assert "prepare_fix" not in executed
        assert "fix_failures" not in executed
        assert "reset_for_reverify" not in executed
        assert "re_lint" not in executed
        assert "re_verify" not in executed
        # These should be skipped because CI passed
        assert "apply_ci_autofixes" not in executed
        assert "prepare_ci_fix" not in executed
        assert "handle_ci_failures" not in executed
        assert "reset_for_final_verify" not in executed
        assert "final_lint" not in executed
        assert "final_verify" not in executed
        assert "commit_and_push_final" not in executed
        assert "second_ci_round" not in executed

    @pytest.mark.asyncio
    async def test_final_status_completed(self, _patch_externals):
        tmp_path = _patch_externals["tmp_path"]
        runtime = _make_runtime()
        engine = BlueprintEngine(runtime=runtime)
        bp = _load_blueprint()
        ctx = _make_context(tmp_path)

        result = await engine.execute(bp, ctx)

        assert result.status == RunStatus.completed
        assert result.pr_url == "https://github.com/org/inventory-api/pull/42"

    @pytest.mark.asyncio
    async def test_context_state_after_happy_path(self, _patch_externals):
        tmp_path = _patch_externals["tmp_path"]
        runtime = _make_runtime()
        engine = BlueprintEngine(runtime=runtime)
        bp = _load_blueprint()
        ctx = _make_context(tmp_path)

        result = await engine.execute(bp, ctx)

        assert result.context.ci_failed is False
        assert result.context.branch_pushed is True
        assert result.context.ci_rounds == 1
        # fix_attempted reset by reset_fix_state
        assert result.context.fix_attempted is False

    @pytest.mark.asyncio
    async def test_agentic_steps_called_twice(self, _patch_externals):
        """Plan + implement = 2 agentic calls."""
        tmp_path = _patch_externals["tmp_path"]
        runtime = _make_runtime()
        engine = BlueprintEngine(runtime=runtime)
        bp = _load_blueprint()
        ctx = _make_context(tmp_path)

        await engine.execute(bp, ctx)

        assert runtime.run.await_count == 2

    @pytest.mark.asyncio
    async def test_total_tokens_accumulated(self, _patch_externals):
        tmp_path = _patch_externals["tmp_path"]
        runtime = _make_runtime()
        engine = BlueprintEngine(runtime=runtime)
        bp = _load_blueprint()
        ctx = _make_context(tmp_path)

        result = await engine.execute(bp, ctx)

        # Each agentic step contributes 150 tokens, 2 steps = 300
        assert result.total_tokens == 300

    @pytest.mark.asyncio
    async def test_all_steps_completed_successfully(self, _patch_externals):
        tmp_path = _patch_externals["tmp_path"]
        runtime = _make_runtime()
        engine = BlueprintEngine(runtime=runtime)
        bp = _load_blueprint()
        ctx = _make_context(tmp_path)

        result = await engine.execute(bp, ctx)

        for step in result.steps:
            assert step.status == "completed", f"Step {step.name} status: {step.status}"


class TestInventoryBlueprintLocalLintFailureThenFix:
    """Lint fails on first run, fix_failures runs, re-lint passes."""

    @pytest.mark.asyncio
    async def test_fix_path_executed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "src.blueprint.nodes.deterministic.subprocess.run",
            _make_git_mock(push_succeeds=True),
        )
        monkeypatch.setattr(
            "src.blueprint.nodes.deterministic.run_all_linters",
            _make_lint_mock(fail_first_n=1),
        )
        monkeypatch.setattr("src.sandbox.runner.get_runner", lambda: None)
        monkeypatch.setattr(
            "src.sandbox.runner.run_local",
            _make_test_runner_mock(fail_first_n=0),
        )
        monkeypatch.setattr(
            "src.blueprint.nodes.deterministic.wait_for_ci",
            _make_ci_mock([_make_ci_result(success=True)]),
        )
        monkeypatch.setattr(
            "src.blueprint.nodes.deterministic.create_github_pr",
            _make_pr_mock(),
        )
        monkeypatch.setattr(
            "src.blueprint.nodes.deterministic.get_autofix_registry",
            lambda: _FakeAutofixRegistry(),
        )

        runtime = _make_runtime()
        engine = BlueprintEngine(runtime=runtime)
        bp = _load_blueprint()
        ctx = _make_context(tmp_path)

        result = await engine.execute(bp, ctx)
        executed = _step_names(result)

        # Fix path should fire
        assert "prepare_fix" in executed
        assert "fix_failures" in executed
        assert "reset_for_reverify" in executed
        assert "re_lint" in executed
        assert "re_verify" in executed

        # After fix succeeds, rest of pipeline should complete
        assert "reset_fix_state" in executed
        assert "commit_and_push" in executed
        assert "first_ci_round" in executed
        assert "open_pr" in executed

        assert result.status == RunStatus.completed
        # plan + implement + fix_failures = 3 agentic calls
        assert runtime.run.await_count == 3


class TestInventoryBlueprintLocalTestFailureThenFix:
    """Tests fail on first run, fix_failures runs, re-verify passes."""

    @pytest.mark.asyncio
    async def test_test_failure_triggers_fix_path(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "src.blueprint.nodes.deterministic.subprocess.run",
            _make_git_mock(push_succeeds=True),
        )
        monkeypatch.setattr(
            "src.blueprint.nodes.deterministic.run_all_linters",
            _make_lint_mock(fail_first_n=0),
        )
        monkeypatch.setattr("src.sandbox.runner.get_runner", lambda: None)
        # Tests fail once, then pass
        monkeypatch.setattr(
            "src.sandbox.runner.run_local",
            _make_test_runner_mock(fail_first_n=1),
        )
        monkeypatch.setattr(
            "src.blueprint.nodes.deterministic.wait_for_ci",
            _make_ci_mock([_make_ci_result(success=True)]),
        )
        monkeypatch.setattr(
            "src.blueprint.nodes.deterministic.create_github_pr",
            _make_pr_mock(),
        )
        monkeypatch.setattr(
            "src.blueprint.nodes.deterministic.get_autofix_registry",
            lambda: _FakeAutofixRegistry(),
        )

        runtime = _make_runtime()
        engine = BlueprintEngine(runtime=runtime)
        bp = _load_blueprint()
        ctx = _make_context(tmp_path)

        result = await engine.execute(bp, ctx)
        executed = _step_names(result)

        assert "verify" in executed
        assert "prepare_fix" in executed
        assert "fix_failures" in executed
        assert "re_verify" in executed
        assert result.status == RunStatus.completed
        assert result.context.ci_failed is False


class TestInventoryBlueprintCIFailureThenFix:
    """Local checks pass, CI fails, handle_ci_failures runs, second CI passes."""

    @pytest.mark.asyncio
    async def test_ci_failure_triggers_handle_ci_failures(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "src.blueprint.nodes.deterministic.subprocess.run",
            _make_git_mock(push_succeeds=True),
        )
        monkeypatch.setattr(
            "src.blueprint.nodes.deterministic.run_all_linters",
            _make_lint_mock(fail_first_n=0),
        )
        monkeypatch.setattr("src.sandbox.runner.get_runner", lambda: None)
        monkeypatch.setattr(
            "src.sandbox.runner.run_local",
            _make_test_runner_mock(fail_first_n=0),
        )
        # CI: first call fails, second passes
        monkeypatch.setattr(
            "src.blueprint.nodes.deterministic.wait_for_ci",
            _make_ci_mock([
                _make_ci_result(success=False, logs="FAILED tests/test_app.py::test_api"),
                _make_ci_result(success=True),
            ]),
        )
        monkeypatch.setattr(
            "src.blueprint.nodes.deterministic.create_github_pr",
            _make_pr_mock(),
        )
        monkeypatch.setattr(
            "src.blueprint.nodes.deterministic.get_autofix_registry",
            lambda: _FakeAutofixRegistry(),
        )

        runtime = _make_runtime()
        engine = BlueprintEngine(runtime=runtime)
        bp = _load_blueprint()
        ctx = _make_context(tmp_path)

        result = await engine.execute(bp, ctx)
        executed = _step_names(result)

        # First CI round fails → triggers CI fix path
        assert "first_ci_round" in executed
        assert "prepare_ci_fix" in executed
        assert "handle_ci_failures" in executed
        assert "reset_for_final_verify" in executed
        assert "final_lint" in executed
        assert "final_verify" in executed
        assert "commit_and_push_final" in executed
        assert "second_ci_round" in executed
        assert "open_pr" in executed

        assert result.status == RunStatus.completed
        assert result.context.ci_rounds == 2
        assert result.context.ci_failed is False
        # plan + implement + handle_ci_failures = 3
        assert runtime.run.await_count == 3


class TestInventoryBlueprintCIFailurePartial:
    """CI always fails, max rounds reached → partial status."""

    @pytest.mark.asyncio
    async def test_max_ci_rounds_yields_partial_status(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "src.blueprint.nodes.deterministic.subprocess.run",
            _make_git_mock(push_succeeds=True),
        )
        monkeypatch.setattr(
            "src.blueprint.nodes.deterministic.run_all_linters",
            _make_lint_mock(fail_first_n=0),
        )
        monkeypatch.setattr("src.sandbox.runner.get_runner", lambda: None)
        monkeypatch.setattr(
            "src.sandbox.runner.run_local",
            _make_test_runner_mock(fail_first_n=0),
        )
        # CI always fails
        monkeypatch.setattr(
            "src.blueprint.nodes.deterministic.wait_for_ci",
            _make_ci_mock([
                _make_ci_result(success=False, logs="FAILED tests/test_integration.py"),
                _make_ci_result(success=False, logs="FAILED tests/test_integration.py"),
            ]),
        )
        monkeypatch.setattr(
            "src.blueprint.nodes.deterministic.create_github_pr",
            _make_pr_mock(),
        )
        monkeypatch.setattr(
            "src.blueprint.nodes.deterministic.get_autofix_registry",
            lambda: _FakeAutofixRegistry(),
        )

        runtime = _make_runtime()
        engine = BlueprintEngine(runtime=runtime)
        bp = _load_blueprint()
        ctx = _make_context(tmp_path)

        result = await engine.execute(bp, ctx)

        assert result.status == RunStatus.partial
        assert result.context.ci_rounds == 2
        assert result.context.ci_failed is True
        assert result.context.branch_pushed is True
        # PR still created even on partial
        assert result.pr_url == "https://github.com/org/inventory-api/pull/42"
        assert "open_pr" in _step_names(result)

    @pytest.mark.asyncio
    async def test_partial_accumulates_ci_context(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "src.blueprint.nodes.deterministic.subprocess.run",
            _make_git_mock(push_succeeds=True),
        )
        monkeypatch.setattr(
            "src.blueprint.nodes.deterministic.run_all_linters",
            _make_lint_mock(fail_first_n=0),
        )
        monkeypatch.setattr("src.sandbox.runner.get_runner", lambda: None)
        monkeypatch.setattr(
            "src.sandbox.runner.run_local",
            _make_test_runner_mock(fail_first_n=0),
        )
        monkeypatch.setattr(
            "src.blueprint.nodes.deterministic.wait_for_ci",
            _make_ci_mock([
                _make_ci_result(success=False, logs="FAILED test_x"),
                _make_ci_result(success=False, logs="FAILED test_y"),
            ]),
        )
        monkeypatch.setattr(
            "src.blueprint.nodes.deterministic.create_github_pr",
            _make_pr_mock(),
        )
        monkeypatch.setattr(
            "src.blueprint.nodes.deterministic.get_autofix_registry",
            lambda: _FakeAutofixRegistry(),
        )

        runtime = _make_runtime()
        engine = BlueprintEngine(runtime=runtime)
        bp = _load_blueprint()
        ctx = _make_context(tmp_path)

        result = await engine.execute(bp, ctx)

        assert result.context.extra.get("ci_run_id") == "ci-42"
        assert "ci_summary" in result.context.extra


class TestInventoryBlueprintAutofix:
    """CI fails with autofix-able errors, autofixes applied, then CI passes."""

    @pytest.mark.asyncio
    async def test_autofixes_bypass_agentic_fix(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "src.blueprint.nodes.deterministic.subprocess.run",
            _make_git_mock(push_succeeds=True),
        )
        monkeypatch.setattr(
            "src.blueprint.nodes.deterministic.run_all_linters",
            _make_lint_mock(fail_first_n=0),
        )
        monkeypatch.setattr("src.sandbox.runner.get_runner", lambda: None)
        monkeypatch.setattr(
            "src.sandbox.runner.run_local",
            _make_test_runner_mock(fail_first_n=0),
        )
        # CI first fails (with autofix-able lint), then passes
        monkeypatch.setattr(
            "src.blueprint.nodes.deterministic.wait_for_ci",
            _make_ci_mock([
                _make_ci_result(
                    success=False,
                    logs="src/app.py:1:1: F401 imported but unused",
                ),
                _make_ci_result(success=True),
            ]),
        )
        monkeypatch.setattr(
            "src.blueprint.nodes.deterministic.create_github_pr",
            _make_pr_mock(),
        )
        # Autofix registry finds and applies ruff fix
        monkeypatch.setattr(
            "src.blueprint.nodes.deterministic.get_autofix_registry",
            lambda: _FakeAutofixRegistry(
                applicable=[MagicMock(name="ruff_lint")],
                applied=["ruff_lint"],
            ),
        )

        runtime = _make_runtime()
        engine = BlueprintEngine(runtime=runtime)
        bp = _load_blueprint()
        ctx = _make_context(tmp_path)

        result = await engine.execute(bp, ctx)
        executed = _step_names(result)

        # Autofixes applied → ci_failed cleared by _apply_autofixes
        assert "apply_ci_autofixes" in executed
        # Since autofix clears ci_failed, handle_ci_failures should be skipped
        assert "handle_ci_failures" not in executed
        # But fix_attempted is set by autofix, so final verify runs
        assert "reset_for_final_verify" in executed
        assert "final_lint" in executed
        assert "final_verify" in executed
        assert "commit_and_push_final" in executed
        assert "second_ci_round" in executed
        assert "open_pr" in executed

        assert result.status == RunStatus.completed
        assert result.context.extra.get("applied_autofixes") == ["ruff_lint"]


class TestInventoryBlueprintGitPushFailure:
    """Git push fails → CI and PR steps skipped."""

    @pytest.mark.asyncio
    async def test_push_failure_skips_ci_and_pr(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "src.blueprint.nodes.deterministic.subprocess.run",
            _make_git_mock(push_succeeds=False),
        )
        monkeypatch.setattr(
            "src.blueprint.nodes.deterministic.run_all_linters",
            _make_lint_mock(fail_first_n=0),
        )
        monkeypatch.setattr("src.sandbox.runner.get_runner", lambda: None)
        monkeypatch.setattr(
            "src.sandbox.runner.run_local",
            _make_test_runner_mock(fail_first_n=0),
        )
        monkeypatch.setattr(
            "src.blueprint.nodes.deterministic.wait_for_ci",
            _make_ci_mock([_make_ci_result(success=True)]),
        )
        monkeypatch.setattr(
            "src.blueprint.nodes.deterministic.create_github_pr",
            _make_pr_mock(),
        )
        monkeypatch.setattr(
            "src.blueprint.nodes.deterministic.get_autofix_registry",
            lambda: _FakeAutofixRegistry(),
        )

        runtime = _make_runtime()
        engine = BlueprintEngine(runtime=runtime)
        bp = _load_blueprint()
        ctx = _make_context(tmp_path)

        result = await engine.execute(bp, ctx)
        executed = _step_names(result)

        # Push failed → last_push_succeeded is False → first_ci_round skipped
        assert "commit_and_push" in executed
        assert "first_ci_round" not in executed
        # branch_pushed is False → open_pr skipped
        assert "open_pr" not in executed

        assert result.context.branch_pushed is False
        assert result.context.last_push_succeeded is False


class TestInventoryBlueprintStepOrdering:
    """Verify that the blueprint step execution order matches the YAML definition."""

    @pytest.mark.asyncio
    async def test_happy_path_step_order(self, _patch_externals):
        tmp_path = _patch_externals["tmp_path"]
        runtime = _make_runtime()
        engine = BlueprintEngine(runtime=runtime)
        bp = _load_blueprint()
        ctx = _make_context(tmp_path)

        result = await engine.execute(bp, ctx)
        executed = _step_names(result)

        # Verify ordering: each step must appear after its predecessor
        expected_order = [
            "hydrate_context",
            "create_branch",
            "plan",
            "implement",
            "run_linter",
            "verify",
            "reset_fix_state",
            "commit_and_push",
            "first_ci_round",
            "open_pr",
        ]

        for i in range(len(expected_order) - 1):
            idx_current = executed.index(expected_order[i])
            idx_next = executed.index(expected_order[i + 1])
            assert idx_current < idx_next, (
                f"{expected_order[i]} (idx={idx_current}) should come before "
                f"{expected_order[i + 1]} (idx={idx_next})"
            )


class TestInventoryBlueprintTaskPayloadIntegration:
    """Verify the actual task payload from inventory_api example works."""

    @pytest.mark.asyncio
    async def test_task_payload_drives_execution(self, _patch_externals):
        tmp_path = _patch_externals["tmp_path"]
        payload = _load_task_payload()

        assert payload["blueprint"] == "default"
        assert "read_file" in payload["tools"]
        assert "edit_file" in payload["tools"]

        runtime = _make_runtime()
        engine = BlueprintEngine(runtime=runtime)
        bp = _load_blueprint()
        ctx = _make_context(tmp_path, payload)

        result = await engine.execute(bp, ctx)

        assert result.status == RunStatus.completed
        assert result.blueprint_name == "default"
        assert result.context.task_description == payload["description"]
        assert result.context.branch_base == payload["branch_base"]

    @pytest.mark.asyncio
    async def test_task_tools_propagated_to_context(self, _patch_externals):
        tmp_path = _patch_externals["tmp_path"]
        payload = _load_task_payload()
        ctx = _make_context(tmp_path, payload)

        assert ctx.extra["task_tools"] == payload["tools"]
