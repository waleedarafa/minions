from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import Callable

import structlog

logger = structlog.get_logger()


@dataclass
class AutofixPattern:
    name: str
    pattern: str  # regex to match in CI output
    fix: Callable[[str, str], bool]  # (repo_path, matched_output) -> success
    description: str = ""


def _run_cmd(cmd: list[str], cwd: str) -> bool:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, cwd=cwd)
        return result.returncode == 0
    except Exception:
        return False


def _fix_ruff(repo_path: str, output: str) -> bool:
    return _run_cmd(["python", "-m", "ruff", "check", ".", "--fix"], repo_path)


def _fix_ruff_format(repo_path: str, output: str) -> bool:
    return _run_cmd(["python", "-m", "ruff", "format", "."], repo_path)


def _fix_trailing_whitespace(repo_path: str, output: str) -> bool:
    return _run_cmd(
        ["grep", "-rl", " $", ".", "--include=*.py"],
        repo_path,
    )


BUILTIN_PATTERNS = [
    AutofixPattern(
        name="ruff_lint",
        pattern=r"ruff check|E[0-9]{3}|F[0-9]{3}",
        fix=_fix_ruff,
        description="Auto-fix ruff lint errors",
    ),
    AutofixPattern(
        name="ruff_format",
        pattern=r"ruff format|would reformat",
        fix=_fix_ruff_format,
        description="Auto-format with ruff",
    ),
]


class AutofixRegistry:
    def __init__(self) -> None:
        self._patterns = list(BUILTIN_PATTERNS)

    def register(self, pattern: AutofixPattern) -> None:
        self._patterns.append(pattern)

    def find_applicable(self, ci_output: str) -> list[AutofixPattern]:
        return [p for p in self._patterns if re.search(p.pattern, ci_output, re.IGNORECASE)]

    def apply_all(self, ci_output: str, repo_path: str) -> list[str]:
        """Apply all matching autofixes. Returns list of applied fix names."""
        applicable = self.find_applicable(ci_output)
        applied = []
        for pattern in applicable:
            logger.info("applying_autofix", name=pattern.name)
            if pattern.fix(repo_path, ci_output):
                applied.append(pattern.name)
                logger.info("autofix_success", name=pattern.name)
            else:
                logger.warning("autofix_failed", name=pattern.name)
        return applied


_registry = AutofixRegistry()


def get_autofix_registry() -> AutofixRegistry:
    return _registry
