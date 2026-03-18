from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import structlog
from pydantic import BaseModel

logger = structlog.get_logger()


class Provider(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    LITELLM = "litellm"


class LLMConfig(BaseModel):
    provider: Provider = Provider.ANTHROPIC
    model: str = "claude-sonnet-4-20250514"
    api_key: str | None = None
    base_url: str | None = None
    max_retries: int = 3
    timeout: float = 120.0
    temperature: float = 0.0
    max_tokens: int = 4096


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    stop_reason: str = ""


TRANSIENT_EXCEPTIONS = (
    "RateLimitError",
    "APITimeoutError",
    "InternalServerError",
    "APIConnectionError",
    "ServiceUnavailableError",
)


class LLMClient:
    def __init__(self, config: LLMConfig) -> None:
        self._config = config
        self._client: Any = None

    @property
    def config(self) -> LLMConfig:
        return self._config

    def _get_anthropic_client(self) -> Any:
        if self._client is None:
            import anthropic

            kwargs: dict[str, Any] = {}
            if self._config.api_key:
                kwargs["api_key"] = self._config.api_key
            if self._config.base_url:
                kwargs["base_url"] = self._config.base_url
            kwargs["timeout"] = self._config.timeout
            kwargs["max_retries"] = 0  # we handle retries ourselves
            self._client = anthropic.AsyncAnthropic(**kwargs)
        return self._client

    def _get_openai_client(self) -> Any:
        if self._client is None:
            import openai

            kwargs: dict[str, Any] = {}
            if self._config.api_key:
                kwargs["api_key"] = self._config.api_key
            if self._config.base_url:
                kwargs["base_url"] = self._config.base_url
            kwargs["timeout"] = self._config.timeout
            kwargs["max_retries"] = 0
            self._client = openai.AsyncOpenAI(**kwargs)
        return self._client

    def _get_litellm(self) -> Any:
        import litellm

        return litellm

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str | None = None,
    ) -> LLMResponse:
        model = model or self._config.model
        last_error: Exception | None = None

        for attempt in range(self._config.max_retries + 1):
            try:
                if self._config.provider == Provider.ANTHROPIC:
                    return await self._chat_anthropic(messages, tools, model)
                elif self._config.provider == Provider.OPENAI:
                    return await self._chat_openai(messages, tools, model)
                else:
                    return await self._chat_litellm(messages, tools, model)
            except Exception as exc:
                last_error = exc
                if not self._is_transient(exc) or attempt == self._config.max_retries:
                    raise
                delay = min(2**attempt, 30)
                logger.warning(
                    "llm_retry",
                    attempt=attempt + 1,
                    delay=delay,
                    error=str(exc),
                )
                await asyncio.sleep(delay)

        raise last_error  # type: ignore[misc]

    async def stream_chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str | None = None,
    ) -> AsyncIterator[LLMResponse]:
        model = model or self._config.model
        if self._config.provider == Provider.ANTHROPIC:
            async for chunk in self._stream_anthropic(messages, tools, model):
                yield chunk
        elif self._config.provider == Provider.OPENAI:
            async for chunk in self._stream_openai(messages, tools, model):
                yield chunk
        else:
            async for chunk in self._stream_litellm(messages, tools, model):
                yield chunk

    # ── Anthropic ──────────────────────────────────────────────────

    async def _chat_anthropic(
        self, messages: list[dict], tools: list[dict] | None, model: str
    ) -> LLMResponse:
        client = self._get_anthropic_client()

        system_text = ""
        api_messages = []
        for msg in messages:
            if msg["role"] == "system":
                system_text = msg["content"]
            else:
                api_messages.append(self._to_anthropic_message(msg))

        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": self._config.max_tokens,
            "messages": api_messages,
            "temperature": self._config.temperature,
        }
        if system_text:
            kwargs["system"] = system_text
        if tools:
            kwargs["tools"] = self._to_anthropic_tools(tools)

        response = await client.messages.create(**kwargs)
        return self._parse_anthropic_response(response)

    async def _stream_anthropic(
        self, messages: list[dict], tools: list[dict] | None, model: str
    ) -> AsyncIterator[LLMResponse]:
        client = self._get_anthropic_client()

        system_text = ""
        api_messages = []
        for msg in messages:
            if msg["role"] == "system":
                system_text = msg["content"]
            else:
                api_messages.append(self._to_anthropic_message(msg))

        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": self._config.max_tokens,
            "messages": api_messages,
            "temperature": self._config.temperature,
        }
        if system_text:
            kwargs["system"] = system_text
        if tools:
            kwargs["tools"] = self._to_anthropic_tools(tools)

        async with client.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                yield LLMResponse(content=text, model=model)
            final = await stream.get_final_message()
            yield self._parse_anthropic_response(final)

    @staticmethod
    def _to_anthropic_message(msg: dict) -> dict:
        if msg["role"] == "tool":
            return {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": msg.get("tool_call_id", ""),
                        "content": msg["content"],
                    }
                ],
            }
        if msg.get("tool_calls"):
            content: list[dict] = []
            if msg.get("content"):
                content.append({"type": "text", "text": msg["content"]})
            for tc in msg["tool_calls"]:
                content.append(
                    {
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": tc["function"]["name"],
                        "input": json.loads(tc["function"]["arguments"])
                        if isinstance(tc["function"]["arguments"], str)
                        else tc["function"]["arguments"],
                    }
                )
            return {"role": "assistant", "content": content}
        return {"role": msg["role"], "content": msg["content"]}

    @staticmethod
    def _to_anthropic_tools(tools: list[dict]) -> list[dict]:
        return [
            {
                "name": t["function"]["name"],
                "description": t["function"].get("description", ""),
                "input_schema": t["function"].get("parameters", {"type": "object"}),
            }
            for t in tools
        ]

    @staticmethod
    def _parse_anthropic_response(response: Any) -> LLMResponse:
        content_parts: list[str] = []
        tool_calls: list[ToolCall] = []

        for block in response.content:
            if block.type == "text":
                content_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.id,
                        name=block.name,
                        arguments=block.input if isinstance(block.input, dict) else {},
                    )
                )

        return LLMResponse(
            content="\n".join(content_parts),
            tool_calls=tool_calls,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            model=response.model,
            stop_reason=response.stop_reason,
        )

    # ── OpenAI ─────────────────────────────────────────────────────

    async def _chat_openai(
        self, messages: list[dict], tools: list[dict] | None, model: str
    ) -> LLMResponse:
        client = self._get_openai_client()

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": self._config.temperature,
            "max_tokens": self._config.max_tokens,
        }
        if tools:
            kwargs["tools"] = tools

        response = await client.chat.completions.create(**kwargs)
        return self._parse_openai_response(response)

    async def _stream_openai(
        self, messages: list[dict], tools: list[dict] | None, model: str
    ) -> AsyncIterator[LLMResponse]:
        client = self._get_openai_client()

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": self._config.temperature,
            "max_tokens": self._config.max_tokens,
            "stream": True,
        }
        if tools:
            kwargs["tools"] = tools

        stream = await client.chat.completions.create(**kwargs)
        async for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta and delta.content:
                yield LLMResponse(content=delta.content, model=model)

    @staticmethod
    def _parse_openai_response(response: Any) -> LLMResponse:
        choice = response.choices[0]
        message = choice.message
        tool_calls: list[ToolCall] = []

        if message.tool_calls:
            for tc in message.tool_calls:
                args = tc.function.arguments
                tool_calls.append(
                    ToolCall(
                        id=tc.id,
                        name=tc.function.name,
                        arguments=json.loads(args) if isinstance(args, str) else args,
                    )
                )

        usage = response.usage
        return LLMResponse(
            content=message.content or "",
            tool_calls=tool_calls,
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            model=response.model,
            stop_reason=choice.finish_reason or "",
        )

    # ── LiteLLM ────────────────────────────────────────────────────

    async def _chat_litellm(
        self, messages: list[dict], tools: list[dict] | None, model: str
    ) -> LLMResponse:
        litellm = self._get_litellm()

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": self._config.temperature,
            "max_tokens": self._config.max_tokens,
        }
        if tools:
            kwargs["tools"] = tools
        if self._config.api_key:
            kwargs["api_key"] = self._config.api_key
        if self._config.base_url:
            kwargs["api_base"] = self._config.base_url

        response = await litellm.acompletion(**kwargs)
        return self._parse_openai_response(response)

    async def _stream_litellm(
        self, messages: list[dict], tools: list[dict] | None, model: str
    ) -> AsyncIterator[LLMResponse]:
        litellm = self._get_litellm()

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": self._config.temperature,
            "max_tokens": self._config.max_tokens,
            "stream": True,
        }
        if tools:
            kwargs["tools"] = tools
        if self._config.api_key:
            kwargs["api_key"] = self._config.api_key
        if self._config.base_url:
            kwargs["api_base"] = self._config.base_url

        response = await litellm.acompletion(**kwargs)
        async for chunk in response:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta and delta.content:
                yield LLMResponse(content=delta.content, model=model)

    @staticmethod
    def _is_transient(exc: Exception) -> bool:
        exc_type = type(exc).__name__
        return exc_type in TRANSIENT_EXCEPTIONS
