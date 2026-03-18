from __future__ import annotations

import asyncio
from collections import deque

import structlog

from src.sandbox.config import SandboxConfig, SandboxPoolConfig
from src.sandbox.docker import DockerSandbox

logger = structlog.get_logger()


class SandboxManager:
    """Manages a pool of :class:`DockerSandbox` instances.

    Supports pre-warming so sandboxes are ready when a task arrives.
    """

    def __init__(self, pool_config: SandboxPoolConfig, sandbox_template: SandboxConfig | None = None) -> None:
        self._config = pool_config
        self._sandbox_template = sandbox_template
        self._pool: deque[DockerSandbox] = deque()
        self._active: list[DockerSandbox] = []
        self._lock = asyncio.Lock()
        self._warming_task: asyncio.Task[None] | None = None
        self._started = False

    async def start(self) -> None:
        """Initialize the pool and optionally pre-warm sandboxes."""
        if self._started:
            return
        self._started = True
        if self._config.pre_warm:
            self._warming_task = asyncio.create_task(self._pre_warm())

    async def _pre_warm(self) -> None:
        """Create sandboxes up to pool_size in the background."""
        for _ in range(self._config.pool_size):
            try:
                sandbox = await self._create_sandbox()
                async with self._lock:
                    self._pool.append(sandbox)
                logger.info(
                    "sandbox_pre_warmed",
                    pool_size=len(self._pool),
                    target=self._config.pool_size,
                )
            except Exception as exc:
                logger.warning("pre_warm_failed", error=str(exc))
                break

    def _make_config(self) -> SandboxConfig:
        if self._sandbox_template is not None:
            return self._sandbox_template.model_copy(deep=True)
        return SandboxConfig(
            image=self._config.base_image,
            mode=self._config.mode,
            network_enabled=self._config.network_enabled,
            allow_host_fallback=self._config.allow_host_fallback,
            preinstall_tools=self._config.preinstall_tools,
        )

    async def _create_sandbox(self) -> DockerSandbox:
        sandbox = DockerSandbox(self._make_config())
        await sandbox.create()
        return sandbox

    async def acquire(self) -> DockerSandbox:
        """Get a sandbox from the pool, or create a new one if the pool is empty."""
        async with self._lock:
            if self._pool:
                sandbox = self._pool.popleft()
                self._active.append(sandbox)
                logger.info("sandbox_acquired_from_pool", pool_remaining=len(self._pool))
                return sandbox

        # Pool is empty — create on demand
        sandbox = await self._create_sandbox()
        async with self._lock:
            self._active.append(sandbox)
        logger.info("sandbox_acquired_new")
        return sandbox

    async def release(self, sandbox: DockerSandbox) -> None:
        """Return a sandbox to the pool or destroy it if the pool is full."""
        async with self._lock:
            if sandbox in self._active:
                self._active.remove(sandbox)

            if len(self._pool) < self._config.pool_size:
                self._pool.append(sandbox)
                logger.info("sandbox_returned_to_pool", pool_size=len(self._pool))
                return

        # Pool is full — destroy the sandbox
        await sandbox.destroy()
        logger.info("sandbox_destroyed_excess")

    async def cleanup(self) -> None:
        """Destroy all sandboxes (pooled and active)."""
        if self._warming_task and not self._warming_task.done():
            self._warming_task.cancel()

        async with self._lock:
            all_sandboxes = list(self._pool) + list(self._active)
            self._pool.clear()
            self._active.clear()

        for sandbox in all_sandboxes:
            try:
                await sandbox.destroy()
            except Exception as exc:
                logger.warning("cleanup_destroy_error", error=str(exc))

        logger.info("sandbox_manager_cleaned_up", destroyed=len(all_sandboxes))
