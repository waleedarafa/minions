from __future__ import annotations

import subprocess
import uuid

import structlog

logger = structlog.get_logger()


def _git(args: list[str], cwd: str) -> tuple[str, int]:
    result = subprocess.run(
        ["git"] + args,
        capture_output=True,
        text=True,
        timeout=60,
        cwd=cwd,
    )
    return (result.stdout + result.stderr).strip(), result.returncode


def create_branch(repo_path: str, branch_name: str, base: str = "main") -> str:
    """Create and checkout a new branch from base. Returns branch name."""
    _git(["fetch", "origin", base], repo_path)
    _git(["checkout", base], repo_path)
    _git(["pull", "origin", base], repo_path)
    out, code = _git(["checkout", "-b", branch_name], repo_path)
    if code != 0:
        raise RuntimeError(f"Failed to create branch {branch_name}: {out}")
    logger.info("branch_created", branch=branch_name, base=base)
    return branch_name


def stage_and_commit(repo_path: str, message: str, paths: list[str] | None = None) -> str:
    """Stage files and create a commit. Returns commit hash."""
    if paths:
        for p in paths:
            _git(["add", p], repo_path)
    else:
        _git(["add", "-A"], repo_path)

    out, code = _git(["commit", "-m", message], repo_path)
    if code != 0:
        if "nothing to commit" in out.lower():
            logger.info("nothing_to_commit")
            return ""
        raise RuntimeError(f"Commit failed: {out}")

    hash_out, _ = _git(["rev-parse", "HEAD"], repo_path)
    logger.info("committed", hash=hash_out[:7], message=message[:60])
    return hash_out.strip()


def push_branch(repo_path: str, branch: str) -> None:
    """Push branch to origin."""
    out, code = _git(["push", "-u", "origin", branch], repo_path)
    if code != 0:
        raise RuntimeError(f"Push failed: {out}")
    logger.info("pushed", branch=branch)


def make_branch_name(task_description: str, prefix: str = "minion") -> str:
    """Generate a safe, descriptive branch name from a task description."""
    import re
    slug = task_description.lower()[:50]
    slug = re.sub(r"[^a-z0-9 ]", "", slug)
    slug = re.sub(r"\s+", "-", slug.strip())
    short_id = str(uuid.uuid4())[:8]
    return f"{prefix}/{slug}-{short_id}"
