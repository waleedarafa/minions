from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog

from src.feedback.autofix import get_autofix_registry
from src.feedback.ci import wait_for_ci
from src.feedback.lint import run_all_linters
from src.feedback.parser import parse_pytest_output, parse_ruff_output, summarize_failures
from src.output.pr import (
    build_context_summary_section,
    build_pr_title,
    build_task_reference_section,
    build_unresolved_issues_section,
    create_github_pr,
    render_pr_body,
)

logger = structlog.get_logger()


def _git_worktree_root(path: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=path,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


@dataclass
class NodeContext:
    """Shared context passed between blueprint nodes."""
    task_description: str = ""
    repo_path: str = "."
    branch: str = ""
    branch_base: str = "main"
    repo: str = ""  # owner/repo
    ci_failed: bool = False
    fix_attempted: bool = False  # True once fix_failures has run
    autofixes_available: bool = False
    changes_after_fix: bool = False
    ci_rounds: int = 0
    max_ci_rounds: int = 2
    pr_url: str = ""
    branch_pushed: bool = False
    last_push_succeeded: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


async def _run(cmd: list[str], cwd: str, timeout: int) -> tuple[str, int]:
    """Run a command locally or inside the active sandbox."""
    from src.sandbox.runner import get_runner, run_local

    runner = get_runner()
    if runner is not None:
        try:
            return await runner.run(" ".join(cmd), timeout=timeout)
        except Exception as exc:
            return f"Error: {exc}", 1
    else:
        return run_local(" ".join(cmd), cwd, timeout)


class DeterministicActionRegistry:
    """Registry of built-in deterministic actions."""

    def __init__(self) -> None:
        self._actions: dict[str, Any] = {}
        self._register_defaults()

    def _register_defaults(self) -> None:
        self._actions = {
            "load_rules_and_tools": self._load_rules_and_tools,
            "run_linters": self._run_linters,
            "run_tests": self._run_tests,
            "reset_ci_status": self._reset_ci_status,
            "mark_fix_attempted": self._mark_fix_attempted,
            "reset_fix_tracking": self._reset_fix_tracking,
            "git_create_branch": self._git_create_branch,
            "git_commit_and_push": self._git_commit_and_push,
            "git_push_and_trigger_ci": self._git_push_and_trigger_ci,
            "wait_for_ci": self._wait_for_ci,
            "apply_autofixes": self._apply_autofixes,
            "create_pull_request": self._create_pull_request,
            "noop": self._noop,
        }

    def get(self, action_name: str):
        return self._actions.get(action_name)

    async def _noop(self, ctx: NodeContext, **kwargs: Any) -> NodeContext:
        logger.info("deterministic_noop")
        return ctx

    async def _reset_ci_status(self, ctx: NodeContext, **kwargs: Any) -> NodeContext:
        """Clear ci_failed before a retry round so gates re-evaluate from scratch."""
        logger.info("ci_status_reset")
        ctx.ci_failed = False
        ctx.extra.pop("lint_output", None)
        ctx.extra.pop("test_output", None)
        return ctx

    async def _mark_fix_attempted(self, ctx: NodeContext, **kwargs: Any) -> NodeContext:
        """Mark that a fix attempt is about to run so re-verify steps can check this."""
        logger.info("fix_attempted_marked")
        ctx.fix_attempted = True
        return ctx

    async def _reset_fix_tracking(self, ctx: NodeContext, **kwargs: Any) -> NodeContext:
        """Clear fix-tracking state before a new feedback phase begins."""
        logger.info("fix_tracking_reset")
        ctx.fix_attempted = False
        ctx.autofixes_available = False
        ctx.changes_after_fix = False
        return ctx

    async def _load_rules_and_tools(self, ctx: NodeContext, **kwargs: Any) -> NodeContext:
        logger.info("loading_rules_and_tools", repo_path=ctx.repo_path)
        # Rules are loaded by the engine before execution; this is a no-op placeholder
        return ctx

    async def _run_linters(self, ctx: NodeContext, timeout: int = 60, **kwargs: Any) -> NodeContext:
        logger.info("running_linters", path=ctx.repo_path)
        results = run_all_linters(ctx.repo_path, timeout=timeout)
        output_sections = [f"## {result.linter}\n{result.output}" for result in results if result.output]
        output = "\n\n".join(output_sections)
        ctx.extra["lint_output"] = output
        failures = []
        for result in results:
            if result.linter == "ruff":
                failures.extend(parse_ruff_output(result.output))
        if failures:
            ctx.extra["lint_failures_summary"] = summarize_failures(failures)
        failing = [
            result for result in results
            if not result.passed and "not found" not in result.output and "No module named" not in result.output
        ]
        if failing:
            logger.warning("lint_failures", output=output[:500])
            ctx.ci_failed = True
        else:
            logger.info("lint_passed")
        return ctx

    async def _run_tests(self, ctx: NodeContext, timeout: int = 60, **kwargs: Any) -> NodeContext:
        logger.info("running_tests", path=ctx.repo_path)

        # Discover test files in the repo
        import glob
        import os
        test_files = glob.glob(os.path.join(ctx.repo_path, "test_*.py")) + \
                     glob.glob(os.path.join(ctx.repo_path, "*_test.py")) + \
                     glob.glob(os.path.join(ctx.repo_path, "tests", "test_*.py"))

        if test_files:
            # Run pytest explicitly on discovered test files
            relative = [os.path.relpath(f, ctx.repo_path) for f in test_files]
            logger.info("running_pytest", test_files=relative)
            # Use a single shell string so install + test run in the same Python env
            files_str = " ".join(relative)
            from src.sandbox.runner import get_runner, run_local
            runner = get_runner()
            if runner is not None:
                output, returncode = await runner.run(
                    f"python -m pytest --tb=short -v {files_str}", timeout=timeout
                )
            else:
                output, returncode = run_local(
                    f"python -m pytest --tb=short -v {files_str}", ctx.repo_path, timeout
                )
        else:
            # No test files — fall back to running main.py as a smoke test
            logger.info("no_test_files_found_running_main")
            output, returncode = await _run(["python", "main.py"], ctx.repo_path, timeout)

        ctx.extra["test_output"] = output
        if returncode != 0:
            logger.warning("test_failures", output=output[:500])
            failures = parse_pytest_output(output)
            if failures:
                ctx.extra["test_failures_summary"] = summarize_failures(failures)
            ctx.ci_failed = True
        else:
            logger.info("tests_passed", output=output[:200])
            # Note: do NOT reset ci_failed here — a prior gate (e.g. linter) may have
            # already set it. Only reset_ci_status clears the flag explicitly.
        return ctx

    async def _git_create_branch(self, ctx: NodeContext, **kwargs: Any) -> NodeContext:
        """Create a new branch for the agent's changes and auto-detect the GitHub repo."""
        # Generate branch name from task description
        slug = re.sub(r"[^a-z0-9]+", "-", ctx.task_description.lower()).strip("-")[:40]
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        branch = f"minion/{slug}-{timestamp}"
        git_root = _git_worktree_root(ctx.repo_path)
        if not git_root:
            logger.warning("git_branch_skipped", reason="no git worktree", repo_path=ctx.repo_path)
            ctx.extra["branch_error"] = "No git worktree found for target path"
            return ctx

        try:
            base_branch = ctx.branch_base or "main"
            subprocess.run(
                ["git", "checkout", base_branch],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=git_root,
            )
            result = subprocess.run(
                ["git", "checkout", "-b", branch],
                capture_output=True, text=True, timeout=30, cwd=git_root,
            )
            if result.returncode == 0:
                ctx.branch = branch
                logger.info("git_branch_created", branch=branch)
            else:
                logger.error("git_branch_failed", stderr=result.stderr[:300])
                ctx.extra["branch_error"] = result.stderr
                return ctx
        except Exception as exc:
            logger.error("git_branch_error", error=str(exc))
            return ctx

        # Auto-detect owner/repo from git remote
        if not ctx.repo:
            try:
                remote = subprocess.run(
                    ["git", "remote", "get-url", "origin"],
                    capture_output=True, text=True, timeout=10, cwd=git_root,
                )
                url = remote.stdout.strip()
                # Handles both https://github.com/owner/repo.git and git@github.com:owner/repo.git
                match = re.search(r"github\.com[:/](.+?/[^/]+?)(?:\.git)?$", url)
                if match:
                    ctx.repo = match.group(1)
                    logger.info("git_repo_detected", repo=ctx.repo)
            except Exception as exc:
                logger.warning("git_repo_detection_failed", error=str(exc))

        return ctx

    async def _git_commit_and_push(self, ctx: NodeContext, **kwargs: Any) -> NodeContext:
        """Stage all changes, commit, and push the branch to origin."""
        if not ctx.branch:
            logger.warning("git_commit_skipped", reason="no branch set")
            return ctx
        git_root = _git_worktree_root(ctx.repo_path)
        if not git_root:
            logger.warning("git_commit_skipped", reason="no git worktree", repo_path=ctx.repo_path)
            ctx.last_push_succeeded = False
            return ctx

        try:
            # Check if there are any changes to commit
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True, text=True, timeout=10, cwd=git_root,
            )
            if not status.stdout.strip():
                logger.info("git_nothing_to_commit")
                ctx.last_push_succeeded = False
                return ctx

            # Stage all changes
            subprocess.run(
                ["git", "add", "-A"],
                capture_output=True, text=True, timeout=10, cwd=git_root,
                check=True,
            )

            # Commit
            message = f"[Minion] {ctx.task_description[:72]}"
            result = subprocess.run(
                ["git", "commit", "-m", message],
                capture_output=True, text=True, timeout=30, cwd=git_root,
            )
            if result.returncode != 0:
                logger.error("git_commit_failed", stderr=result.stderr[:300])
                ctx.extra["commit_error"] = result.stderr
                return ctx
            logger.info("git_committed", message=message)

            # Push
            push = subprocess.run(
                ["git", "push", "-u", "origin", ctx.branch],
                capture_output=True, text=True, timeout=60, cwd=git_root,
            )
            if push.returncode == 0:
                logger.info("git_pushed", branch=ctx.branch)
                ctx.branch_pushed = True
                ctx.last_push_succeeded = True
            else:
                logger.error("git_push_failed", stderr=push.stderr[:300])
                ctx.extra["push_error"] = push.stderr
                ctx.last_push_succeeded = False

        except Exception as exc:
            logger.error("git_commit_and_push_error", error=str(exc))
            ctx.last_push_succeeded = False

        return ctx

    async def _git_push_and_trigger_ci(self, ctx: NodeContext, **kwargs: Any) -> NodeContext:
        if not ctx.branch:
            logger.warning("no_branch_set_for_push")
            return ctx
        git_root = _git_worktree_root(ctx.repo_path)
        if not git_root:
            logger.warning("git_push_skipped", reason="no git worktree", repo_path=ctx.repo_path)
            return ctx
        logger.info("git_push", branch=ctx.branch)
        try:
            result = subprocess.run(
                ["git", "push", "-u", "origin", ctx.branch],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=git_root,
            )
            if result.returncode == 0:
                logger.info("git_push_success")
                ctx.branch_pushed = True
                ctx.last_push_succeeded = True
            else:
                logger.error("git_push_failed", stderr=result.stderr[:300])
                ctx.extra["push_error"] = result.stderr
                ctx.last_push_succeeded = False
        except Exception as exc:
            logger.error("git_push_error", error=str(exc))
            ctx.last_push_succeeded = False
        return ctx

    async def _wait_for_ci(self, ctx: NodeContext, timeout: int = 1800, **kwargs: Any) -> NodeContext:
        if not ctx.branch_pushed or not ctx.last_push_succeeded:
            logger.warning("ci_wait_skipped", reason="branch not pushed in current round")
            return ctx
        if not ctx.repo:
            logger.warning("ci_wait_skipped", reason="repo not set")
            return ctx

        logger.info("waiting_for_ci", repo=ctx.repo, branch=ctx.branch, round=ctx.ci_rounds + 1)
        ctx.ci_rounds += 1
        result = await wait_for_ci(ctx.repo, ctx.branch, timeout=timeout)
        ctx.extra["ci_status"] = result.status
        ctx.extra["ci_conclusion"] = result.conclusion
        ctx.extra["ci_run_id"] = result.run_id
        ctx.extra["ci_summary"] = result.summary
        ctx.extra["ci_output"] = result.logs
        ctx.last_push_succeeded = False

        failures = []
        if result.logs:
            failures.extend(parse_ruff_output(result.logs))
            failures.extend(parse_pytest_output(result.logs))
        if failures:
            ctx.extra["ci_failures_summary"] = summarize_failures(failures)

        if result.success:
            logger.info("ci_passed", run_id=result.run_id)
            ctx.ci_failed = False
            ctx.autofixes_available = False
            return ctx

        logger.warning("ci_failed", run_id=result.run_id, conclusion=result.conclusion)
        ctx.ci_failed = True
        applicable = get_autofix_registry().find_applicable(result.logs)
        ctx.autofixes_available = bool(applicable)
        if applicable:
            ctx.extra["autofix_candidates"] = [pattern.name for pattern in applicable]
        return ctx

    async def _apply_autofixes(self, ctx: NodeContext, **kwargs: Any) -> NodeContext:
        logger.info("applying_autofixes", repo_path=ctx.repo_path)
        try:
            failure_text = "\n\n".join(
                part for part in [
                    ctx.extra.get("ci_output", ""),
                    ctx.extra.get("lint_output", ""),
                    ctx.extra.get("test_output", ""),
                ]
                if part
            )
            applied = get_autofix_registry().apply_all(failure_text, ctx.repo_path)
            ctx.extra["applied_autofixes"] = applied
            ctx.autofixes_available = False
            if applied:
                ctx.changes_after_fix = True
                ctx.fix_attempted = True
                ctx.ci_failed = False
                logger.info("autofixes_applied")
        except Exception as exc:
            logger.error("autofix_error", error=str(exc))
        return ctx

    async def _create_pull_request(self, ctx: NodeContext, template: str = "default", **kwargs: Any) -> NodeContext:
        logger.info("creating_pull_request", branch=ctx.branch, repo=ctx.repo)
        if not ctx.branch or not ctx.repo:
            logger.warning("pr_skipped", reason="no branch or repo set")
            return ctx
        try:
            partial = ctx.ci_failed and ctx.ci_rounds >= ctx.max_ci_rounds
            title = build_pr_title(ctx.task_description)
            if partial:
                title = title.replace("[Minion]", "[Minion][Partial]", 1)

            change_sections = []
            for key, heading in (
                ("implement_output", "Implementation Summary"),
                ("fix_failures_output", "Local Fix Summary"),
                ("handle_ci_failures_output", "CI Fix Summary"),
            ):
                value = ctx.extra.get(key)
                if isinstance(value, str) and value.strip():
                    change_sections.append(f"### {heading}\n{value.strip()}")

            changes_summary = "\n\n".join(change_sections)
            task_reference = build_task_reference_section(
                ctx.task_description,
                source=str(ctx.extra.get("source", "")),
                created_by=str(ctx.extra.get("created_by", "")),
                priority=str(ctx.extra.get("priority", "")),
                branch_base=ctx.branch_base or "main",
                jira_ticket=ctx.extra.get("jira_ticket"),
                context_links=ctx.extra.get("context_links", []),
            )
            context_summary = build_context_summary_section(
                hydrated_context=str(ctx.extra.get("hydrated_context", "")),
                hydration_trace=ctx.extra.get("hydration_trace", []),
                rule_scope_targets=ctx.extra.get("rule_scope_targets", []),
                rule_trace=ctx.extra.get("rule_trace", []),
            )
            unresolved_issues = build_unresolved_issues_section(
                partial=partial,
                ci_summary=str(ctx.extra.get("ci_summary", "")),
                ci_failures_summary=str(ctx.extra.get("ci_failures_summary", "")),
                ci_conclusion=ctx.extra.get("ci_conclusion"),
                ci_run_id=ctx.extra.get("ci_run_id"),
                autofix_candidates=ctx.extra.get("autofix_candidates", []),
                security_events=ctx.extra.get("security_events", []),
            )
            testing_summary = "\n".join([
                f"- [x] Local verification attempted: {'yes' if ctx.fix_attempted or 'lint_output' in ctx.extra or 'test_output' in ctx.extra else 'no'}",
                f"- [x] CI rounds executed: {ctx.ci_rounds}",
                f"- [x] Final CI conclusion: {ctx.extra.get('ci_conclusion', 'not_run')}",
                "- [ ] Change is backward compatible",
            ])
            body = render_pr_body(
                task_description=ctx.task_description,
                changes_summary=changes_summary,
                template_name=template,
                task_reference=task_reference,
                context_summary=context_summary,
                unresolved_issues=unresolved_issues,
                testing_summary=testing_summary,
            )
            labels = list(ctx.extra.get("pr_labels", []))
            if partial and "minion-partial" not in labels:
                labels.append("minion-partial")
            reviewers = list(ctx.extra.get("pr_reviewers", []))
            pr = await create_github_pr(
                repo=ctx.repo,
                head_branch=ctx.branch,
                base_branch=ctx.branch_base or "main",
                task_description=ctx.task_description,
                changes_summary=changes_summary,
                template_name=template,
                body=body,
                title=title,
                labels=labels or None,
                reviewers=reviewers or None,
            )
            ctx.pr_url = pr.get("html_url", "")
            ctx.extra["pr_title"] = title
            ctx.extra["pr_body"] = body
            logger.info("pr_created", url=ctx.pr_url)
        except Exception as exc:
            logger.error("pr_creation_error", error=str(exc))
            ctx.extra["pr_error"] = str(exc)
        return ctx


# Singleton registry
_registry = DeterministicActionRegistry()


def get_action_registry() -> DeterministicActionRegistry:
    return _registry
