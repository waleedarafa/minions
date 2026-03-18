from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from src.blueprint.schema import Blueprint, BlueprintNode, NodeType, BlueprintRegistry
from src.blueprint.nodes.deterministic import NodeContext
from src.blueprint.nodes.agentic import resolve_allowed_tools, resolve_max_iterations


def test_blueprint_from_yaml():
    yaml_content = """
name: test_blueprint
steps:
  - name: step1
    type: deterministic
    action: noop
  - name: step2
    type: agentic
    prompt: "Do something"
"""
    bp = Blueprint.from_yaml(yaml_content)
    assert bp.name == "test_blueprint"
    assert len(bp.steps) == 2
    assert bp.steps[0].type == NodeType.deterministic
    assert bp.steps[1].type == NodeType.agentic


def test_blueprint_node_validation():
    # Deterministic node without action should fail
    with pytest.raises(Exception):
        BlueprintNode(name="bad", type=NodeType.deterministic)

    # Agentic node without prompt should fail
    with pytest.raises(Exception):
        BlueprintNode(name="bad", type=NodeType.agentic)


def test_blueprint_registry():
    registry = BlueprintRegistry()
    bp = Blueprint.from_yaml("""
name: test
steps:
  - name: step1
    type: deterministic
    action: noop
""")
    registry.register(bp)
    assert "test" in registry.list()
    assert registry.get("test") is bp
    assert registry.get("nonexistent") is None


def test_node_context_defaults():
    ctx = NodeContext()
    assert ctx.ci_failed is False
    assert ctx.ci_rounds == 0
    assert ctx.max_ci_rounds == 2
    assert ctx.branch_base == "main"
    assert ctx.branch_pushed is False
    assert ctx.last_push_succeeded is False


def test_blueprint_get_step():
    bp = Blueprint.from_yaml("""
name: test
steps:
  - name: my_step
    type: deterministic
    action: noop
""")
    assert bp.get_step("my_step") is not None
    assert bp.get_step("missing") is None


def test_agentic_node_uses_blueprint_iterations_without_override():
    node = BlueprintNode(name="implement", type=NodeType.agentic, prompt="Do something", max_iterations=12)
    ctx = NodeContext()
    assert resolve_max_iterations(node, ctx) == 12


def test_agentic_node_uses_context_override_when_present():
    node = BlueprintNode(name="implement", type=NodeType.agentic, prompt="Do something", max_iterations=12)
    ctx = NodeContext(extra={"agent_max_iterations": 75})
    assert resolve_max_iterations(node, ctx) == 75


def test_agentic_node_uses_blueprint_tools_without_task_override():
    node = BlueprintNode(name="implement", type=NodeType.agentic, prompt="Do something", tools=["read_file", "edit_file"])
    ctx = NodeContext()
    assert resolve_allowed_tools(node, ctx) == ["read_file", "edit_file"]


def test_agentic_node_intersects_task_tools_with_blueprint_tools():
    node = BlueprintNode(name="implement", type=NodeType.agentic, prompt="Do something", tools=["read_file", "edit_file"])
    ctx = NodeContext(extra={"task_tools": ["read_file", "grep_search"]})
    assert resolve_allowed_tools(node, ctx) == ["read_file"]


def test_agentic_node_can_be_restricted_to_zero_tools():
    node = BlueprintNode(name="implement", type=NodeType.agentic, prompt="Do something", tools=["read_file"])
    ctx = NodeContext(extra={"task_tools": ["edit_file"]})
    assert resolve_allowed_tools(node, ctx) == []


def test_agentic_prompt_includes_hydrated_context():
    from src.blueprint.nodes.agentic import AgenticNodeExecutor

    runtime = MagicMock()
    executor = AgenticNodeExecutor(runtime=runtime)
    node = BlueprintNode(name="plan", type=NodeType.agentic, prompt="Plan the change")
    ctx = NodeContext(
        task_description="Implement feature",
        repo_path="/tmp/repo",
        extra={"hydrated_context": "### Linked Context\n- https://example.com/spec"},
    )

    prompt = executor._build_task_prompt(node, ctx)

    assert "## Hydrated Context" in prompt
    assert "https://example.com/spec" in prompt


def test_agentic_prompt_includes_repository_overview():
    from src.blueprint.nodes.agentic import AgenticNodeExecutor

    runtime = MagicMock()
    executor = AgenticNodeExecutor(runtime=runtime)
    node = BlueprintNode(name="plan", type=NodeType.agentic, prompt="Plan the change")
    ctx = NodeContext(
        task_description="Implement feature",
        repo_path="/tmp/repo",
        extra={"repo_overview": "Top Level:\n- src/\n- tests/\n\nsrc/:\n- src/app.py"},
    )

    prompt = executor._build_task_prompt(node, ctx)

    assert "## Repository Overview" in prompt
    assert "src/app.py" in prompt


def test_agentic_prompt_includes_rule_scope_observability():
    from src.blueprint.nodes.agentic import AgenticNodeExecutor

    runtime = MagicMock()
    executor = AgenticNodeExecutor(runtime=runtime)
    node = BlueprintNode(name="implement", type=NodeType.agentic, prompt="Implement the change")
    ctx = NodeContext(
        task_description="Implement feature",
        repo_path="/tmp/repo",
        extra={
            "rule_scope_targets": ["/tmp/repo/src/app.py"],
            "rule_trace": [
                {"source": ".minion/rules.yaml", "scope": "**"},
                {"source": "src/.minion/rules.yaml", "scope": "src/**"},
            ],
        },
    )

    prompt = executor._build_task_prompt(node, ctx)

    assert "## Active Rule Scope" in prompt
    assert "/tmp/repo/src/app.py" in prompt
    assert "## Active Rule Sources" in prompt
    assert "src/.minion/rules.yaml" in prompt


def test_agentic_prompt_includes_structured_failure_summaries():
    from src.blueprint.nodes.agentic import AgenticNodeExecutor

    runtime = MagicMock()
    executor = AgenticNodeExecutor(runtime=runtime)
    node = BlueprintNode(name="fix_failures", type=NodeType.agentic, prompt="Fix the failures")
    ctx = NodeContext(
        task_description="Fix failing checks",
        repo_path="/tmp/repo",
        ci_failed=True,
        extra={
            "lint_failures_summary": "Found 1 failure(s):\n  - [F401] unused import",
            "test_failures_summary": "Found 1 failure(s):\n  - [AssertionError] test_api",
            "ci_failures_summary": "Found 1 failure(s):\n  - [TestFailure] test_worker",
            "lint_output": "src/app.py:1:1: F401 imported but unused",
            "test_output": "FAILED tests/test_api.py::test_ok",
            "ci_output": "FAILED tests/test_worker.py::test_retry",
        },
    )

    prompt = executor._build_task_prompt(node, ctx)

    assert "## Failure Summary" in prompt
    assert "### Lint Summary" in prompt
    assert "### Test Summary" in prompt
    assert "### CI Summary" in prompt
    assert "## Verification Failures" in prompt
    assert "### CI Logs" in prompt


@pytest.mark.asyncio
async def test_blueprint_engine_noop():
    """Test that blueprint engine can run a simple noop deterministic step."""
    from src.blueprint.engine import BlueprintEngine
    mock_runtime = MagicMock()
    engine = BlueprintEngine(runtime=mock_runtime)

    bp = Blueprint.from_yaml("""
name: test
steps:
  - name: step1
    type: deterministic
    action: noop
""")
    ctx = NodeContext(task_description="test task")
    result = await engine.execute(bp, ctx)
    assert result.blueprint_name == "test"
    assert len(result.steps) == 1
    assert result.steps[0].name == "step1"
    assert result.steps[0].status == "completed"


@pytest.mark.asyncio
async def test_blueprint_engine_uses_dynamic_rules_resolver_for_agentic_steps():
    from src.blueprint.engine import BlueprintEngine

    runtime = MagicMock()
    runtime.run = AsyncMock(
        return_value=MagicMock(
        success=True,
        iterations=1,
        output="done",
        tokens_used={"total_tokens": 10},
        tool_calls_made=[],
        error=None,
        )
    )

    bp = Blueprint.from_yaml("""
name: test
steps:
  - name: step1
    type: agentic
    prompt: "Do something"
""")
    ctx = NodeContext(task_description="test task")
    resolver = MagicMock(return_value="- Use scoped rules")
    engine = BlueprintEngine(runtime=runtime, rules_text="- fallback", rules_resolver=resolver)

    result = await engine.execute(bp, ctx)

    assert result.steps[0].status == "completed"
    resolver.assert_called_once()
    system_prompt = runtime.run.await_args.kwargs["system_prompt"]
    assert "Use scoped rules" in system_prompt


@pytest.mark.asyncio
async def test_blueprint_engine_marks_final_failed_ci_as_partial():
    from src.blueprint.engine import BlueprintEngine, RunStatus

    mock_runtime = MagicMock()
    engine = BlueprintEngine(runtime=mock_runtime)
    bp = Blueprint.from_yaml("""
name: test
steps:
  - name: step1
    type: deterministic
    action: noop
""")
    ctx = NodeContext(task_description="test task", ci_failed=True, ci_rounds=2, max_ci_rounds=2, branch_pushed=True)

    result = await engine.execute(bp, ctx)

    assert result.status == RunStatus.partial


@pytest.mark.asyncio
async def test_blueprint_engine_marks_agentic_runtime_failure_as_failed():
    from src.blueprint.engine import BlueprintEngine, RunStatus

    runtime = MagicMock()
    runtime.run = AsyncMock(
        return_value=MagicMock(
            success=False,
            iterations=1,
            output="",
            tokens_used={"total_tokens": 0},
            tool_calls_made=[],
            error="LLM error: Connection error.",
        )
    )

    bp = Blueprint.from_yaml("""
name: test
steps:
  - name: plan
    type: agentic
    prompt: "Plan the work"
""")
    ctx = NodeContext(task_description="test task")
    engine = BlueprintEngine(runtime=runtime)

    result = await engine.execute(bp, ctx)

    assert result.status == RunStatus.failed
    assert result.steps[0].status == "failed"
    assert "Connection error" in (result.steps[0].error or "")
