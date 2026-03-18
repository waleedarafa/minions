from __future__ import annotations

import subprocess
from pathlib import Path

import structlog

from src.agent.tools import tool

logger = structlog.get_logger()

_WORKING_DIR: str = "."


def set_working_dir(path: str) -> None:
    global _WORKING_DIR
    _WORKING_DIR = path


@tool(
    name="grep_search",
    description="Search for a regex pattern in files. Returns matching lines with file:line references.",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regex pattern to search for"},
            "path": {"type": "string", "description": "Directory or file to search (default: .)"},
            "include": {"type": "string", "description": "Glob pattern for files to include (e.g. '*.py')"},
            "case_insensitive": {"type": "boolean", "description": "Case insensitive search"},
        },
        "required": ["pattern"],
    },
)
def grep_search(
    pattern: str,
    path: str = ".",
    include: str = "",
    case_insensitive: bool = False,
) -> str:
    search_path = path if Path(path).is_absolute() else str(Path(_WORKING_DIR) / path)
    cmd = ["grep", "-rn", "--color=never"]
    if case_insensitive:
        cmd.append("-i")
    if include:
        cmd.extend(["--include", include])
    cmd.extend([pattern, search_path])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        output = result.stdout.strip()
        if not output:
            return f"No matches found for pattern: {pattern}"
        lines = output.splitlines()
        if len(lines) > 200:
            lines = lines[:200]
            output = "\n".join(lines) + f"\n... (truncated, {len(lines)} lines shown)"
        return output
    except subprocess.TimeoutExpired:
        return "Error: Search timed out after 30 seconds"
    except Exception as exc:
        return f"Error running grep: {exc}"


@tool(
    name="glob_search",
    description="Find files matching a glob pattern.",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Glob pattern (e.g. 'src/**/*.py')"},
            "path": {"type": "string", "description": "Base directory (default: .)"},
        },
        "required": ["pattern"],
    },
)
def glob_search(pattern: str, path: str = ".") -> str:
    base = Path(path) if Path(path).is_absolute() else Path(_WORKING_DIR) / path
    matches = sorted(base.glob(pattern))
    if not matches:
        return f"No files found matching: {pattern}"
    if len(matches) > 500:
        matches = matches[:500]
    return "\n".join(str(m) for m in matches)
