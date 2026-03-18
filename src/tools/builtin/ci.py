from __future__ import annotations

import os
from typing import Any

import httpx
import structlog

from src.agent.tools import tool

logger = structlog.get_logger()

_GITHUB_TOKEN: str = os.environ.get("GITHUB_TOKEN", "")
_API_BASE = "https://api.github.com"


def _headers() -> dict[str, str]:
    h = {"Accept": "application/vnd.github.v3+json"}
    if _GITHUB_TOKEN:
        h["Authorization"] = f"token {_GITHUB_TOKEN}"
    return h


@tool(
    name="get_ci_status",
    description="Get the CI status of a branch on GitHub.",
    parameters={
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "GitHub repo (owner/repo)"},
            "branch": {"type": "string", "description": "Branch name"},
        },
        "required": ["repo", "branch"],
    },
)
def get_ci_status(repo: str, branch: str) -> str:
    if not _GITHUB_TOKEN:
        return "Error: GITHUB_TOKEN not set. Cannot query CI status."
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.get(
                f"{_API_BASE}/repos/{repo}/commits/{branch}/check-runs",
                headers=_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            runs: list[dict[str, Any]] = data.get("check_runs", [])
            if not runs:
                return f"No CI runs found for {repo}@{branch}"
            lines = [f"CI Status for {repo}@{branch}:"]
            for run in runs[:10]:
                lines.append(f"  - {run['name']}: {run['status']} / {run.get('conclusion', 'pending')}")
            return "\n".join(lines)
    except Exception as exc:
        return f"Error fetching CI status: {exc}"


@tool(
    name="get_ci_logs",
    description="Get the logs of a specific CI run on GitHub.",
    parameters={
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "GitHub repo (owner/repo)"},
            "run_id": {"type": "string", "description": "Workflow run ID"},
        },
        "required": ["repo", "run_id"],
    },
)
def get_ci_logs(repo: str, run_id: str) -> str:
    if not _GITHUB_TOKEN:
        return "Error: GITHUB_TOKEN not set."
    try:
        with httpx.Client(timeout=60, follow_redirects=True) as client:
            resp = client.get(
                f"{_API_BASE}/repos/{repo}/actions/runs/{run_id}/logs",
                headers=_headers(),
            )
            resp.raise_for_status()
            log_text = resp.text
            if len(log_text) > 20000:
                log_text = log_text[-20000:]  # last 20k chars most relevant
            return log_text
    except Exception as exc:
        return f"Error fetching CI logs: {exc}"


@tool(
    name="get_test_results",
    description="Get test results from a GitHub Actions run.",
    parameters={
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "GitHub repo (owner/repo)"},
            "run_id": {"type": "string", "description": "Workflow run ID"},
        },
        "required": ["repo", "run_id"],
    },
)
def get_test_results(repo: str, run_id: str) -> str:
    if not _GITHUB_TOKEN:
        return "Error: GITHUB_TOKEN not set."
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.get(
                f"{_API_BASE}/repos/{repo}/actions/runs/{run_id}/jobs",
                headers=_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            jobs = data.get("jobs", [])
            lines = [f"Test results for run {run_id}:"]
            for job in jobs:
                status = job.get("conclusion") or job.get("status", "unknown")
                lines.append(f"  Job: {job['name']} — {status}")
                for step in job.get("steps", []):
                    if step.get("conclusion") == "failure":
                        lines.append(f"    FAILED step: {step['name']}")
            return "\n".join(lines)
    except Exception as exc:
        return f"Error fetching test results: {exc}"
