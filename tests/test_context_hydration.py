from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_hydrate_context_includes_jira_and_links(monkeypatch):
    from src.context.hydration import hydrate_context

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, link: str):
            return SimpleNamespace(
                headers={"content-type": "text/html"},
                text="<html><head><title>Spec Doc</title></head><body>Design details for the task.</body></html>",
                raise_for_status=lambda: None,
            )

    monkeypatch.setattr("src.context.hydration.httpx.AsyncClient", lambda **kwargs: FakeClient())

    text = await hydrate_context(
        context_links=["https://example.com/spec"],
        jira_ticket="PROJ-123",
    )

    assert "### Jira" in text
    assert "PROJ-123" in text
    assert "### Linked Context" in text
    assert "Spec Doc" in text
    assert "https://example.com/spec" in text


@pytest.mark.asyncio
async def test_hydrate_context_handles_fetch_failure(monkeypatch):
    from src.context.hydration import hydrate_context

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, link: str):
            raise RuntimeError("network down")

    monkeypatch.setattr("src.context.hydration.httpx.AsyncClient", lambda **kwargs: FakeClient())

    text = await hydrate_context(
        context_links=["https://example.com/spec"],
        jira_ticket=None,
    )

    assert "Could not fetch linked context" in text


@pytest.mark.asyncio
async def test_hydrate_context_uses_structured_github_issue_resolution(monkeypatch):
    from src.context.hydration import hydrate_context

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, link: str, headers=None):
            return SimpleNamespace(
                json=lambda: {
                    "title": "Fix webhook retries",
                    "state": "open",
                    "body": "Investigate duplicate webhook delivery behavior.",
                },
                raise_for_status=lambda: None,
            )

    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr("src.context.hydration.httpx.AsyncClient", lambda **kwargs: FakeClient())

    text = await hydrate_context(
        context_links=["https://github.com/acme/payments/issues/42"],
        jira_ticket=None,
    )

    assert "GitHub Issue: #42 [open] Fix webhook retries" in text
    assert "duplicate webhook delivery behavior" in text


@pytest.mark.asyncio
async def test_hydrate_context_uses_structured_github_actions_resolution(monkeypatch):
    from src.context.hydration import hydrate_context

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, link: str, headers=None):
            return SimpleNamespace(
                json=lambda: {
                    "name": "CI",
                    "status": "completed",
                    "conclusion": "failure",
                    "event": "push",
                    "head_branch": "feature/test",
                },
                raise_for_status=lambda: None,
            )

    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr("src.context.hydration.httpx.AsyncClient", lambda **kwargs: FakeClient())

    text = await hydrate_context(
        context_links=["https://github.com/acme/payments/actions/runs/12345"],
        jira_ticket=None,
    )

    assert "GitHub Actions Run: CI" in text
    assert "Status: completed / failure" in text
    assert "Branch: feature/test" in text


@pytest.mark.asyncio
async def test_hydrate_context_uses_jira_base_url_when_available(monkeypatch):
    from src.context.hydration import hydrate_context

    monkeypatch.setenv("JIRA_BASE_URL", "https://jira.example.com")

    text = await hydrate_context(context_links=[], jira_ticket="PROJ-456")

    assert "Ticket: PROJ-456" in text
    assert "https://jira.example.com/browse/PROJ-456" in text


@pytest.mark.asyncio
async def test_hydrate_context_runs_registered_metadata_hooks(monkeypatch):
    from src.context.hydration import clear_metadata_hooks, hydrate_context, register_metadata_hook

    async def fake_hook(context_links: list[str], jira_ticket: str | None):
        return ["### Extra Context\n- Hook saw " + ",".join(context_links) + f" / {jira_ticket}"]

    clear_metadata_hooks()
    register_metadata_hook(fake_hook)
    try:
        text = await hydrate_context(
            context_links=["https://example.com/spec"],
            jira_ticket="PROJ-789",
        )
    finally:
        clear_metadata_hooks()

    assert "### Extra Context" in text
    assert "https://example.com/spec" in text
    assert "PROJ-789" in text


@pytest.mark.asyncio
async def test_hydrate_context_detailed_returns_trace(monkeypatch):
    from src.context.hydration import hydrate_context_detailed

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, link: str):
            return SimpleNamespace(
                headers={"content-type": "text/html"},
                text="<html><head><title>Spec Doc</title></head><body>Design details for the task.</body></html>",
                raise_for_status=lambda: None,
            )

    monkeypatch.setattr("src.context.hydration.httpx.AsyncClient", lambda **kwargs: FakeClient())

    result = await hydrate_context_detailed(
        context_links=["https://example.com/spec"],
        jira_ticket="PROJ-321",
    )

    assert "Spec Doc" in result.text
    assert {trace.resolver for trace in result.traces} == {"jira_ticket", "context_links"}
