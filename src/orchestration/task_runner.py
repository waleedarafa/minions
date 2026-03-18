from __future__ import annotations

import inspect
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from src.agent import AgentRuntime, ContextManager, LLMClient, LLMConfig
from src.agent.tools import Tool
from src.blueprint.engine import BlueprintEngine, BlueprintRunResult
from src.blueprint.nodes.deterministic import NodeContext
from src.blueprint.schema import Blueprint, BlueprintNode, BlueprintRegistry
from src.context import hydrate_context_detailed
from src.rules.resolver import RuleResolver
from src.sandbox.config import SandboxMode
from src.sandbox.runner import SandboxRunner, default_sandbox_config, set_runner
from src.tools.builtin.execution import set_working_dir as set_exec_dir
from src.tools.builtin.file_ops import set_working_dir as set_file_dir
from src.tools.mcp_client import MCPClient, MCPServerConfig
from src.tools.registry import GlobalToolRegistry
from src.tools.security import SecurityPolicy, get_security_events, reset_security_events, set_security_policy

if TYPE_CHECKING:
    from src.agent.tools import ToolRegistry

logger = structlog.get_logger()
_GITHUB_ACTIONS_LINK_RE = re.compile(r"^https://github\.com/([^/]+/[^/]+)/actions/runs/([^/?#]+)")
_PATH_HINT_RE = re.compile(r"(?<!\w)(?:[\w.-]+/)*[\w.-]+\.[A-Za-z0-9]+")
_GITHUB_BLOB_LINK_RE = re.compile(r"^https://github\.com/[^/]+/[^/]+/(?:blob|tree)/[^/]+/(.+?)(?:[#?].*)?$")
_ORCHESTRATION_TOOL_NAMES = {"get_ci_logs", "get_test_results"}


def _load_project_env() -> None:
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return

    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def _blueprints_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "blueprints"


def load_blueprint_registry() -> BlueprintRegistry:
    registry = BlueprintRegistry()
    registry.load_from_directory(str(_blueprints_dir()))
    return registry


def get_blueprint(name: str) -> Blueprint | None:
    return load_blueprint_registry().get(name)


def list_blueprints() -> list[Blueprint]:
    registry = load_blueprint_registry()
    return sorted(
        [registry.get(name) for name in registry.list() if registry.get(name) is not None],
        key=lambda bp: bp.name,
    )


def infer_rule_scope_targets(repo_path: str, description: str, context_links: list[str]) -> list[str]:
    root = Path(repo_path).resolve()
    candidates: list[str] = []

    for match in _PATH_HINT_RE.findall(description):
        candidate = (root / match).resolve()
        if candidate.exists() and candidate.is_relative_to(root):
            candidates.append(str(candidate))

    for link in context_links:
        match = _GITHUB_BLOB_LINK_RE.match(link)
        if not match:
            continue
        candidate = (root / match.group(1)).resolve()
        if candidate.exists() and candidate.is_relative_to(root):
            candidates.append(str(candidate))

    unique: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            unique.append(candidate)
    return unique


def build_repo_overview(repo_path: str) -> str:
    root = Path(repo_path)
    if not root.exists():
        return ""

    def _sorted_entries(path: Path) -> list[Path]:
        try:
            return sorted(path.iterdir(), key=lambda entry: (entry.is_file(), entry.name))
        except OSError:
            return []

    sections: list[str] = []

    top_level = [entry.name + ("/" if entry.is_dir() else "") for entry in _sorted_entries(root)[:12]]
    if top_level:
        sections.append("Top Level:\n" + "\n".join(f"- {name}" for name in top_level))

    for dirname in ("src", "tests"):
        directory = root / dirname
        if not directory.is_dir():
            continue
        child_entries = [
            str(entry.relative_to(root)) + ("/" if entry.is_dir() else "")
            for entry in _sorted_entries(directory)[:12]
        ]
        if child_entries:
            sections.append(f"{dirname}/:\n" + "\n".join(f"- {name}" for name in child_entries))

    return "\n\n".join(sections)


def collect_rule_scope_sources(description: str, context: NodeContext | None = None) -> list[str]:
    sources = [description]
    if context is None:
        return sources

    for key, value in context.extra.items():
        if not key.endswith("_output") or not isinstance(value, str) or not value.strip():
            continue
        sources.append(value)
    return sources


def resolve_rule_scope_targets(
    repo_path: str,
    description: str,
    context_links: list[str],
    context: NodeContext | None = None,
) -> list[str]:
    candidates: list[str] = []
    for source in collect_rule_scope_sources(description, context):
        candidates.extend(infer_rule_scope_targets(repo_path, source, context_links))

    if context is not None:
        prior_targets = context.extra.get("rule_scope_targets")
        if isinstance(prior_targets, list):
            candidates.extend([target for target in prior_targets if isinstance(target, str)])

    unique: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            unique.append(candidate)
    return unique


def collect_blueprint_tool_names(blueprint: Blueprint) -> set[str]:
    names: set[str] = set()
    for step in blueprint.steps:
        names.update(step.tools)
    return names


def resolve_run_tool_names(blueprint: Blueprint, task_tools: list[str]) -> set[str]:
    names = collect_blueprint_tool_names(blueprint) | _ORCHESTRATION_TOOL_NAMES
    requested = {tool for tool in task_tools if isinstance(tool, str)}
    if requested:
        return names & requested
    return names


def build_run_tool_registry(blueprint: Blueprint, task_tools: list[str]) -> ToolRegistry:
    allowed_names = resolve_run_tool_names(blueprint, task_tools)
    return GlobalToolRegistry().create_builtin_registry(allowed_names=allowed_names)


def build_effective_rules_text(
    repo_path: str,
    description: str,
    context_links: list[str],
    overrides: list[str],
    context: NodeContext | None = None,
) -> tuple[str, int, list[str]]:
    resolution = build_rule_resolution(repo_path, description, context_links, overrides, context)
    return resolution["text"], resolution["ruleset_count"], resolution["targets"]


def build_rule_resolution(
    repo_path: str,
    description: str,
    context_links: list[str],
    overrides: list[str],
    context: NodeContext | None = None,
) -> dict[str, Any]:
    resolver = RuleResolver(repo_path)
    targets = resolve_rule_scope_targets(repo_path, description, context_links, context)
    matching_rulesets = resolver.get_matching_rulesets(targets)
    parts = []
    scoped_text = resolver.get_rules_for_files(targets)
    if scoped_text:
        parts.append(scoped_text)

    override_lines = [rule.strip() for rule in overrides if rule.strip()]
    if override_lines:
        parts.append("\n".join(f"- {rule}" for rule in override_lines))

    trace = [
        {
            "source": ruleset.source,
            "scope": ruleset.scope,
            "global": ruleset.scope == "**",
        }
        for ruleset in matching_rulesets
    ]
    if override_lines:
        trace.append(
            {
                "source": "request.rules_override",
                "scope": "request",
                "global": False,
                "override_count": len(override_lines),
            }
        )

    return {
        "text": "\n\n".join(parts),
        "ruleset_count": len(matching_rulesets),
        "targets": targets,
        "trace": trace,
    }


async def _invoke_tool(tool: Tool, argument_candidates: list[dict[str, Any]]) -> str | None:
    for arguments in argument_candidates:
        try:
            result = tool.handler(**arguments)
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, str) and result.strip():
                return result.strip()
        except Exception:
            continue
    return None


def build_metadata_hooks(registry: ToolRegistry) -> list:
    hooks = []
    jira_tool = registry.get("jira_get_issue")
    if jira_tool is not None:

        async def jira_hook(context_links: list[str], jira_ticket: str | None) -> list[str] | None:
            if not jira_ticket:
                return None
            output = await _invoke_tool(
                jira_tool,
                [
                    {"issue_key": jira_ticket},
                    {"ticket_id": jira_ticket},
                    {"key": jira_ticket},
                    {"id": jira_ticket},
                    {"issue": jira_ticket},
                ],
            )
            if not output:
                return None
            return [f"### Jira Details\n{output}"]

        hooks.append(jira_hook)

    ci_logs_tool = registry.get("get_ci_logs")
    test_results_tool = registry.get("get_test_results")
    if ci_logs_tool is not None or test_results_tool is not None:

        async def github_actions_hook(context_links: list[str], jira_ticket: str | None) -> list[str] | None:
            sections: list[str] = []
            for link in context_links:
                match = _GITHUB_ACTIONS_LINK_RE.match(link)
                if not match:
                    continue

                repo, run_id = match.group(1), match.group(2)
                tool_sections: list[str] = []
                if test_results_tool is not None:
                    test_output = await _invoke_tool(
                        test_results_tool,
                        [{"repo": repo, "run_id": run_id}],
                    )
                    if test_output and not test_output.startswith("Error:"):
                        tool_sections.append(f"Test Results:\n{test_output}")

                if ci_logs_tool is not None:
                    log_output = await _invoke_tool(
                        ci_logs_tool,
                        [{"repo": repo, "run_id": run_id}],
                    )
                    if log_output and not log_output.startswith("Error:"):
                        excerpt = log_output[:1500]
                        tool_sections.append(f"Logs:\n{excerpt}")

                if tool_sections:
                    sections.append(
                        "### CI Context\n"
                        f"- Link: {link}\n"
                        + "\n\n".join(tool_sections)
                    )

            return sections or None

        hooks.append(github_actions_hook)

    return hooks


@dataclass
class TaskRunRequest:
    description: str
    repo: str = "."
    blueprint: str = "default"
    model: str = "claude-sonnet-4-20250514"
    max_iterations: int | None = None
    jira: str | None = None
    branch_base: str = "main"
    context_links: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    rules_override: list[str] = field(default_factory=list)
    priority: str = "normal"
    created_by: str = ""
    source: str = "cli"


class TaskRunner:
    def __init__(self, status_callback: callable | None = None) -> None:
        self._status_callback = status_callback

    async def run(self, request: TaskRunRequest) -> BlueprintRunResult:
        _load_project_env()
        repo_path = os.path.abspath(request.repo)
        sandbox_config = default_sandbox_config(
            repo_path,
            SandboxMode.development if request.source == "cli" else SandboxMode.production,
        )

        set_exec_dir(repo_path)
        set_file_dir(repo_path)
        reset_security_events()
        security_policy = SecurityPolicy(
            allowed_commands=["git", "python", "pytest", "ruff", "mypy", "npm", "yarn", "make"],
            blocked_paths=["/etc/passwd", "/etc/shadow", "/root", "~/.ssh", "~/.aws"],
            allow_network=sandbox_config.network_enabled,
            allow_write=True,
            working_dir=repo_path,
        )
        set_security_policy(security_policy)

        blueprint = get_blueprint(request.blueprint)
        if blueprint is None:
            raise ValueError(f"Blueprint '{request.blueprint}' not found.")
        tools = build_run_tool_registry(blueprint, request.tools)
        mcp_clients = await self._connect_mcp_servers(
            tools,
            allowed_names=resolve_run_tool_names(blueprint, request.tools),
        )

        llm = LLMClient(LLMConfig(model=request.model))
        runtime = AgentRuntime(
            llm_client=llm,
            tool_registry=tools,
            context_manager=ContextManager(),
        )

        rule_targets = infer_rule_scope_targets(repo_path, request.description, request.context_links)

        sandbox = SandboxRunner(
            repo_path=repo_path,
            config=sandbox_config,
            use_pool=sandbox_config.mode == SandboxMode.production,
        )
        try:
            await sandbox.start()
            set_runner(sandbox)
            self._emit(
                f"  sandbox=docker{'-pooled' if sandbox.config.mode == SandboxMode.production else ''} ({sandbox.config.mode.value}, network={'on' if sandbox.config.network_enabled else 'off'})"
            )
            sandbox_backend = "docker-pooled" if sandbox.config.mode == SandboxMode.production else "docker"
            sandbox_host_fallback = False
        except Exception as exc:
            if sandbox.config.allow_host_fallback:
                set_runner(None)
                self._emit(f"  sandbox=local-degraded (Docker unavailable: {exc})")
                sandbox_backend = "local"
                sandbox_host_fallback = True
            else:
                raise RuntimeError(
                    f"Docker sandbox unavailable in {sandbox.config.mode.value} mode: {exc}"
                ) from exc

        extra: dict[str, Any] = {
            "branch_base": request.branch_base,
            "context_links": list(request.context_links),
            "priority": request.priority,
            "source": request.source,
            "created_by": request.created_by,
            "repo_overview": build_repo_overview(repo_path),
            "sandbox_mode": sandbox.config.mode.value,
            "sandbox_backend": sandbox_backend,
            "sandbox_network_enabled": sandbox.config.network_enabled,
            "sandbox_host_fallback": sandbox_host_fallback,
        }
        if request.jira:
            extra["jira_ticket"] = request.jira
        if request.max_iterations is not None:
            extra["agent_max_iterations"] = request.max_iterations
        if request.tools:
            extra["task_tools"] = list(request.tools)
        if request.rules_override:
            extra["rules_override"] = list(request.rules_override)

        node_ctx = NodeContext(
            task_description=request.description,
            repo_path=repo_path,
            branch_base=request.branch_base,
            extra=extra,
        )
        if rule_targets:
            node_ctx.extra["rule_scope_targets"] = list(rule_targets)

        hydration_result = await hydrate_context_detailed(
            context_links=request.context_links,
            jira_ticket=request.jira,
            metadata_hooks=build_metadata_hooks(tools),
        )
        if hydration_result.text:
            node_ctx.extra["hydrated_context"] = hydration_result.text
            node_ctx.extra["hydration_trace"] = [
                {
                    "resolver": trace.resolver,
                    "section": trace.section,
                    "item_count": trace.item_count,
                }
                for trace in hydration_result.traces
            ]
            self._emit("  context hydrated")

        try:
            rules_text = self._load_rules_text(
                repo_path,
                request.description,
                request.context_links,
                request.rules_override,
                node_ctx,
            )
            engine = BlueprintEngine(
                runtime=runtime,
                rules_text=rules_text,
                rules_resolver=lambda node, context: self._resolve_rules_for_node(
                    repo_path=repo_path,
                    description=request.description,
                    context_links=request.context_links,
                    overrides=request.rules_override,
                    node=node,
                    context=context,
                ),
            )
            result = await engine.execute(blueprint, node_ctx)
            node_ctx.extra["security_events"] = get_security_events()
            return result
        finally:
            await sandbox.stop()
            set_runner(None)
            for mcp in mcp_clients:
                await mcp.close()

    def _emit(self, message: str) -> None:
        if self._status_callback is not None:
            self._status_callback(message)

    def _load_rules_text(
        self,
        repo_path: str,
        description: str,
        context_links: list[str],
        overrides: list[str],
        context: NodeContext | None = None,
    ) -> str:
        resolution = build_rule_resolution(
            repo_path,
            description,
            context_links,
            overrides,
            context,
        )
        text = resolution["text"]
        ruleset_count = resolution["ruleset_count"]
        rule_targets = resolution["targets"]
        override_count = len([rule for rule in overrides if rule.strip()])
        if ruleset_count:
            self._emit(f"  rules loaded from {ruleset_count} file(s)")
        if rule_targets:
            self._emit(f"  scoped rules resolved for {len(rule_targets)} path(s)")
        else:
            self._emit("  scoped rules resolved from global context only")
        if override_count:
            self._emit(f"  rules overrides applied: {override_count}")
        if context is not None:
            context.extra["rule_scope_targets"] = list(rule_targets)
            context.extra["rule_trace"] = resolution["trace"]
        return text

    def _resolve_rules_for_node(
        self,
        repo_path: str,
        description: str,
        context_links: list[str],
        overrides: list[str],
        node: BlueprintNode,
        context: NodeContext,
    ) -> str:
        resolution = build_rule_resolution(
            repo_path,
            description,
            context_links,
            overrides,
            context,
        )
        context.extra["rule_scope_targets"] = list(resolution["targets"])
        context.extra["rule_trace"] = resolution["trace"]
        context.extra["last_rule_scope_node"] = node.name
        return resolution["text"]

    async def _connect_mcp_servers(self, registry: ToolRegistry, allowed_names: set[str]) -> list[MCPClient]:
        raw = os.environ.get("MCP_SERVERS", "").strip()
        if not raw:
            return []

        clients: list[MCPClient] = []
        for entry in raw.split(","):
            entry = entry.strip()
            parts = entry.split(":", 2)
            if len(parts) < 3:
                logger.warning("mcp_invalid_entry", entry=entry)
                continue

            name, transport, value = parts[0].strip(), parts[1].strip(), parts[2].strip()
            if transport == "stdio":
                config = MCPServerConfig(name=name, transport="stdio", command=shlex.split(value))
            else:
                config = MCPServerConfig(name=name, transport="http", url=f"{transport}:{value}")

            client = MCPClient(config)
            try:
                await client.connect()
                discovered = await client.discover_tools()
                registered_count = 0
                for tool in discovered:
                    if tool.name not in allowed_names:
                        continue
                    registry.register(tool)
                    registered_count += 1
                clients.append(client)
                self._emit(f"  mcp={name} ({registered_count}/{len(discovered)} tools)")
            except Exception as exc:
                logger.warning("mcp_server_skipped", name=name, error=str(exc))

        return clients
