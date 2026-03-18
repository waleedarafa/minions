from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()

_TEMPLATE_DIR = Path(__file__).parent / "templates"


def load_template(name: str = "default") -> str:
    path = _TEMPLATE_DIR / f"{name}.md"
    if not path.exists():
        path = _TEMPLATE_DIR / "default.md"
    return path.read_text()


def _truncate(text: str, max_length: int = 600) -> str:
    text = text.strip()
    if len(text) <= max_length:
        return text
    return text[: max_length - 3].rstrip() + "..."


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    items: list[str] = []
    for value in values:
        normalized = value.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        items.append(normalized)
    return items


def _format_bullets(items: list[str]) -> str:
    if not items:
        return "- None"
    return "\n".join(f"- {item}" for item in items)


def build_task_reference_section(
    task_description: str,
    *,
    source: str = "",
    created_by: str = "",
    priority: str = "",
    branch_base: str = "",
    jira_ticket: str | None = None,
    context_links: list[str] | None = None,
) -> str:
    items = [f"Task: {task_description}"]
    if source:
        items.append(f"Source: {source}")
    if created_by:
        items.append(f"Created By: {created_by}")
    if priority:
        items.append(f"Priority: {priority}")
    if branch_base:
        items.append(f"Base Branch: {branch_base}")
    if jira_ticket:
        jira_base = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
        if jira_base:
            items.append(f"Jira: {jira_ticket} ({jira_base}/browse/{jira_ticket})")
        else:
            items.append(f"Jira: {jira_ticket}")
    for link in _dedupe(context_links or [])[:10]:
        items.append(f"Linked Context: {link}")
    return _format_bullets(items)


def build_context_summary_section(
    *,
    hydrated_context: str = "",
    hydration_trace: list[dict[str, Any]] | None = None,
    rule_scope_targets: list[str] | None = None,
    rule_trace: list[dict[str, Any]] | None = None,
) -> str:
    sections: list[str] = []
    targets = _dedupe(rule_scope_targets or [])
    if targets:
        sections.append("### Active Rule Scope\n" + _format_bullets(targets[:10]))

    if rule_trace:
        rule_sources: list[str] = []
        for item in rule_trace[:10]:
            source = str(item.get("source", "")).strip()
            scope = str(item.get("scope", "")).strip()
            if source and scope:
                rule_sources.append(f"{source} ({scope})")
        if rule_sources:
            sections.append("### Active Rule Sources\n" + _format_bullets(_dedupe(rule_sources)))

    if hydration_trace:
        traces = [
            f"{item.get('resolver', 'unknown')} -> {item.get('section', 'context')} ({item.get('item_count', 0)} item(s))"
            for item in hydration_trace[:10]
        ]
        sections.append("### Hydration Trace\n" + _format_bullets(traces))

    if hydrated_context.strip():
        sections.append("### Hydrated Context\n" + _truncate(hydrated_context, max_length=1200))

    if not sections:
        return "_No additional context was pre-hydrated for this run._"
    return "\n\n".join(sections)


def build_unresolved_issues_section(
    *,
    partial: bool = False,
    ci_summary: str = "",
    ci_failures_summary: str = "",
    ci_conclusion: str | None = None,
    ci_run_id: str | None = None,
    autofix_candidates: list[str] | None = None,
    security_events: list[dict[str, Any]] | None = None,
) -> str:
    sections: list[str] = []
    if partial:
        sections.append(
            "### Partial Success\nThe agent produced a branch and PR, but the capped verification loop still ended with unresolved issues."
        )

    if ci_summary:
        sections.append(f"### Final CI Summary\n{ci_summary.strip()}")
    if ci_failures_summary:
        sections.append(f"### Remaining CI Failures\n{ci_failures_summary.strip()}")

    ci_state: list[str] = []
    if ci_conclusion:
        ci_state.append(f"Conclusion: {ci_conclusion}")
    if ci_run_id:
        ci_state.append(f"Run ID: {ci_run_id}")
    if ci_state:
        sections.append("### Final CI State\n" + _format_bullets(ci_state))

    candidates = _dedupe(autofix_candidates or [])
    if candidates:
        sections.append("### Autofix Candidates\n" + _format_bullets(candidates))

    if security_events:
        events = []
        for event in security_events[:10]:
            reason = str(event.get("reason", "policy_block")).strip()
            tool_name = str(event.get("tool_name", "")).strip()
            command = str(event.get("command", "")).strip()
            path = str(event.get("path", "")).strip()
            details = [part for part in [tool_name, command, path] if part]
            summary = reason if not details else f"{reason}: {' | '.join(details)}"
            events.append(summary)
        if events:
            sections.append("### Blocked Tool or Security Events\n" + _format_bullets(events))

    if not sections:
        return "_No unresolved issues were recorded at PR creation time._"
    return "\n\n".join(sections)


def render_pr_body(
    task_description: str,
    changes_summary: str = "",
    template_name: str = "default",
    *,
    task_reference: str = "",
    context_summary: str = "",
    unresolved_issues: str = "",
    testing_summary: str = "",
) -> str:
    template = load_template(template_name)
    body = template.replace("{{task_description}}", task_description)
    body = body.replace("{{changes_summary}}", changes_summary or "_No change summary was recorded._")
    body = body.replace("{{task_reference}}", task_reference or f"- Task: {task_description}")
    body = body.replace(
        "{{context_summary}}",
        context_summary or "_No additional context was pre-hydrated for this run._",
    )
    body = body.replace(
        "{{unresolved_issues}}",
        unresolved_issues or "_No unresolved issues were recorded at PR creation time._",
    )
    body = body.replace("{{testing_summary}}", testing_summary or "- [ ] Local lint passes\n- [ ] Tests pass in CI")
    body = body.replace("<!-- Brief description of what was changed and why -->", changes_summary)
    body = body.replace("<!-- List of files changed and what was done -->", changes_summary)
    return body


def build_pr_title(task_description: str, max_length: int = 72) -> str:
    title = task_description.strip()
    if len(title) > max_length:
        title = title[:max_length - 3] + "..."
    return f"[Minion] {title}"


async def create_github_pr(
    repo: str,
    head_branch: str,
    base_branch: str,
    task_description: str,
    changes_summary: str = "",
    template_name: str = "default",
    body: str | None = None,
    title: str | None = None,
    labels: list[str] | None = None,
    reviewers: list[str] | None = None,
) -> dict:
    """Create a PR on GitHub. Returns PR data dict."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise ValueError("GITHUB_TOKEN environment variable not set")

    from src.integrations.github import GitHubClient
    client = GitHubClient(token=token)

    title = title or build_pr_title(task_description)
    body = body or render_pr_body(task_description, changes_summary, template_name)

    pr = await client.create_pr(
        repo=repo,
        head=head_branch,
        base=base_branch,
        title=title,
        body=body,
    )

    pr_number = pr.get("number")
    if pr_number:
        if labels:
            try:
                await client.add_labels(repo, pr_number, labels)
            except Exception as exc:
                logger.warning("label_add_failed", error=str(exc))
        if reviewers:
            try:
                await client.add_reviewers(repo, pr_number, reviewers)
            except Exception as exc:
                logger.warning("reviewer_add_failed", error=str(exc))

    logger.info("pr_created", url=pr.get("html_url", ""), number=pr_number)
    return pr
