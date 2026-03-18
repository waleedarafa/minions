from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass

import httpx
import structlog

logger = structlog.get_logger()

_API_BASE = "https://api.github.com"


@dataclass
class CIResult:
    success: bool
    status: str
    conclusion: str | None
    run_id: str | None
    logs: str = ""
    summary: str = ""
    failures: list[str] | None = None


def _headers() -> dict[str, str]:
    token = os.environ.get("GITHUB_TOKEN", "")
    h = {"Accept": "application/vnd.github.v3+json"}
    if token:
        h["Authorization"] = f"token {token}"
    return h


async def wait_for_ci(
    repo: str,
    branch: str,
    sha: str | None = None,
    poll_interval: int = 30,
    timeout: int = 1800,
) -> CIResult:
    """Poll GitHub Actions until the CI run completes. Returns CIResult."""
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(timeout=30) as client:
        while time.monotonic() < deadline:
            try:
                url = f"{_API_BASE}/repos/{repo}/actions/runs"
                params: dict = {"branch": branch, "per_page": 5}
                resp = await client.get(url, headers=_headers(), params=params)
                resp.raise_for_status()
                runs = resp.json().get("workflow_runs", [])
                if not runs:
                    logger.info("ci_no_runs_yet", branch=branch)
                    await asyncio.sleep(poll_interval)
                    continue

                run = runs[0]
                status = run.get("status", "unknown")
                conclusion = run.get("conclusion")
                run_id = str(run.get("id", ""))

                logger.info("ci_status", status=status, conclusion=conclusion, run_id=run_id)

                if status == "completed":
                    success = conclusion == "success"
                    logs = ""
                    summary = await _get_job_summary(client, repo, run_id)
                    if not success:
                        logs = await _get_logs(client, repo, run_id)
                    return CIResult(
                        success=success,
                        status=status,
                        conclusion=conclusion,
                        run_id=run_id,
                        logs=logs,
                        summary=summary,
                    )
            except Exception as exc:
                logger.error("ci_poll_error", error=str(exc))
            await asyncio.sleep(poll_interval)

    return CIResult(
        success=False,
        status="timeout",
        conclusion=None,
        run_id=None,
        logs="CI timed out waiting for completion",
        summary="CI timed out waiting for completion",
    )


async def _get_logs(client: httpx.AsyncClient, repo: str, run_id: str) -> str:
    try:
        resp = await client.get(
            f"{_API_BASE}/repos/{repo}/actions/runs/{run_id}/logs",
            headers=_headers(),
            follow_redirects=True,
        )
        resp.raise_for_status()
        log_text = resp.text
        return log_text[-20000:] if len(log_text) > 20000 else log_text
    except Exception as exc:
        return f"Could not fetch logs: {exc}"


async def _get_job_summary(client: httpx.AsyncClient, repo: str, run_id: str) -> str:
    try:
        resp = await client.get(
            f"{_API_BASE}/repos/{repo}/actions/runs/{run_id}/jobs",
            headers=_headers(),
        )
        resp.raise_for_status()
        jobs = resp.json().get("jobs", [])
        if not jobs:
            return ""
        lines = [f"CI run {run_id} job summary:"]
        for job in jobs[:10]:
            status = job.get("conclusion") or job.get("status", "unknown")
            lines.append(f"- {job.get('name', 'unnamed job')}: {status}")
            for step in job.get("steps", [])[:20]:
                if step.get("conclusion") == "failure":
                    lines.append(f"  failed step: {step.get('name', 'unknown')}")
        return "\n".join(lines)
    except Exception as exc:
        return f"Could not fetch CI job summary: {exc}"
