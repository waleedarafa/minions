from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import structlog

logger = structlog.get_logger()


@dataclass
class LintResult:
    passed: bool
    output: str
    linter: str
    file_count: int = 0
    error_count: int = 0


HEURISTIC_LINTERS = {
    ".py": ["ruff", "mypy"],
    ".js": ["eslint"],
    ".ts": ["eslint"],
    ".tsx": ["eslint"],
    ".go": ["golint"],
    ".rb": ["rubocop"],
}


def detect_linters(repo_path: str) -> list[str]:
    """Heuristically detect which linters to run based on project files."""
    root = Path(repo_path)
    linters = set()
    extensions = set()
    for f in root.rglob("*"):
        if f.is_file() and not any(
            part.startswith(".") or part in ("node_modules", "__pycache__", "venv")
            for part in f.parts
        ):
            extensions.add(f.suffix)

    for ext in extensions:
        for linter in HEURISTIC_LINTERS.get(ext, []):
            # Only add if linter is available
            try:
                subprocess.run([linter, "--version"], capture_output=True, timeout=5)
                linters.add(linter)
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
    return list(linters)


def run_lint(repo_path: str, linter: str = "ruff", timeout: int = 60) -> LintResult:
    """Run a single linter and return results."""
    if linter == "ruff":
        cmd = ["python", "-m", "ruff", "check", "."]
    elif linter == "mypy":
        cmd = ["python", "-m", "mypy", "."]
    elif linter == "eslint":
        cmd = ["npx", "eslint", "."]
    else:
        cmd = [linter, "."]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=repo_path,
        )
        output = (result.stdout + result.stderr).strip()
        passed = result.returncode == 0
        error_count = output.count("\n") if not passed else 0
        return LintResult(
            passed=passed,
            output=output or ("OK" if passed else "Lint failed"),
            linter=linter,
            error_count=error_count,
        )
    except FileNotFoundError:
        return LintResult(passed=True, output=f"{linter} not found, skipping", linter=linter)
    except subprocess.TimeoutExpired:
        return LintResult(passed=False, output=f"{linter} timed out after {timeout}s", linter=linter)
    except Exception as exc:
        return LintResult(passed=False, output=f"Error running {linter}: {exc}", linter=linter)


def run_all_linters(repo_path: str, timeout: int = 60) -> list[LintResult]:
    """Run all applicable linters for the project."""
    linters = detect_linters(repo_path) or ["ruff"]
    return [run_lint(repo_path, linter, timeout) for linter in linters]
