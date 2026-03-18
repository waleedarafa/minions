from __future__ import annotations

import subprocess

import structlog

from src.agent.tools import tool

logger = structlog.get_logger()

_WORKING_DIR: str = "."


def set_working_dir(path: str) -> None:
    global _WORKING_DIR
    _WORKING_DIR = path


def _run_git(args: list[str], cwd: str | None = None) -> str:
    cwd = cwd or _WORKING_DIR
    try:
        result = subprocess.run(
            ["git"] + args,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=cwd,
        )
        if result.returncode != 0 and result.stderr:
            return f"git error: {result.stderr.strip()}"
        return result.stdout.strip() or result.stderr.strip()
    except subprocess.TimeoutExpired:
        return "Error: git command timed out"
    except Exception as exc:
        return f"Error running git: {exc}"


@tool(
    name="git_diff",
    description="Show git diff of current changes.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Repository path"},
            "staged": {"type": "boolean", "description": "Show staged changes only"},
        },
    },
)
def git_diff(path: str = ".", staged: bool = False) -> str:
    args = ["diff"]
    if staged:
        args.append("--staged")
    return _run_git(args, cwd=path)


@tool(
    name="git_log",
    description="Show git log of recent commits.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Repository path"},
            "n": {"type": "integer", "description": "Number of commits to show (default: 10)"},
        },
    },
)
def git_log(path: str = ".", n: int = 10) -> str:
    return _run_git(["log", f"-{n}", "--oneline", "--decorate"], cwd=path)


@tool(
    name="git_blame",
    description="Show git blame for a file.",
    parameters={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "File to blame"},
        },
        "required": ["file_path"],
    },
)
def git_blame(file_path: str) -> str:
    return _run_git(["blame", "--line-porcelain", file_path])


@tool(
    name="git_status",
    description="Show current git status.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Repository path"},
        },
    },
)
def git_status(path: str = ".") -> str:
    return _run_git(["status", "--short"], cwd=path)


@tool(
    name="git_branch",
    description="List git branches.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Repository path"},
            "all": {"type": "boolean", "description": "Show all branches including remote"},
        },
    },
)
def git_branch(path: str = ".", all: bool = False) -> str:
    args = ["branch", "-v"]
    if all:
        args.append("-a")
    return _run_git(args, cwd=path)
