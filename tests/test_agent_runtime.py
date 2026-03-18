"""Tests for the agent runtime."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agent.context import ContextManager
from src.agent.llm_client import LLMResponse, ToolCall
from src.agent.runtime import AgentRuntime
from src.agent.tools import Tool, ToolRegistry
from src.tools.security import SecurityPolicy, set_security_policy


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_tool(name: str = "echo", handler=None) -> Tool:
    if handler is None:
        handler = lambda text="": text  # noqa: E731
    return Tool(
        name=name,
        description=f"A mock {name} tool",
        parameters={"type": "object", "properties": {"text": {"type": "string"}}},
        handler=handler,
    )


@pytest.fixture
def tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(_make_tool("echo"))
    return registry


@pytest.fixture
def context_manager() -> ContextManager:
    return ContextManager(max_tokens=128_000)


@pytest.fixture
def mock_llm_client() -> AsyncMock:
    client = AsyncMock()
    client.config = MagicMock(model="test-model")
    return client


@pytest.fixture
def runtime(
    mock_llm_client: AsyncMock,
    tool_registry: ToolRegistry,
    context_manager: ContextManager,
) -> AgentRuntime:
    set_security_policy(SecurityPolicy(working_dir=".", allow_write=True))
    return AgentRuntime(mock_llm_client, tool_registry, context_manager)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSimpleToolCall:
    async def test_simple_tool_call(self, runtime: AgentRuntime, mock_llm_client: AsyncMock) -> None:
        """LLM returns one tool call, then a final text response."""
        tool_response = LLMResponse(
            content="",
            tool_calls=[
                ToolCall(id="tc_1", name="echo", arguments={"text": "hello"})
            ],
            input_tokens=100,
            output_tokens=50,
            model="test-model",
        )
        final_response = LLMResponse(
            content="Done!",
            tool_calls=[],
            input_tokens=80,
            output_tokens=30,
            model="test-model",
        )
        mock_llm_client.chat.side_effect = [tool_response, final_response]

        result = await runtime.run("Say hello", max_iterations=10)

        assert result.success is True
        assert result.output == "Done!"
        assert len(result.tool_calls_made) == 1
        assert result.tool_calls_made[0]["name"] == "echo"
        assert result.iterations == 2


class TestMaxIterations:
    async def test_max_iterations(self, runtime: AgentRuntime, mock_llm_client: AsyncMock) -> None:
        """Agent stops after reaching max_iterations."""
        # LLM keeps returning tool calls indefinitely
        tool_response = LLMResponse(
            content="",
            tool_calls=[ToolCall(id="tc_1", name="echo", arguments={"text": "loop"})],
            input_tokens=10,
            output_tokens=10,
            model="test-model",
        )
        mock_llm_client.chat.return_value = tool_response

        result = await runtime.run("Loop forever", max_iterations=3)

        assert result.success is False
        assert result.iterations == 3
        assert "maximum iterations" in result.error.lower()


class TestNoToolCalls:
    async def test_no_tool_calls(self, runtime: AgentRuntime, mock_llm_client: AsyncMock) -> None:
        """Agent returns immediately when LLM produces no tool calls."""
        response = LLMResponse(
            content="Here is your answer.",
            tool_calls=[],
            input_tokens=50,
            output_tokens=20,
            model="test-model",
        )
        mock_llm_client.chat.return_value = response

        result = await runtime.run("Answer me")

        assert result.success is True
        assert result.output == "Here is your answer."
        assert result.iterations == 1
        assert result.tool_calls_made == []


class TestToolErrorHandling:
    async def test_tool_error_handling(self, runtime: AgentRuntime, mock_llm_client: AsyncMock) -> None:
        """When a tool raises an error, the error message is sent back to the LLM."""
        # Register a tool that raises
        def bad_tool(**kwargs):
            raise RuntimeError("disk full")

        runtime.tool_registry.register(
            Tool(
                name="bad_tool",
                description="A tool that fails",
                parameters={"type": "object", "properties": {}},
                handler=bad_tool,
            )
        )

        tool_response = LLMResponse(
            content="",
            tool_calls=[ToolCall(id="tc_err", name="bad_tool", arguments={})],
            input_tokens=10,
            output_tokens=10,
            model="test-model",
        )
        final_response = LLMResponse(
            content="I encountered an error.",
            tool_calls=[],
            input_tokens=10,
            output_tokens=10,
            model="test-model",
        )
        mock_llm_client.chat.side_effect = [tool_response, final_response]

        result = await runtime.run("Do something risky", max_iterations=5)

        assert result.success is True
        assert result.iterations == 2
        # Verify the error was communicated back via context
        messages = runtime.context.get_messages()
        tool_msgs = [m for m in messages if m["role"] == "tool"]
        assert any("Error:" in m["content"] for m in tool_msgs)


class TestToolSecurity:
    async def test_blocked_tool_call_returns_error(self, runtime: AgentRuntime, mock_llm_client: AsyncMock) -> None:
        runtime.tool_registry.register(
            Tool(
                name="run_command",
                description="Run commands",
                parameters={"type": "object", "properties": {"command": {"type": "string"}}},
                handler=lambda command="": command,
            )
        )
        set_security_policy(
            SecurityPolicy(
                allowed_commands=["git"],
                working_dir=".",
                allow_write=True,
            )
        )

        tool_response = LLMResponse(
            content="",
            tool_calls=[ToolCall(id="tc_blocked", name="run_command", arguments={"command": "rm -rf /tmp/x"})],
            input_tokens=10,
            output_tokens=10,
            model="test-model",
        )
        final_response = LLMResponse(
            content="I was blocked.",
            tool_calls=[],
            input_tokens=10,
            output_tokens=10,
            model="test-model",
        )
        mock_llm_client.chat.side_effect = [tool_response, final_response]

        result = await runtime.run("Do something risky", max_iterations=5)

        assert result.success is True
        tool_msgs = [m for m in runtime.context.get_messages() if m["role"] == "tool"]
        assert any("Security policy blocked tool call" in m["content"] for m in tool_msgs)

    async def test_shell_control_command_is_blocked(self, runtime: AgentRuntime, mock_llm_client: AsyncMock) -> None:
        runtime.tool_registry.register(
            Tool(
                name="run_command",
                description="Run commands",
                parameters={"type": "object", "properties": {"command": {"type": "string"}}},
                handler=lambda command="": command,
            )
        )
        set_security_policy(
            SecurityPolicy(
                allowed_commands=["python"],
                working_dir=".",
                allow_write=True,
            )
        )

        tool_response = LLMResponse(
            content="",
            tool_calls=[ToolCall(id="tc_shell", name="run_command", arguments={"command": "python -m pytest && echo done"})],
            input_tokens=10,
            output_tokens=10,
            model="test-model",
        )
        final_response = LLMResponse(
            content="Blocked.",
            tool_calls=[],
            input_tokens=10,
            output_tokens=10,
            model="test-model",
        )
        mock_llm_client.chat.side_effect = [tool_response, final_response]

        result = await runtime.run("Do something risky", max_iterations=5)

        assert result.success is True
        tool_msgs = [m for m in runtime.context.get_messages() if m["role"] == "tool"]
        assert any("Security policy blocked tool call" in m["content"] for m in tool_msgs)


class TestTokenBudget:
    async def test_token_budget(self, mock_llm_client: AsyncMock) -> None:
        """Agent respects the token budget by trimming context."""
        # Use a very small budget so messages get trimmed
        ctx = ContextManager(max_tokens=200, reserve_output_tokens=50, chars_per_token=1.0)
        registry = ToolRegistry()
        registry.register(_make_tool("echo"))
        rt = AgentRuntime(mock_llm_client, registry, ctx)

        response = LLMResponse(
            content="ok",
            tool_calls=[],
            input_tokens=10,
            output_tokens=10,
            model="test-model",
        )
        mock_llm_client.chat.return_value = response

        result = await rt.run("x" * 300, max_iterations=2)

        assert result.success is True
        # The context budget property should be max_tokens - reserve_output_tokens
        assert ctx.budget == 150
