from __future__ import annotations

import pytest

from src.agent.tools import ToolRegistry
from src.rules.loader import RuleSet


def test_build_effective_rules_text_includes_scoped_repo_rules_and_overrides(monkeypatch):
    from src.orchestration.task_runner import build_effective_rules_text

    class FakeRuleResolver:
        def __init__(self, root_dir: str) -> None:
            self.root_dir = root_dir

        def get_matching_rulesets(self, file_paths):
            assert file_paths == ["/repo/src/app.py"]
            return [
                RuleSet(scope="**", rules=["Use typed APIs"], source=".minion/rules.yaml"),
                RuleSet(scope="src/**", rules=["Keep handlers small"], source="src/.minion/rules.yaml"),
            ]

        def get_rules_for_files(self, file_paths):
            assert file_paths == ["/repo/src/app.py"]
            return (
                "[Rules from .minion/rules.yaml]\n- Use typed APIs\n\n"
                "[Rules from src/.minion/rules.yaml]\n- Keep handlers small"
            )

    monkeypatch.setattr("src.orchestration.task_runner.RuleResolver", FakeRuleResolver)
    monkeypatch.setattr(
        "src.orchestration.task_runner.infer_rule_scope_targets",
        lambda repo_path, description, context_links: ["/repo/src/app.py"],
    )

    text, count, targets = build_effective_rules_text(
        repo_path="/repo",
        description="Update src/app.py",
        context_links=[],
        overrides=["Prefer explicit names", "Add tests for new behavior"],
    )

    assert count == 2
    assert targets == ["/repo/src/app.py"]
    assert "- Use typed APIs" in text
    assert "- Keep handlers small" in text
    assert "- Prefer explicit names" in text
    assert "- Add tests for new behavior" in text


def test_build_effective_rules_text_ignores_blank_overrides(monkeypatch):
    from src.orchestration.task_runner import build_effective_rules_text

    class FakeRuleResolver:
        def __init__(self, root_dir: str) -> None:
            self.root_dir = root_dir

        def get_matching_rulesets(self, file_paths):
            assert file_paths == []
            return []

        def get_rules_for_files(self, file_paths):
            assert file_paths == []
            return ""

    monkeypatch.setattr("src.orchestration.task_runner.RuleResolver", FakeRuleResolver)
    monkeypatch.setattr(
        "src.orchestration.task_runner.infer_rule_scope_targets",
        lambda repo_path, description, context_links: [],
    )

    text, count, targets = build_effective_rules_text(
        repo_path=".",
        description="General cleanup",
        context_links=[],
        overrides=["", "  ", "Use safe defaults"],
    )

    assert count == 0
    assert targets == []
    assert text == "- Use safe defaults"


def test_infer_rule_scope_targets_finds_task_and_link_paths(tmp_path):
    from src.orchestration.task_runner import infer_rule_scope_targets

    src_dir = tmp_path / "src"
    src_dir.mkdir()
    app_file = src_dir / "app.py"
    app_file.write_text("print('ok')\n")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    test_file = tests_dir / "test_app.py"
    test_file.write_text("def test_ok():\n    pass\n")

    targets = infer_rule_scope_targets(
        str(tmp_path),
        "Update src/app.py and keep tests/test_app.py green",
        ["https://github.com/acme/minions/blob/main/src/app.py"],
    )

    assert targets == [str(app_file), str(test_file)]


def test_resolve_rule_scope_targets_includes_prior_and_output_paths(tmp_path):
    from src.blueprint.nodes.deterministic import NodeContext
    from src.orchestration.task_runner import resolve_rule_scope_targets

    src_dir = tmp_path / "src"
    src_dir.mkdir()
    app_file = src_dir / "app.py"
    app_file.write_text("print('ok')\n")
    worker_file = src_dir / "worker.py"
    worker_file.write_text("print('worker')\n")

    context = NodeContext(
        extra={
            "plan_output": "Modify src/worker.py to support retries",
            "rule_scope_targets": [str(app_file)],
        }
    )

    targets = resolve_rule_scope_targets(
        str(tmp_path),
        "Update src/app.py",
        [],
        context,
    )

    assert targets == [str(app_file), str(worker_file)]


def test_resolve_run_tool_names_uses_blueprint_and_task_allowlist():
    from src.blueprint.schema import Blueprint
    from src.orchestration.task_runner import resolve_run_tool_names

    blueprint = Blueprint.from_yaml("""
name: test
steps:
  - name: plan
    type: agentic
    prompt: "Plan"
    tools: [read_file, jira_get_issue]
  - name: implement
    type: agentic
    prompt: "Implement"
    tools: [edit_file, grep_search]
""")

    names = resolve_run_tool_names(blueprint, ["read_file", "edit_file", "jira_get_issue"])

    assert names == {"read_file", "edit_file", "jira_get_issue"}


def test_build_run_tool_registry_limits_builtin_tools_to_run_scope():
    from src.blueprint.schema import Blueprint
    from src.orchestration.task_runner import build_run_tool_registry

    blueprint = Blueprint.from_yaml("""
name: test
steps:
  - name: implement
    type: agentic
    prompt: "Implement"
    tools: [read_file]
""")

    registry = build_run_tool_registry(blueprint, [])
    tool_names = {tool.name for tool in registry.list_tools()}

    assert "read_file" in tool_names
    assert "get_ci_logs" in tool_names
    assert "edit_file" not in tool_names


@pytest.mark.asyncio
async def test_task_runner_injects_hydrated_context(monkeypatch):
    from src.blueprint.engine import BlueprintRunResult, RunStatus
    from src.orchestration.task_runner import TaskRunRequest, TaskRunner
    from src.sandbox.config import SandboxConfig, SandboxMode

    class FakeSandboxRunner:
        def __init__(self, repo_path: str, config: SandboxConfig | None = None, use_pool: bool = False) -> None:
            self.repo_path = repo_path
            self.config = config or SandboxConfig(mode=SandboxMode.development)
            self.use_pool = use_pool

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    captured = {}

    class FakeHydrationResult:
        def __init__(self) -> None:
            self.text = "### Linked Context\n- hydrated"
            self.traces = [SimpleTrace()]

    class SimpleTrace:
        resolver = "context_links"
        section = "linked_context"
        item_count = 1

    async def fake_hydrate_context(context_links, jira_ticket=None, metadata_hooks=None):
        return FakeHydrationResult()

    class FakeEngine:
        def __init__(self, runtime, rules_text="", rules_resolver=None) -> None:
            self.runtime = runtime
            self.rules_text = rules_text
            self.rules_resolver = rules_resolver

        async def execute(self, blueprint, context):
            captured["context"] = context
            return BlueprintRunResult(
                run_id="run-1",
                blueprint_name=blueprint.name,
                status=RunStatus.completed,
                context=context,
            )

    monkeypatch.setattr("src.orchestration.task_runner.SandboxRunner", FakeSandboxRunner)
    monkeypatch.setattr("src.orchestration.task_runner.hydrate_context_detailed", fake_hydrate_context)
    monkeypatch.setattr("src.orchestration.task_runner.BlueprintEngine", FakeEngine)

    runner = TaskRunner()
    result = await runner.run(
        TaskRunRequest(
            description="Hydrate task",
            repo=".",
            context_links=["https://example.com/spec"],
        )
    )

    assert result.status == RunStatus.completed
    assert captured["context"].extra["hydrated_context"] == "### Linked Context\n- hydrated"
    assert captured["context"].extra["hydration_trace"] == [
        {"resolver": "context_links", "section": "linked_context", "item_count": 1}
    ]


@pytest.mark.asyncio
async def test_task_runner_rejects_host_fallback_in_production_mode(monkeypatch):
    from src.orchestration.task_runner import TaskRunRequest, TaskRunner
    from src.sandbox.config import SandboxConfig, SandboxMode

    class FakeSandboxRunner:
        def __init__(self, repo_path: str, config: SandboxConfig | None = None, use_pool: bool = False) -> None:
            self.repo_path = repo_path
            self.config = config or SandboxConfig(mode=SandboxMode.production)
            self.use_pool = use_pool

        async def start(self) -> None:
            raise RuntimeError("docker unavailable")

        async def stop(self) -> None:
            return None

    monkeypatch.setattr("src.orchestration.task_runner.SandboxRunner", FakeSandboxRunner)
    monkeypatch.setattr(
        "src.orchestration.task_runner.default_sandbox_config",
        lambda repo_path, mode=None: SandboxConfig(
            image="minions-sandbox:latest",
            volumes={repo_path: "/workspace"},
            mode=SandboxMode.production,
            allow_host_fallback=False,
        ),
    )

    runner = TaskRunner()
    with pytest.raises(RuntimeError, match="Docker sandbox unavailable in production mode"):
        await runner.run(
            TaskRunRequest(
                description="Hydrate task",
                repo=".",
                source="api",
            )
        )


@pytest.mark.asyncio
async def test_task_runner_allows_development_fallback(monkeypatch):
    from src.blueprint.engine import BlueprintRunResult, RunStatus
    from src.orchestration.task_runner import TaskRunRequest, TaskRunner
    from src.sandbox.config import SandboxConfig, SandboxMode

    class FakeSandboxRunner:
        def __init__(self, repo_path: str, config: SandboxConfig | None = None, use_pool: bool = False) -> None:
            self.repo_path = repo_path
            self.config = config or SandboxConfig(mode=SandboxMode.development, allow_host_fallback=True)
            self.use_pool = use_pool

        async def start(self) -> None:
            raise RuntimeError("docker unavailable")

        async def stop(self) -> None:
            return None

    class FakeEngine:
        def __init__(self, runtime, rules_text="", rules_resolver=None) -> None:
            self.runtime = runtime
            self.rules_text = rules_text
            self.rules_resolver = rules_resolver

        async def execute(self, blueprint, context):
            return BlueprintRunResult(
                run_id="run-1",
                blueprint_name=blueprint.name,
                status=RunStatus.completed,
                context=context,
            )

    messages: list[str] = []
    monkeypatch.setattr("src.orchestration.task_runner.SandboxRunner", FakeSandboxRunner)
    monkeypatch.setattr("src.orchestration.task_runner.BlueprintEngine", FakeEngine)
    monkeypatch.setattr(
        "src.orchestration.task_runner.default_sandbox_config",
        lambda repo_path, mode=None: SandboxConfig(
            image="minions-sandbox:latest",
            volumes={repo_path: "/workspace"},
            mode=SandboxMode.development,
            allow_host_fallback=True,
        ),
    )

    runner = TaskRunner(status_callback=messages.append)
    result = await runner.run(
        TaskRunRequest(
            description="Hydrate task",
            repo=".",
            source="cli",
        )
    )

    assert result.status == RunStatus.completed
    assert any("sandbox=local-degraded" in message for message in messages)


@pytest.mark.asyncio
async def test_task_runner_uses_pooled_sandbox_in_production(monkeypatch):
    from src.blueprint.engine import BlueprintRunResult, RunStatus
    from src.orchestration.task_runner import TaskRunRequest, TaskRunner
    from src.sandbox.config import SandboxConfig, SandboxMode

    class FakeSandboxRunner:
        def __init__(self, repo_path: str, config: SandboxConfig | None = None, use_pool: bool = False) -> None:
            self.repo_path = repo_path
            self.config = config or SandboxConfig(mode=SandboxMode.production)
            self.use_pool = use_pool

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    class FakeEngine:
        def __init__(self, runtime, rules_text="", rules_resolver=None) -> None:
            self.runtime = runtime
            self.rules_text = rules_text
            self.rules_resolver = rules_resolver

        async def execute(self, blueprint, context):
            return BlueprintRunResult(
                run_id="run-1",
                blueprint_name=blueprint.name,
                status=RunStatus.completed,
                context=context,
            )

    seen: dict[str, bool] = {}

    def fake_runner(repo_path: str, config: SandboxConfig | None = None, use_pool: bool = False):
        seen["use_pool"] = use_pool
        return FakeSandboxRunner(repo_path=repo_path, config=config, use_pool=use_pool)

    messages: list[str] = []
    monkeypatch.setattr("src.orchestration.task_runner.SandboxRunner", fake_runner)
    monkeypatch.setattr("src.orchestration.task_runner.BlueprintEngine", FakeEngine)
    monkeypatch.setattr(
        "src.orchestration.task_runner.default_sandbox_config",
        lambda repo_path, mode=None: SandboxConfig(
            image="minions-sandbox:latest",
            volumes={repo_path: "/workspace"},
            mode=SandboxMode.production,
            allow_host_fallback=False,
        ),
    )

    runner = TaskRunner(status_callback=messages.append)
    result = await runner.run(
        TaskRunRequest(
            description="Hydrate task",
            repo=".",
            source="api",
        )
    )

    assert result.status == RunStatus.completed
    assert seen["use_pool"] is True
    assert any("sandbox=docker-pooled" in message for message in messages)


def test_default_sandbox_config_uses_project_image_and_policy_env(monkeypatch):
    from src.sandbox.config import SandboxMode
    from src.sandbox.runner import default_sandbox_config

    monkeypatch.setenv("MINIONS_SANDBOX_IMAGE", "custom-sandbox:v1")
    monkeypatch.setenv("MINIONS_SANDBOX_CPU_LIMIT", "4")
    monkeypatch.setenv("MINIONS_SANDBOX_MEMORY_LIMIT", "8g")
    monkeypatch.setenv("MINIONS_SANDBOX_TIMEOUT", "900")

    config = default_sandbox_config("/repo", SandboxMode.production)

    assert config.image == "custom-sandbox:v1"
    assert config.cpu_limit == "4"
    assert config.memory_limit == "8g"
    assert config.timeout == 900
    assert config.network_enabled is False


def test_task_runner_resolves_rules_per_node_from_context_outputs(monkeypatch):
    from src.blueprint.nodes.deterministic import NodeContext
    from src.blueprint.schema import BlueprintNode, NodeType
    from src.orchestration.task_runner import TaskRunner

    runner = TaskRunner()
    context = NodeContext(extra={"plan_output": "Update src/worker.py"})
    captured = {}

    def fake_build_rule_resolution(repo_path, description, context_links, overrides, context=None):
        captured["context"] = context
        return {
            "text": "scoped rules",
            "ruleset_count": 1,
            "targets": ["/repo/src/app.py", "/repo/src/worker.py"],
            "trace": [{"source": "src/.minion/rules.yaml", "scope": "src/**", "global": False}],
        }

    monkeypatch.setattr(
        "src.orchestration.task_runner.build_rule_resolution",
        fake_build_rule_resolution,
    )

    text = runner._resolve_rules_for_node(
        repo_path="/repo",
        description="Update src/app.py",
        context_links=[],
        overrides=["Prefer tests"],
        node=BlueprintNode(name="implement", type=NodeType.agentic, prompt="Do it"),
        context=context,
    )

    assert text == "scoped rules"
    assert captured["context"] is context
    assert context.extra["rule_scope_targets"] == ["/repo/src/app.py", "/repo/src/worker.py"]
    assert context.extra["rule_trace"] == [
        {"source": "src/.minion/rules.yaml", "scope": "src/**", "global": False}
    ]
    assert context.extra["last_rule_scope_node"] == "implement"


@pytest.mark.asyncio
async def test_build_metadata_hooks_uses_jira_tool():
    from src.agent.tools import Tool
    from src.orchestration.task_runner import build_metadata_hooks

    registry = ToolRegistry()

    async def jira_get_issue(issue_key: str) -> str:
        return f"- Issue {issue_key}\n- Summary: Fix the billing flow"

    registry.register(
        Tool(
            name="jira_get_issue",
            description="Fetch Jira issue details",
            parameters={"type": "object", "properties": {"issue_key": {"type": "string"}}},
            handler=jira_get_issue,
        )
    )

    hooks = build_metadata_hooks(registry)
    result = await hooks[0](context_links=[], jira_ticket="PROJ-100")

    assert result is not None
    assert "### Jira Details" in result[0]
    assert "Fix the billing flow" in result[0]


@pytest.mark.asyncio
async def test_connect_mcp_servers_registers_only_allowed_tools(monkeypatch):
    from src.agent.tools import Tool
    from src.orchestration.task_runner import TaskRunner
    from src.tools.mcp_client import MCPServerConfig

    registry = ToolRegistry()
    runner = TaskRunner()

    class FakeMCPClient:
        def __init__(self, config: MCPServerConfig) -> None:
            self.config = config

        async def connect(self) -> None:
            return None

        async def discover_tools(self):
            return [
                Tool(
                    name="jira_get_issue",
                    description="Fetch issue",
                    parameters={"type": "object", "properties": {}},
                    handler=lambda **kwargs: "ok",
                ),
                Tool(
                    name="jira_search_issues",
                    description="Search issues",
                    parameters={"type": "object", "properties": {}},
                    handler=lambda **kwargs: "ok",
                ),
            ]

        async def close(self) -> None:
            return None

    monkeypatch.setenv("MCP_SERVERS", "jira:stdio:uvx mcp-atlassian")
    monkeypatch.setattr("src.orchestration.task_runner.MCPClient", FakeMCPClient)

    clients = await runner._connect_mcp_servers(registry, allowed_names={"jira_get_issue"})

    assert len(clients) == 1
    assert registry.get("jira_get_issue") is not None
    assert registry.get("jira_search_issues") is None


@pytest.mark.asyncio
async def test_build_metadata_hooks_uses_ci_tools_for_actions_links():
    from src.agent.tools import Tool
    from src.orchestration.task_runner import build_metadata_hooks

    registry = ToolRegistry()

    def get_test_results(repo: str, run_id: str) -> str:
        return f"Test results for run {run_id}:\n  Job: unit-tests — failure"

    def get_ci_logs(repo: str, run_id: str) -> str:
        return f"Logs for {repo} run {run_id}: stacktrace..."

    registry.register(
        Tool(
            name="get_test_results",
            description="Fetch test results",
            parameters={"type": "object", "properties": {}},
            handler=get_test_results,
        )
    )
    registry.register(
        Tool(
            name="get_ci_logs",
            description="Fetch CI logs",
            parameters={"type": "object", "properties": {}},
            handler=get_ci_logs,
        )
    )

    hooks = build_metadata_hooks(registry)
    ci_hook = hooks[0] if len(hooks) == 1 else hooks[1]
    result = await ci_hook(
        context_links=["https://github.com/acme/payments/actions/runs/12345"],
        jira_ticket=None,
    )

    assert result is not None
    assert "### CI Context" in result[0]
    assert "unit-tests" in result[0]
    assert "stacktrace" in result[0]
