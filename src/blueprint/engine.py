from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable

import structlog

from src.blueprint.nodes.deterministic import NodeContext, get_action_registry
from src.blueprint.schema import Blueprint, BlueprintNode, NodeType

if TYPE_CHECKING:
    from src.agent.runtime import AgentRuntime

logger = structlog.get_logger()


class RunStatus(str, Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    partial = "partial"


@dataclass
class StepResult:
    name: str
    status: str
    duration_seconds: float = 0.0
    error: str | None = None
    tokens_used: dict[str, int] = field(default_factory=dict)


@dataclass
class BlueprintRunResult:
    run_id: str
    blueprint_name: str
    status: RunStatus
    steps: list[StepResult] = field(default_factory=list)
    context: NodeContext = field(default_factory=NodeContext)
    pr_url: str = ""
    total_tokens: int = 0
    duration_seconds: float = 0.0
    error: str | None = None


class BlueprintEngine:
    """State machine executor for blueprints."""

    def __init__(
        self,
        runtime: AgentRuntime,
        rules_text: str = "",
        rules_resolver: Callable[[BlueprintNode, NodeContext], str] | None = None,
    ) -> None:
        self._runtime = runtime
        self._rules = rules_text
        self._rules_resolver = rules_resolver
        self._action_registry = get_action_registry()

    async def execute(
        self,
        blueprint: Blueprint,
        context: NodeContext,
    ) -> BlueprintRunResult:
        run_id = str(uuid.uuid4())
        started_at = time.monotonic()

        logger.info(
            "blueprint_start",
            run_id=run_id,
            blueprint=blueprint.name,
            task=context.task_description[:100],
        )

        result = BlueprintRunResult(
            run_id=run_id,
            blueprint_name=blueprint.name,
            status=RunStatus.running,
            context=context,
        )

        from src.blueprint.nodes.agentic import AgenticNodeExecutor
        agentic_executor = AgenticNodeExecutor(self._runtime)

        for node in blueprint.steps:
            # Check condition
            if not self._check_condition(node, context):
                logger.info("node_skipped", name=node.name, condition=node.condition)
                continue

            # Check hard limits
            if context.ci_rounds >= context.max_ci_rounds and node.name in (
                "handle_ci_failures",
                "second_ci_round",
                "git_push_and_trigger_ci",
            ):
                if context.ci_rounds >= context.max_ci_rounds:
                    logger.warning("max_ci_rounds_reached", rounds=context.ci_rounds)
                    step_result = StepResult(
                        name=node.name,
                        status="skipped",
                        error=f"Max CI rounds ({context.max_ci_rounds}) reached",
                    )
                    result.steps.append(step_result)
                    continue

            node_start = time.monotonic()
            logger.info("node_start", name=node.name, type=node.type)

            try:
                if node.type == NodeType.deterministic:
                    context = await self._execute_deterministic(node, context)
                else:
                    rules = self._rules_resolver(node, context) if self._rules_resolver else self._rules
                    context = await agentic_executor.execute(node, context, rules)

                duration = time.monotonic() - node_start
                step_result = StepResult(
                    name=node.name,
                    status="completed",
                    duration_seconds=duration,
                    tokens_used=context.extra.get(f"{node.name}_tokens", {}),
                )
                result.steps.append(step_result)
                logger.info("node_complete", name=node.name, duration=f"{duration:.1f}s")

            except Exception as exc:
                duration = time.monotonic() - node_start
                logger.error("node_error", name=node.name, error=str(exc), exc_info=True)
                step_result = StepResult(
                    name=node.name,
                    status="failed",
                    duration_seconds=duration,
                    error=str(exc),
                )
                result.steps.append(step_result)
                # Continue to next step rather than aborting (partial success is valuable)

        has_failed_steps = any(step.status == "failed" for step in result.steps)

        if context.ci_failed and context.ci_rounds >= context.max_ci_rounds and context.branch_pushed:
            result.status = RunStatus.partial
        elif context.ci_failed or has_failed_steps:
            result.status = RunStatus.failed
        else:
            result.status = RunStatus.completed
        result.pr_url = context.pr_url
        result.duration_seconds = time.monotonic() - started_at

        # Calculate total tokens
        for step in result.steps:
            if isinstance(step.tokens_used, dict):
                result.total_tokens += step.tokens_used.get("total_tokens", 0)

        logger.info(
            "blueprint_complete",
            run_id=run_id,
            status=result.status,
            duration=f"{result.duration_seconds:.1f}s",
            pr_url=result.pr_url,
            total_tokens=result.total_tokens,
        )
        return result

    async def _execute_deterministic(
        self, node: BlueprintNode, ctx: NodeContext
    ) -> NodeContext:
        assert node.action is not None
        action = self._action_registry.get(node.action)
        if action is None:
            logger.warning("unknown_action", action=node.action)
            return ctx

        timeout_secs = self._parse_timeout(node.timeout)
        kwargs: dict[str, Any] = {}
        if timeout_secs:
            kwargs["timeout"] = timeout_secs
        if node.template:
            kwargs["template"] = node.template

        return await action(ctx, **kwargs)

    def _check_condition(self, node: BlueprintNode, ctx: NodeContext) -> bool:
        if not node.condition:
            return True
        condition = node.condition
        if condition == "ci_failed":
            return ctx.ci_failed
        if condition == "autofixes_available":
            return ctx.autofixes_available
        if condition == "changes_after_fix":
            return ctx.changes_after_fix
        if condition == "ci_rounds_not_exceeded":
            return ctx.ci_rounds < ctx.max_ci_rounds
        if condition == "fix_attempted":
            return ctx.fix_attempted
        if condition == "not_ci_failed":
            return not ctx.ci_failed
        if condition == "branch_pushed":
            return ctx.branch_pushed
        if condition == "last_push_succeeded":
            return ctx.last_push_succeeded
        if condition == "fix_attempted_and_not_ci_failed":
            return ctx.fix_attempted and not ctx.ci_failed
        logger.warning("unknown_condition", condition=condition)
        return True

    @staticmethod
    def _parse_timeout(timeout_str: str | None) -> int | None:
        if not timeout_str:
            return None
        s = timeout_str.lower().strip()
        if s.endswith("s"):
            return int(s[:-1])
        if s.endswith("m"):
            return int(s[:-1]) * 60
        return int(s)
