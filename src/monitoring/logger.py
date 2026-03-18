from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

import structlog

logger = structlog.get_logger()


class RunLogger:
    """Structured logger that records every step, tool call, and LLM
    interaction for a single agent run and persists the transcript to disk."""

    def __init__(self, run_id: str, output_dir: str = ".minion/runs") -> None:
        self._run_id = run_id
        self._output_dir = output_dir
        self._transcript: list[dict[str, Any]] = []

    @property
    def run_id(self) -> str:
        return self._run_id

    # -- Logging helpers -----------------------------------------------------

    def log_step(
        self,
        step_name: str,
        status: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        """Record a blueprint step execution."""
        entry: dict[str, Any] = {
            "type": "step",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": self._run_id,
            "step_name": step_name,
            "status": status,
            "data": data or {},
        }
        self._transcript.append(entry)
        logger.info("run_step", run_id=self._run_id, step=step_name, status=status)

    def log_tool_call(
        self,
        tool_name: str,
        args: dict[str, Any],
        result: Any,
    ) -> None:
        """Record a tool invocation and its result."""
        entry: dict[str, Any] = {
            "type": "tool_call",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": self._run_id,
            "tool_name": tool_name,
            "args": args,
            "result": str(result),
        }
        self._transcript.append(entry)
        logger.debug("run_tool_call", run_id=self._run_id, tool=tool_name)

    def log_llm_call(
        self,
        messages: list[dict[str, Any]],
        response: str,
        tokens: int,
    ) -> None:
        """Record an LLM interaction (prompt + response summary)."""
        entry: dict[str, Any] = {
            "type": "llm_call",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": self._run_id,
            "messages_count": len(messages),
            "response_preview": response[:500] if response else "",
            "tokens": tokens,
        }
        self._transcript.append(entry)
        logger.debug("run_llm_call", run_id=self._run_id, tokens=tokens)

    # -- Persistence ---------------------------------------------------------

    def save(self) -> str:
        """Persist the full transcript to disk as JSON.  Returns the file path."""
        os.makedirs(self._output_dir, exist_ok=True)
        path = os.path.join(self._output_dir, f"{self._run_id}.json")
        with open(path, "w") as fh:
            json.dump(
                {"run_id": self._run_id, "transcript": self._transcript},
                fh,
                indent=2,
            )
        logger.info("run_log_saved", path=path, entries=len(self._transcript))
        return path

    def get_transcript(self) -> list[dict[str, Any]]:
        """Return a copy of the full run transcript."""
        return list(self._transcript)
