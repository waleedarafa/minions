from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import structlog

from src.agent.context import ContextManager, Message
from src.agent.llm_client import LLMClient, LLMResponse
from src.agent.tools import ToolExecutor, ToolRegistry

logger = structlog.get_logger()


@dataclass
class AgentResult:
    success: bool
    output: str
    tool_calls_made: list[dict[str, Any]] = field(default_factory=list)
    tokens_used: dict[str, int] = field(default_factory=dict)
    iterations: int = 0
    error: str | None = None


StreamCallback = Callable[[str], Any]


class AgentRuntime:
    def __init__(
        self,
        llm_client: LLMClient,
        tool_registry: ToolRegistry,
        context_manager: ContextManager,
    ) -> None:
        self._llm = llm_client
        self._tools = tool_registry
        self._executor = ToolExecutor(tool_registry)
        self._context = context_manager

    @property
    def context(self) -> ContextManager:
        return self._context

    @property
    def tool_registry(self) -> ToolRegistry:
        return self._tools

    async def run(
        self,
        task: str,
        system_prompt: str = "You are a helpful assistant.",
        max_iterations: int = 50,
        stream_callback: StreamCallback | None = None,
    ) -> AgentResult:
        self._context.clear()
        self._context.add_message(Message(role="system", content=system_prompt))
        self._context.add_message(Message(role="user", content=task))

        tool_schemas = self._tools.to_openai_schemas() or None
        tool_calls_made: list[dict[str, Any]] = []
        final_output = ""

        for iteration in range(1, max_iterations + 1):
            logger.info("agent_iteration", iteration=iteration, max=max_iterations)

            self._context.trim_to_budget()

            try:
                if stream_callback:
                    response = await self._stream_iteration(tool_schemas, stream_callback)
                else:
                    messages_snapshot = self._context.get_messages()
                    logger.debug(
                        "llm_request",
                        iteration=iteration,
                        messages=[{"role": m["role"], "content": str(m.get("content") or "")[:500]} for m in messages_snapshot],
                    )
                    response = await self._llm.chat(
                        messages=messages_snapshot,
                        tools=tool_schemas,
                    )
                    logger.debug(
                        "llm_response",
                        iteration=iteration,
                        content=(response.content or "")[:500],
                        tool_calls=[{"name": tc.name, "arguments": tc.arguments} for tc in (response.tool_calls or [])],
                    )
            except Exception as exc:
                logger.error("llm_error", error=str(exc))
                return AgentResult(
                    success=False,
                    output="",
                    tool_calls_made=tool_calls_made,
                    tokens_used=self._token_summary(),
                    iterations=iteration,
                    error=f"LLM error: {exc}",
                )

            self._context.record_usage(response.input_tokens, response.output_tokens)

            if not response.tool_calls:
                final_output = response.content
                self._context.add_message(
                    Message(role="assistant", content=response.content)
                )
                logger.info("agent_complete", iterations=iteration)
                return AgentResult(
                    success=True,
                    output=final_output,
                    tool_calls_made=tool_calls_made,
                    tokens_used=self._token_summary(),
                    iterations=iteration,
                )

            # Record assistant message with tool calls
            assistant_tool_calls = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments),
                    },
                }
                for tc in response.tool_calls
            ]
            self._context.add_message(
                Message(
                    role="assistant",
                    content=response.content,
                    tool_calls=assistant_tool_calls,
                )
            )

            for tc in response.tool_calls:
                tool_calls_made.append({"name": tc.name, "arguments": tc.arguments})
                logger.info("tool_call", name=tc.name, arguments=tc.arguments)

                result = await self._executor.execute(tc.name, tc.arguments)

                content = result.output if result.success else f"Error: {result.error}"
                self._context.add_message(
                    Message(role="tool", content=content, tool_call_id=tc.id)
                )

        logger.warning("agent_max_iterations", max_iterations=max_iterations)
        return AgentResult(
            success=False,
            output=final_output,
            tool_calls_made=tool_calls_made,
            tokens_used=self._token_summary(),
            iterations=max_iterations,
            error=f"Reached maximum iterations ({max_iterations})",
        )

    async def _stream_iteration(
        self,
        tool_schemas: list[dict] | None,
        callback: StreamCallback,
    ) -> LLMResponse:
        final_response: LLMResponse | None = None
        async for chunk in self._llm.stream_chat(
            messages=self._context.get_messages(),
            tools=tool_schemas,
        ):
            if chunk.content and not chunk.tool_calls and not chunk.input_tokens:
                callback(chunk.content)
            if chunk.input_tokens or chunk.tool_calls:
                final_response = chunk

        if final_response is None:
            return LLMResponse(content="", model=self._llm.config.model)
        return final_response

    def _token_summary(self) -> dict[str, int]:
        usage = self._context.usage
        return {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "total_tokens": usage.total,
        }
