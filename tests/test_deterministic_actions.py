from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.blueprint.nodes.deterministic import NodeContext, get_action_registry


@pytest.mark.asyncio
async def test_git_create_branch_checks_out_branch_base(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["git", "rev-parse", "--show-toplevel"]:
            return SimpleNamespace(returncode=0, stdout="/repo/root\n", stderr="")
        if cmd[:4] == ["git", "remote", "get-url", "origin"]:
            return SimpleNamespace(returncode=0, stdout="git@github.com:org/repo.git\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("src.blueprint.nodes.deterministic.subprocess.run", fake_run)

    ctx = NodeContext(task_description="Add feature", repo_path=".", branch_base="develop")
    result = await get_action_registry()._git_create_branch(ctx)

    assert result.branch.startswith("minion/")
    assert calls[0] == ["git", "rev-parse", "--show-toplevel"]
    assert calls[1] == ["git", "checkout", "develop"]
    assert calls[2][0:3] == ["git", "checkout", "-b"]


@pytest.mark.asyncio
async def test_create_pull_request_uses_branch_base(monkeypatch):
    created = {}

    async def fake_create_github_pr(
        repo,
        head_branch,
        base_branch,
        task_description,
        changes_summary="",
        template_name="default",
        body=None,
        title=None,
        labels=None,
        reviewers=None,
    ):
        created["repo"] = repo
        created["head"] = head_branch
        created["base"] = base_branch
        created["title"] = title
        created["changes_summary"] = changes_summary
        created["body"] = body
        created["labels"] = labels
        created["reviewers"] = reviewers
        return {"html_url": "https://example.com/pr/1"}

    monkeypatch.setattr("src.blueprint.nodes.deterministic.create_github_pr", fake_create_github_pr)

    ctx = NodeContext(
        task_description="Ship fix",
        repo_path=".",
        repo="org/repo",
        branch="minion/ship-fix",
        branch_base="release",
    )

    result = await get_action_registry()._create_pull_request(ctx)

    assert result.pr_url == "https://example.com/pr/1"
    assert created["base"] == "release"
    assert created["title"] == "[Minion] Ship fix"
    assert "## Task Reference" in created["body"]
    assert "Base Branch: release" in created["body"]


@pytest.mark.asyncio
async def test_create_pull_request_marks_partial_runs(monkeypatch):
    created = {}

    async def fake_create_github_pr(
        repo,
        head_branch,
        base_branch,
        task_description,
        changes_summary="",
        template_name="default",
        body=None,
        title=None,
        labels=None,
        reviewers=None,
    ):
        created["title"] = title
        created["changes_summary"] = changes_summary
        created["body"] = body
        created["labels"] = labels
        created["reviewers"] = reviewers
        return {"html_url": "https://example.com/pr/2"}

    monkeypatch.setattr("src.blueprint.nodes.deterministic.create_github_pr", fake_create_github_pr)

    ctx = NodeContext(
        task_description="Ship partial fix",
        repo_path=".",
        repo="org/repo",
        branch="minion/ship-partial-fix",
        branch_base="main",
        ci_failed=True,
        ci_rounds=2,
        max_ci_rounds=2,
        extra={
            "source": "api",
            "created_by": "engineer@example.com",
            "priority": "high",
            "context_links": ["https://github.com/org/repo/issues/123"],
            "jira_ticket": "PROJ-123",
            "hydrated_context": "### GitHub Issue\n- #123: tighten PR handoff",
            "hydration_trace": [{"resolver": "context_links", "section": "linked_context", "item_count": 1}],
            "rule_scope_targets": ["src/output/pr.py"],
            "rule_trace": [{"source": "src/.minion/rules.yaml", "scope": "src/**"}],
            "handle_ci_failures_output": "Tried a targeted retry fix.",
            "ci_summary": "CI run 999 job summary:\n- integration-tests: failure",
            "ci_failures_summary": "Found 1 failure(s):\n  - [AssertionError] test_retry",
            "ci_conclusion": "failure",
            "ci_run_id": "999",
            "autofix_candidates": ["ruff_lint"],
            "security_events": [{"reason": "shell_control", "tool_name": "run_command", "command": "pytest && rm -rf /"}],
            "pr_reviewers": ["reviewer1"],
        },
    )

    result = await get_action_registry()._create_pull_request(ctx)

    assert result.pr_url == "https://example.com/pr/2"
    assert created["title"] == "[Minion][Partial] Ship partial fix"
    assert created["labels"] == ["minion-partial"]
    assert created["reviewers"] == ["reviewer1"]
    assert "### CI Fix Summary" in created["changes_summary"]
    assert "## Task Reference" in created["body"]
    assert "Source: api" in created["body"]
    assert "Created By: engineer@example.com" in created["body"]
    assert "Linked Context: https://github.com/org/repo/issues/123" in created["body"]
    assert "## Context" in created["body"]
    assert "### Active Rule Scope" in created["body"]
    assert "### Hydration Trace" in created["body"]
    assert "## Unresolved Issues" in created["body"]
    assert "Partial Success" in created["body"]
    assert "Final CI Summary" in created["body"]
    assert "Remaining CI Failures" in created["body"]
    assert "Blocked Tool or Security Events" in created["body"]


@pytest.mark.asyncio
async def test_git_commit_and_push_marks_branch_as_pushed(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["git", "rev-parse", "--show-toplevel"]:
            return SimpleNamespace(returncode=0, stdout="/repo/root\n", stderr="")
        if cmd[:3] == ["git", "status", "--porcelain"]:
            return SimpleNamespace(returncode=0, stdout="M src/app.py\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("src.blueprint.nodes.deterministic.subprocess.run", fake_run)

    ctx = NodeContext(task_description="Ship fix", repo_path=".", branch="minion/ship-fix")
    result = await get_action_registry()._git_commit_and_push(ctx)

    assert result.branch_pushed is True
    assert result.last_push_succeeded is True
    assert ["git", "push", "-u", "origin", "minion/ship-fix"] in calls


@pytest.mark.asyncio
async def test_wait_for_ci_sets_failure_state_and_autofixes(monkeypatch):
    from src.feedback.ci import CIResult

    async def fake_wait_for_ci(repo, branch, sha=None, poll_interval=30, timeout=1800):
        return CIResult(
            success=False,
            status="completed",
            conclusion="failure",
            run_id="123",
            summary="CI run 123 job summary:\n- unit-tests: failure",
            logs="src/app.py:1:1: F401 imported but unused\nFAILED tests/test_app.py::test_ok",
        )

    monkeypatch.setattr("src.blueprint.nodes.deterministic.wait_for_ci", fake_wait_for_ci)

    ctx = NodeContext(
        task_description="Fix CI",
        repo_path=".",
        repo="org/repo",
        branch="minion/fix-ci",
        branch_pushed=True,
        last_push_succeeded=True,
    )
    result = await get_action_registry()._wait_for_ci(ctx, timeout=10)

    assert result.ci_rounds == 1
    assert result.ci_failed is True
    assert result.autofixes_available is True
    assert result.extra["ci_run_id"] == "123"
    assert "unit-tests" in result.extra["ci_summary"]
    assert "Found" in result.extra["ci_failures_summary"]


@pytest.mark.asyncio
async def test_apply_autofixes_uses_registry(monkeypatch):
    class FakeRegistry:
        def apply_all(self, ci_output: str, repo_path: str) -> list[str]:
            assert "F401" in ci_output
            assert repo_path == "."
            return ["ruff_lint"]

    monkeypatch.setattr("src.blueprint.nodes.deterministic.get_autofix_registry", lambda: FakeRegistry())

    ctx = NodeContext(
        task_description="Fix lint",
        repo_path=".",
        ci_failed=True,
        autofixes_available=True,
        extra={"ci_output": "src/app.py:1:1: F401 imported but unused"},
    )
    result = await get_action_registry()._apply_autofixes(ctx)

    assert result.fix_attempted is True
    assert result.changes_after_fix is True
    assert result.ci_failed is False
    assert result.extra["applied_autofixes"] == ["ruff_lint"]
