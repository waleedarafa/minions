from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import structlog

logger = structlog.get_logger()

MessageRole = Literal["system", "user", "assistant", "tool"]


@dataclass
class Message:
    role: MessageRole
    content: str
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict] | None = None


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


class ContextManager:
    def __init__(
        self,
        max_tokens: int = 128_000,
        reserve_output_tokens: int = 4_096,
        chars_per_token: float = 4.0,
    ) -> None:
        self._messages: list[Message] = []
        self._max_tokens = max_tokens
        self._reserve_output_tokens = reserve_output_tokens
        self._chars_per_token = chars_per_token
        self._usage = TokenUsage()

    @property
    def budget(self) -> int:
        return self._max_tokens - self._reserve_output_tokens

    @property
    def usage(self) -> TokenUsage:
        return self._usage

    @property
    def messages(self) -> list[Message]:
        return list(self._messages)

    def add_message(self, message: Message) -> None:
        self._messages.append(message)
        logger.debug(
            "message_added",
            role=message.role,
            estimated_tokens=self.estimate_tokens(message.content),
            total_messages=len(self._messages),
        )

    def get_messages(self) -> list[dict]:
        return [self._message_to_dict(m) for m in self._messages]

    def estimate_tokens(self, text: str) -> int:
        if not text:
            return 0
        return max(1, int(len(text) / self._chars_per_token))

    def estimate_total_tokens(self) -> int:
        total = 0
        for msg in self._messages:
            total += self.estimate_tokens(msg.content)
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    total += self.estimate_tokens(str(tc))
        return total

    def trim_to_budget(self) -> int:
        estimated = self.estimate_total_tokens()
        if estimated <= self.budget:
            return 0

        trimmed = 0
        system_messages = [m for m in self._messages if m.role == "system"]
        non_system = [m for m in self._messages if m.role != "system"]

        while non_system and self._estimate_messages_tokens(system_messages + non_system) > self.budget:
            removed = non_system.pop(0)
            trimmed += 1
            logger.debug("message_trimmed", role=removed.role, trimmed_count=trimmed)

            # If we removed an assistant message with tool_calls, also remove the
            # corresponding tool result messages that follow.
            while non_system and non_system[0].role == "tool":
                non_system.pop(0)
                trimmed += 1

        self._messages = system_messages + non_system
        logger.info("context_trimmed", messages_removed=trimmed, remaining=len(self._messages))
        return trimmed

    def record_usage(self, input_tokens: int, output_tokens: int) -> None:
        self._usage.input_tokens += input_tokens
        self._usage.output_tokens += output_tokens

    def clear(self) -> None:
        self._messages.clear()
        self._usage = TokenUsage()

    def _estimate_messages_tokens(self, messages: list[Message]) -> int:
        total = 0
        for msg in messages:
            total += self.estimate_tokens(msg.content)
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    total += self.estimate_tokens(str(tc))
            total += 4  # overhead per message (role, separators)
        return total

    @staticmethod
    def _message_to_dict(msg: Message) -> dict:
        d: dict = {"role": msg.role, "content": msg.content}
        if msg.name is not None:
            d["name"] = msg.name
        if msg.tool_call_id is not None:
            d["tool_call_id"] = msg.tool_call_id
        if msg.tool_calls is not None:
            d["tool_calls"] = msg.tool_calls
        return d
