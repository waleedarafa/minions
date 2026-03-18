from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from src.blueprint.nodes.deterministic import NodeContext

if TYPE_CHECKING:
    from src.agent.runtime import AgentRuntime
    from src.blueprint.schema import BlueprintNode

logger = structlog.get_logger()


def resolve_max_iterations(node: BlueprintNode, ctx: NodeContext) -> int:
    override = ctx.extra.get("agent_max_iterations")
    if isinstance(override, int) and override > 0:
        return override
    return node.max_iterations


def resolve_allowed_tools(node: BlueprintNode, ctx: NodeContext) -> list[str]:
    task_tools = ctx.extra.get("task_tools")
    if not isinstance(task_tools, list) or not task_tools:
        return list(node.tools)
    if not node.tools:
        return [tool for tool in task_tools if isinstance(tool, str)]
    return [tool for tool in node.tools if tool in task_tools]


class AgenticNodeExecutor:
    """Executes agentic (LLM-driven) blueprint nodes."""

    def __init__(self, runtime: AgentRuntime) -> None:
        self._runtime = runtime

    async def execute(self, node: BlueprintNode, ctx: NodeContext, rules: str = "") -> NodeContext:
        """Run the agent for a single agentic node."""
        assert node.prompt is not None

        # Build system prompt with rules
        system_parts = [
            "You are an autonomous coding agent. You implement code changes precisely and carefully.",
            "Follow instructions step by step. Use tools to understand the codebase before making changes.",
        ]
        if rules:
            system_parts.append(f"\n## Project Rules\n{rules}")
        system_parts.append(
            "\nWhen done with your task, provide a brief summary of what you changed."
        )
        system_prompt = "\n".join(system_parts)

        # Build task prompt
        task_prompt = self._build_task_prompt(node, ctx)

        # Filter tools to those allowed for this step
        allowed_tools = resolve_allowed_tools(node, ctx)
        if node.tools or ctx.extra.get("task_tools"):
            from src.agent.tools import ToolRegistry
            filtered = ToolRegistry()
            for t in self._runtime.tool_registry.filter_tools(allowed_tools):
                filtered.register(t)
            # Temporarily swap registry
            original_registry = self._runtime._tools
            self._runtime._tools = filtered
        else:
            original_registry = None

        try:
            result = await self._runtime.run(
                task=task_prompt,
                system_prompt=system_prompt,
                max_iterations=resolve_max_iterations(node, ctx),
            )

            logger.info(
                "agentic_node_complete",
                node=node.name,
                success=result.success,
                iterations=result.iterations,
                tokens=result.tokens_used,
            )

            ctx.extra[f"{node.name}_output"] = result.output
            ctx.extra[f"{node.name}_tokens"] = result.tokens_used
            ctx.extra[f"{node.name}_tool_calls"] = len(result.tool_calls_made)

            if not result.success:
                logger.warning("agentic_node_failed", node=node.name, error=result.error)
                ctx.extra[f"{node.name}_error"] = result.error
                raise RuntimeError(result.error or f"Agentic node '{node.name}' failed")

        finally:
            if original_registry is not None:
                self._runtime._tools = original_registry

        return ctx

    def _build_task_prompt(self, node: BlueprintNode, ctx: NodeContext) -> str:
        parts = [node.prompt]
        if ctx.task_description:
            parts.append(f"\n## Task Description\n{ctx.task_description}")
        if ctx.repo_path:
            parts.append(f"\n## Working Directory\n{ctx.repo_path}")
        if ctx.extra.get("repo_overview"):
            parts.append(f"\n## Repository Overview\n{ctx.extra['repo_overview']}")
        if ctx.branch:
            parts.append(f"\n## Branch\n{ctx.branch}")
        rule_scope_targets = ctx.extra.get("rule_scope_targets")
        rule_trace = ctx.extra.get("rule_trace")
        if isinstance(rule_scope_targets, list) and rule_scope_targets:
            target_lines = "\n".join(f"- {target}" for target in rule_scope_targets[:10])
            parts.append(f"\n## Active Rule Scope\n{target_lines}")
        if isinstance(rule_trace, list) and rule_trace:
            source_lines = []
            for item in rule_trace[:10]:
                if not isinstance(item, dict):
                    continue
                source = item.get("source")
                scope = item.get("scope")
                if source and scope:
                    source_lines.append(f"- {source} ({scope})")
            if source_lines:
                parts.append("\n## Active Rule Sources\n" + "\n".join(source_lines))
        if ctx.extra.get("hydrated_context"):
            parts.append(f"\n## Hydrated Context\n{ctx.extra['hydrated_context']}")
        # Inject plan output for the implement step
        if node.name == "implement" and ctx.extra.get("plan_output"):
            parts.append(f"\n## Implementation Plan\n{ctx.extra['plan_output']}")
        # Pass all available failure output so the LLM sees the full picture
        if ctx.ci_failed:
            summaries = []
            if ctx.extra.get("lint_failures_summary"):
                summaries.append(f"### Lint Summary\n{ctx.extra['lint_failures_summary']}")
            if ctx.extra.get("test_failures_summary"):
                summaries.append(f"### Test Summary\n{ctx.extra['test_failures_summary']}")
            if ctx.extra.get("ci_failures_summary"):
                summaries.append(f"### CI Summary\n{ctx.extra['ci_failures_summary']}")
            if summaries:
                parts.append("\n## Failure Summary\n" + "\n\n".join(summaries))

            sections = []
            if ctx.extra.get("lint_output"):
                sections.append(f"### Lint\n{ctx.extra['lint_output']}")
            if ctx.extra.get("test_output"):
                sections.append(f"### Tests\n{ctx.extra['test_output']}")
            if ctx.extra.get("ci_output"):
                sections.append(f"### CI Logs\n{ctx.extra['ci_output']}")
            if sections:
                parts.append("\n## Verification Failures\n```\n" + "\n\n".join(sections) + "\n```")
        return "\n".join(parts)
