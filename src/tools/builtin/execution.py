from __future__ import annotations

import asyncio

import structlog

from src.agent.tools import tool

logger = structlog.get_logger()

_WORKING_DIR: str = "."
_DEFAULT_TIMEOUT: int = 120


def set_working_dir(path: str) -> None:
    global _WORKING_DIR
    _WORKING_DIR = path


def _run(cmd: str, cwd: str, timeout: int) -> str:
    from src.sandbox.runner import get_runner, run_local

    runner = get_runner()
    if runner is not None:
        try:
            output, _ = asyncio.run(runner.run(cmd, timeout=timeout))
            return output
        except Exception as exc:
            return f"Error: {exc}"
    else:
        output, _ = run_local(cmd, cwd, timeout)
        return output


@tool(
    name="run_command",
    description="Run a shell command in the working directory.",
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to run"},
            "cwd": {"type": "string", "description": "Working directory (default: project root)"},
            "timeout": {"type": "integer", "description": "Timeout in seconds (default: 120)"},
        },
        "required": ["command"],
    },
)
def run_command(command: str, cwd: str = ".", timeout: int = _DEFAULT_TIMEOUT) -> str:
    work_dir = cwd if cwd != "." else _WORKING_DIR
    logger.info("run_command", command=command, cwd=work_dir)
    return _run(command, work_dir, timeout)


@tool(
    name="run_tests",
    description="Run the project test suite.",
    parameters={
        "type": "object",
        "properties": {
            "test_path": {"type": "string", "description": "Path to test file or directory"},
            "framework": {"type": "string", "description": "Test framework: pytest, unittest, jest, etc."},
            "args": {"type": "string", "description": "Extra arguments to pass to the test runner"},
        },
    },
)
def run_tests(test_path: str = "", framework: str = "pytest", args: str = "") -> str:
    if framework == "pytest":
        cmd = f"python -m pytest {test_path} {args} -v --tb=short"
    elif framework == "unittest":
        cmd = f"python -m unittest {test_path or 'discover'} {args}"
    elif framework == "jest":
        cmd = f"npx jest {test_path} {args}"
    else:
        cmd = f"{framework} {test_path} {args}"
    return _run(cmd.strip(), _WORKING_DIR, timeout=300)


@tool(
    name="run_linter",
    description="Run a linter on the codebase.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to lint"},
            "linter": {"type": "string", "description": "Linter to use: ruff, flake8, eslint, etc."},
            "fix": {"type": "boolean", "description": "Auto-fix issues where possible"},
        },
    },
)
def run_linter(path: str = ".", linter: str = "ruff", fix: bool = False) -> str:
    if linter == "ruff":
        cmd = f"ruff check {path}" + (" --fix" if fix else "")
    elif linter == "flake8":
        cmd = f"flake8 {path}"
    elif linter == "eslint":
        cmd = f"npx eslint {path}" + (" --fix" if fix else "")
    elif linter == "mypy":
        cmd = f"python -m mypy {path}"
    else:
        cmd = f"{linter} {path}"
    return _run(cmd, _WORKING_DIR, timeout=60)
