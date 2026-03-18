from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from html import unescape
from typing import Awaitable, Callable

import httpx
import structlog

logger = structlog.get_logger()
_GITHUB_URL_RE = re.compile(r"^https://github\.com/([^/]+/[^/]+)/(issues|pull|commit|actions/runs)/([^/?#]+)")
MetadataHydrationHook = Callable[[list[str], str | None], Awaitable[list[str] | None]]
_METADATA_HOOKS: list[MetadataHydrationHook] = []


@dataclass
class HydrationTrace:
    resolver: str
    section: str
    item_count: int = 0


@dataclass
class HydrationResult:
    text: str = ""
    traces: list[HydrationTrace] = field(default_factory=list)


def _extract_html_title(html: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    title = unescape(match.group(1))
    return " ".join(title.split()).strip()


def _extract_text_excerpt(body: str, limit: int = 280) -> str:
    text = re.sub(r"<[^>]+>", " ", body)
    text = unescape(text)
    text = " ".join(text.split()).strip()
    return text[:limit].rstrip() if len(text) > limit else text


def _parse_github_link(link: str) -> tuple[str, str, str] | None:
    match = _GITHUB_URL_RE.match(link)
    if not match:
        return None
    return match.group(1), match.group(2), match.group(3)


def _github_headers() -> dict[str, str]:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def register_metadata_hook(hook: MetadataHydrationHook) -> None:
    _METADATA_HOOKS.append(hook)


def clear_metadata_hooks() -> None:
    _METADATA_HOOKS.clear()


def _hydrate_jira_ticket(jira_ticket: str | None) -> str | None:
    if not jira_ticket:
        return None

    base_url = os.environ.get("JIRA_BASE_URL", "").strip().rstrip("/")
    if base_url:
        return f"### Jira\n- Ticket: {jira_ticket}\n- Link: {base_url}/browse/{jira_ticket}"
    return f"### Jira\n- Ticket: {jira_ticket}"


async def _run_metadata_hooks(
    context_links: list[str],
    jira_ticket: str | None,
    metadata_hooks: list[MetadataHydrationHook] | None = None,
) -> tuple[list[str], list[HydrationTrace]]:
    sections: list[str] = []
    traces: list[HydrationTrace] = []
    hooks = list(_METADATA_HOOKS)
    if metadata_hooks:
        hooks.extend(metadata_hooks)
    for hook in hooks:
        hook_name = getattr(hook, "__name__", "hook")
        try:
            result = await hook(context_links, jira_ticket)
        except Exception as exc:
            logger.warning("metadata_hydration_hook_failed", hook=hook_name, error=str(exc))
            continue
        if result:
            valid_sections = [section for section in result if section and section.strip()]
            sections.extend(valid_sections)
            traces.append(
                HydrationTrace(
                    resolver=hook_name,
                    section="metadata_hook",
                    item_count=len(valid_sections),
                )
            )
    return sections, traces


async def _hydrate_github_link(link: str, repo: str, kind: str, identifier: str, timeout: float = 10.0) -> str | None:
    if not os.environ.get("GITHUB_TOKEN", "").strip():
        return None

    if kind == "issues":
        endpoint = f"https://api.github.com/repos/{repo}/issues/{identifier}"
    elif kind == "pull":
        endpoint = f"https://api.github.com/repos/{repo}/pulls/{identifier}"
    elif kind == "commit":
        endpoint = f"https://api.github.com/repos/{repo}/commits/{identifier}"
    elif kind == "actions/runs":
        endpoint = f"https://api.github.com/repos/{repo}/actions/runs/{identifier}"
    else:
        return None

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(endpoint, headers=_github_headers())
            response.raise_for_status()
        data = response.json()
    except Exception as exc:
        logger.warning("github_context_hydration_failed", link=link, error=str(exc))
        return f"- {link}\n  Note: Could not fetch GitHub context ({exc})"

    if kind == "issues":
        title = data.get("title", "")
        state = data.get("state", "unknown")
        body = _extract_text_excerpt(data.get("body", "") or "")
        return f"- {link}\n  GitHub Issue: #{identifier} [{state}] {title}\n  Summary: {body or 'No description provided'}"

    if kind == "pull":
        title = data.get("title", "")
        state = data.get("state", "unknown")
        base = data.get("base", {}).get("ref", "")
        head = data.get("head", {}).get("ref", "")
        body = _extract_text_excerpt(data.get("body", "") or "")
        return (
            f"- {link}\n"
            f"  GitHub PR: #{identifier} [{state}] {title}\n"
            f"  Branches: {head} -> {base}\n"
            f"  Summary: {body or 'No description provided'}"
        )

    if kind == "commit":
        commit = data.get("commit", {})
        message = _extract_text_excerpt(commit.get("message", "") or "", limit=200)
        author = commit.get("author", {}).get("name", "unknown")
        sha = str(data.get("sha", identifier))[:12]
        return f"- {link}\n  GitHub Commit: {sha} by {author}\n  Message: {message or 'No commit message provided'}"

    if kind == "actions/runs":
        name = data.get("name", "workflow")
        status = data.get("status", "unknown")
        conclusion = data.get("conclusion", "pending")
        event = data.get("event", "unknown")
        head_branch = data.get("head_branch", "")
        return (
            f"- {link}\n"
            f"  GitHub Actions Run: {name}\n"
            f"  Status: {status} / {conclusion}\n"
            f"  Event: {event}\n"
            f"  Branch: {head_branch or 'unknown'}"
        )

    return None


async def _hydrate_url(link: str, timeout: float = 10.0) -> str:
    github_parts = _parse_github_link(link)
    if github_parts is not None:
        repo, kind, identifier = github_parts
        hydrated = await _hydrate_github_link(link, repo, kind, identifier, timeout=timeout)
        if hydrated is not None:
            return hydrated

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(link)
            response.raise_for_status()

        content_type = response.headers.get("content-type", "")
        body = response.text
        if "html" in content_type:
            title = _extract_html_title(body)
            excerpt = _extract_text_excerpt(body)
            if title and excerpt and excerpt != title:
                return f"- {link}\n  Title: {title}\n  Summary: {excerpt}"
            if title:
                return f"- {link}\n  Title: {title}"
            if excerpt:
                return f"- {link}\n  Summary: {excerpt}"
        else:
            excerpt = _extract_text_excerpt(body)
            if excerpt:
                return f"- {link}\n  Summary: {excerpt}"
    except Exception as exc:
        logger.warning("context_hydration_failed", link=link, error=str(exc))
        return f"- {link}\n  Note: Could not fetch linked context ({exc})"

    return f"- {link}"


async def hydrate_context_detailed(
    context_links: list[str],
    jira_ticket: str | None = None,
    metadata_hooks: list[MetadataHydrationHook] | None = None,
) -> HydrationResult:
    sections: list[str] = []
    traces: list[HydrationTrace] = []

    jira_section = _hydrate_jira_ticket(jira_ticket)
    if jira_section:
        sections.append(jira_section)
        traces.append(HydrationTrace(resolver="jira_ticket", section="jira", item_count=1))

    if context_links:
        hydrated_links = [await _hydrate_url(link) for link in context_links]
        sections.append("### Linked Context\n" + "\n".join(hydrated_links))
        traces.append(HydrationTrace(resolver="context_links", section="linked_context", item_count=len(hydrated_links)))

    hook_sections, hook_traces = await _run_metadata_hooks(
        context_links,
        jira_ticket,
        metadata_hooks=metadata_hooks,
    )
    sections.extend(hook_sections)
    traces.extend(hook_traces)

    return HydrationResult(
        text="\n\n".join(section for section in sections if section.strip()),
        traces=traces,
    )


async def hydrate_context(
    context_links: list[str],
    jira_ticket: str | None = None,
    metadata_hooks: list[MetadataHydrationHook] | None = None,
) -> str:
    result = await hydrate_context_detailed(
        context_links=context_links,
        jira_ticket=jira_ticket,
        metadata_hooks=metadata_hooks,
    )
    return result.text
