from __future__ import annotations

from typing import Any

import httpx
import structlog

logger = structlog.get_logger()

_GITHUB_API_BASE = "https://api.github.com"


class GitHubClient:
    """Async GitHub API client for pull-request and Actions workflows."""

    def __init__(self, token: str) -> None:
        self._token = token
        self._client = httpx.AsyncClient(
            base_url=_GITHUB_API_BASE,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )

    async def close(self) -> None:
        await self._client.aclose()

    # -- Pull Requests -------------------------------------------------------

    async def create_pr(
        self,
        repo: str,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> dict[str, Any]:
        """Create a pull request on *repo* (``owner/repo``)."""
        resp = await self._client.post(
            f"/repos/{repo}/pulls",
            json={"head": head, "base": base, "title": title, "body": body},
        )
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        logger.info("pr_created", repo=repo, number=data.get("number"), url=data.get("html_url"))
        return data

    async def add_labels(self, repo: str, pr_number: int, labels: list[str]) -> None:
        resp = await self._client.post(
            f"/repos/{repo}/issues/{pr_number}/labels",
            json={"labels": labels},
        )
        resp.raise_for_status()
        logger.info("labels_added", repo=repo, pr=pr_number, labels=labels)

    async def add_reviewers(self, repo: str, pr_number: int, reviewers: list[str]) -> None:
        resp = await self._client.post(
            f"/repos/{repo}/pulls/{pr_number}/requested_reviewers",
            json={"reviewers": reviewers},
        )
        resp.raise_for_status()
        logger.info("reviewers_added", repo=repo, pr=pr_number, reviewers=reviewers)

    async def get_pr(self, repo: str, pr_number: int) -> dict[str, Any]:
        resp = await self._client.get(f"/repos/{repo}/pulls/{pr_number}")
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    # -- Actions / Workflows --------------------------------------------------

    async def trigger_workflow(
        self, repo: str, workflow_id: str, ref: str
    ) -> dict[str, Any]:
        resp = await self._client.post(
            f"/repos/{repo}/actions/workflows/{workflow_id}/dispatches",
            json={"ref": ref},
        )
        resp.raise_for_status()
        logger.info("workflow_dispatched", repo=repo, workflow=workflow_id, ref=ref)
        return {"status": "dispatched"}

    async def get_workflow_run(self, repo: str, run_id: int) -> dict[str, Any]:
        resp = await self._client.get(f"/repos/{repo}/actions/runs/{run_id}")
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    async def get_workflow_run_logs(self, repo: str, run_id: int) -> str:
        resp = await self._client.get(
            f"/repos/{repo}/actions/runs/{run_id}/logs",
            follow_redirects=True,
        )
        resp.raise_for_status()
        return resp.text
