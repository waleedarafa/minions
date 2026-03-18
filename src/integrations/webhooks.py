from __future__ import annotations

import hashlib
import hmac
from typing import Any

import structlog
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from src.api.models import TaskCreate

logger = structlog.get_logger()

router = APIRouter(prefix="/webhooks")

# Configurable secret — in production, load from environment
_GITHUB_WEBHOOK_SECRET: str = ""


def set_github_webhook_secret(secret: str) -> None:
    """Set the shared secret used to verify GitHub webhook signatures."""
    global _GITHUB_WEBHOOK_SECRET
    _GITHUB_WEBHOOK_SECRET = secret


def _verify_github_signature(payload: bytes, signature: str, secret: str) -> bool:
    """Verify the ``X-Hub-Signature-256`` header sent by GitHub."""
    if not secret:
        return True  # no secret configured — skip in dev
    expected = "sha256=" + hmac.new(
        secret.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


class CustomWebhookPayload(BaseModel):
    """Body for the ``/webhooks/custom`` endpoint."""

    description: str
    repo: str = "."
    blueprint: str = "default"
    branch_base: str = "main"
    context_links: list[str] = Field(default_factory=list)
    priority: str = "normal"


@router.post("/github")
async def github_webhook(
    request: Request,
    x_hub_signature_256: str = Header(""),
    x_github_event: str = Header(""),
) -> dict[str, Any]:
    """Receive and handle a GitHub webhook event.

    Currently handles ``check_suite`` failures by creating a bugfix task.
    """
    body = await request.body()

    if not _verify_github_signature(body, x_hub_signature_256, _GITHUB_WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    payload = await request.json()
    logger.info("github_webhook_received", event=x_github_event)

    if x_github_event == "check_suite":
        action = payload.get("action")
        conclusion = payload.get("check_suite", {}).get("conclusion")

        if action == "completed" and conclusion == "failure":
            repo_name = payload.get("repository", {}).get("full_name", "")
            branch = payload.get("check_suite", {}).get("head_branch", "main")
            logger.info("ci_failure_detected", repo=repo_name, branch=branch)

            from src.api.routes import create_task

            task_payload = TaskCreate(
                description=f"CI failure on {repo_name} (branch {branch}). Investigate and fix.",
                repo=repo_name,
                branch_base=branch,
                blueprint="fix_and_verify",
                priority="high",
            )
            task = await create_task(task_payload)
            return {"status": "task_created", "task_id": task.id}

    return {"status": "ignored", "event": x_github_event}


@router.post("/custom")
async def custom_webhook(payload: CustomWebhookPayload) -> dict[str, Any]:
    """Accept an arbitrary webhook that directly creates a task."""
    from src.api.routes import create_task

    task_payload = TaskCreate(**payload.model_dump())
    task = await create_task(task_payload)
    logger.info("custom_webhook_task_created", task_id=task.id)
    return {"status": "task_created", "task_id": task.id}
