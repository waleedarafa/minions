from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger()

# slack-bolt is an optional dependency
try:
    from slack_bolt.async_app import AsyncApp
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

    _SLACK_AVAILABLE = True
except ImportError:
    _SLACK_AVAILABLE = False


class SlackBot:
    """Slack integration for triggering and monitoring minion tasks.

    Requires the optional ``slack-bolt`` package.  If it is not installed the
    bot can still be instantiated (for config validation, etc.) but calling
    :meth:`start` will raise :class:`RuntimeError`.
    """

    def __init__(
        self,
        bot_token: str,
        app_token: str,
        channel_ids: list[str] | None = None,
    ) -> None:
        self._bot_token = bot_token
        self._app_token = app_token
        self._channel_ids = channel_ids or []
        self._app: Any = None
        self._handler: Any = None

    async def start(self) -> None:
        """Start listening for Slack events via Socket Mode."""
        if not _SLACK_AVAILABLE:
            raise RuntimeError(
                "slack-bolt is not installed. Install it with: pip install slack-bolt"
            )

        self._app = AsyncApp(token=self._bot_token)

        @self._app.event("message")
        async def _on_message(event: dict[str, Any], say: Any) -> None:
            await self.handle_message(event, say)

        self._handler = AsyncSocketModeHandler(self._app, self._app_token)
        logger.info("slack_bot_starting", channels=self._channel_ids)
        await self._handler.start_async()

    async def handle_message(self, event: dict[str, Any], say: Any = None) -> None:
        """Parse an incoming Slack message and create a task if applicable."""
        text: str = event.get("text", "")
        channel: str = event.get("channel", "")

        # Only act in configured channels, if any were specified
        if self._channel_ids and channel not in self._channel_ids:
            return

        # Simple command detection: messages starting with "!minion"
        if not text.lower().startswith("!minion"):
            return

        description = text[len("!minion"):].strip()
        if not description:
            await self.send_update(channel, "Usage: `!minion <task description>`", say)
            return

        logger.info("slack_task_requested", channel=channel, description=description[:80])

        try:
            from src.api.models import TaskCreate
            from src.api.routes import create_task

            payload = TaskCreate(description=description, repo=".")
            task = await create_task(payload)
            await self.send_update(
                channel,
                f"Task created: `{task.id}` — {task.description[:120]}",
                say,
            )
        except Exception as exc:
            logger.error("slack_task_creation_failed", error=str(exc))
            await self.send_update(channel, f"Failed to create task: {exc}", say)

    async def send_update(
        self, channel: str, message: str, say: Any = None
    ) -> None:
        """Send a message to a Slack channel."""
        if say is not None:
            await say(text=message, channel=channel)
        elif self._app is not None:
            await self._app.client.chat_postMessage(channel=channel, text=message)
        else:
            logger.warning("slack_send_no_client", channel=channel, message=message[:80])
