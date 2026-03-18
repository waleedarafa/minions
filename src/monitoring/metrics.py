from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from typing import Any

import structlog

logger = structlog.get_logger()

# Cost table: USD per 1 M tokens (blended input/output estimate)
_COST_PER_M_TOKENS: dict[str, float] = {
    "claude-sonnet-4-20250514": 3.0,
    "claude-opus-4-20250514": 15.0,
    "gpt-4o": 5.0,
    "gpt-4o-mini": 0.15,
}


@dataclass
class RunRecord:
    run_id: str
    success: bool
    tokens: int
    duration: float
    ci_rounds: int


class MetricsCollector:
    """Collects per-run metrics with optional file-based persistence."""

    def __init__(self, persist_path: str | None = None) -> None:
        self._records: list[RunRecord] = []
        self._lock = threading.Lock()
        self._persist_path = persist_path

        if persist_path and os.path.isfile(persist_path):
            try:
                with open(persist_path) as fh:
                    data = json.load(fh)
                self._records = [RunRecord(**r) for r in data]
                logger.info("metrics_loaded", count=len(self._records))
            except Exception as exc:
                logger.warning("metrics_load_error", error=str(exc))

    # -- Recording -----------------------------------------------------------

    def record_run(
        self,
        run_id: str,
        success: bool,
        tokens: int,
        duration: float,
        ci_rounds: int = 0,
    ) -> None:
        record = RunRecord(
            run_id=run_id,
            success=success,
            tokens=tokens,
            duration=duration,
            ci_rounds=ci_rounds,
        )
        with self._lock:
            self._records.append(record)
        self._persist()
        logger.info(
            "metric_recorded",
            run_id=run_id,
            success=success,
            tokens=tokens,
            duration=round(duration, 2),
        )

    # -- Summaries -----------------------------------------------------------

    def get_summary(self) -> dict[str, Any]:
        """Return aggregate statistics across all recorded runs."""
        with self._lock:
            records = list(self._records)

        if not records:
            return {
                "total_runs": 0,
                "success_rate": 0.0,
                "avg_tokens": 0,
                "avg_duration": 0.0,
                "avg_ci_rounds": 0.0,
                "total_tokens": 0,
            }

        n = len(records)
        successes = sum(1 for r in records if r.success)
        total_tokens = sum(r.tokens for r in records)
        total_duration = sum(r.duration for r in records)
        total_ci = sum(r.ci_rounds for r in records)

        return {
            "total_runs": n,
            "success_rate": round(successes / n, 4),
            "avg_tokens": total_tokens // n,
            "avg_duration": round(total_duration / n, 2),
            "avg_ci_rounds": round(total_ci / n, 2),
            "total_tokens": total_tokens,
        }

    @staticmethod
    def get_cost_estimate(tokens: int, model: str) -> float:
        """Estimate cost in USD for the given token count and model."""
        rate = _COST_PER_M_TOKENS.get(model, 3.0)
        return round(tokens * rate / 1_000_000, 6)

    # -- Persistence ---------------------------------------------------------

    def _persist(self) -> None:
        if not self._persist_path:
            return
        try:
            os.makedirs(os.path.dirname(self._persist_path) or ".", exist_ok=True)
            with self._lock:
                data = [
                    {
                        "run_id": r.run_id,
                        "success": r.success,
                        "tokens": r.tokens,
                        "duration": r.duration,
                        "ci_rounds": r.ci_rounds,
                    }
                    for r in self._records
                ]
            with open(self._persist_path, "w") as fh:
                json.dump(data, fh, indent=2)
        except Exception as exc:
            logger.warning("metrics_persist_error", error=str(exc))
