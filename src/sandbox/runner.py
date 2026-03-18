from __future__ import annotations

import os
import subprocess
from typing import Any

import structlog

from src.sandbox.config import (
    DEFAULT_SANDBOX_CPU_LIMIT,
    DEFAULT_SANDBOX_IMAGE,
    DEFAULT_SANDBOX_MEMORY_LIMIT,
    DEFAULT_SANDBOX_TIMEOUT,
    SandboxConfig,
    SandboxMode,
    SandboxPoolConfig,
)
from src.sandbox.manager import SandboxManager

logger = structlog.get_logger()

# Module-level active runner — None means use local subprocess
_active_runner: SandboxRunner | None = None
_sandbox_managers: dict[tuple[str, str, bool], SandboxManager] = {}


def get_runner() -> SandboxRunner | None:
    return _active_runner


def set_runner(runner: SandboxRunner | None) -> None:
    global _active_runner
    _active_runner = runner


def run_local(cmd: str, cwd: str, timeout: int) -> tuple[str, int]:
    """Execute cmd locally via subprocess. Returns (output, returncode)."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=timeout, cwd=cwd,
        )
        output = result.stdout + result.stderr
        if len(output) > 10000:
            output = output[:5000] + "\n... [truncated] ...\n" + output[-5000:]
        return output.strip() or f"(exit code: {result.returncode})", result.returncode
    except subprocess.TimeoutExpired:
        return f"Error: Command timed out after {timeout}s", 1
    except Exception as exc:
        return f"Error: {exc}", 1


def resolve_sandbox_mode(default: SandboxMode = SandboxMode.production) -> SandboxMode:
    raw = os.environ.get("MINIONS_SANDBOX_MODE", "").strip().lower()
    if raw in {mode.value for mode in SandboxMode}:
        return SandboxMode(raw)
    return default


def should_allow_host_fallback(mode: SandboxMode) -> bool:
    if mode == SandboxMode.development:
        return os.environ.get("MINIONS_ALLOW_HOST_FALLBACK", "1").strip().lower() not in {"0", "false", "no"}
    return os.environ.get("MINIONS_ALLOW_HOST_FALLBACK", "0").strip().lower() in {"1", "true", "yes"}


def _resolve_sandbox_image() -> str:
    return os.environ.get("MINIONS_SANDBOX_IMAGE", DEFAULT_SANDBOX_IMAGE)


def _resolve_cpu_limit() -> str:
    return os.environ.get("MINIONS_SANDBOX_CPU_LIMIT", DEFAULT_SANDBOX_CPU_LIMIT)


def _resolve_memory_limit() -> str:
    return os.environ.get("MINIONS_SANDBOX_MEMORY_LIMIT", DEFAULT_SANDBOX_MEMORY_LIMIT)


def _resolve_timeout() -> int:
    raw = os.environ.get("MINIONS_SANDBOX_TIMEOUT")
    if raw and raw.isdigit():
        return int(raw)
    return DEFAULT_SANDBOX_TIMEOUT


def default_sandbox_config(repo_path: str, mode: SandboxMode | None = None) -> SandboxConfig:
    resolved_mode = mode or resolve_sandbox_mode()
    return SandboxConfig(
        image=_resolve_sandbox_image(),
        cpu_limit=_resolve_cpu_limit(),
        memory_limit=_resolve_memory_limit(),
        timeout=_resolve_timeout(),
        volumes={repo_path: SandboxRunner.WORKSPACE},
        network_enabled=resolved_mode == SandboxMode.development,
        mode=resolved_mode,
        allow_host_fallback=should_allow_host_fallback(resolved_mode),
        preinstall_tools=False,
    )


def default_pool_config(config: SandboxConfig) -> SandboxPoolConfig:
    pool_size = int(os.environ.get("MINIONS_SANDBOX_POOL_SIZE", "2"))
    pre_warm = os.environ.get("MINIONS_SANDBOX_PREWARM", "1").strip().lower() not in {"0", "false", "no"}
    return SandboxPoolConfig(
        pool_size=pool_size,
        pre_warm=pre_warm,
        base_image=config.image,
        mode=config.mode,
        network_enabled=config.network_enabled,
        allow_host_fallback=config.allow_host_fallback,
        preinstall_tools=config.preinstall_tools,
    )


async def get_shared_sandbox_manager(repo_path: str, config: SandboxConfig) -> SandboxManager:
    key = (repo_path, config.mode.value, config.network_enabled)
    manager = _sandbox_managers.get(key)
    if manager is None:
        template = config.model_copy(deep=True)
        template.volumes = {repo_path: SandboxRunner.WORKSPACE}
        manager = SandboxManager(default_pool_config(config), sandbox_template=template)
        await manager.start()
        _sandbox_managers[key] = manager
    return manager


async def cleanup_shared_sandbox_managers() -> None:
    managers = list(_sandbox_managers.values())
    _sandbox_managers.clear()
    for manager in managers:
        try:
            await manager.cleanup()
        except Exception as exc:
            logger.warning("shared_sandbox_manager_cleanup_failed", error=str(exc))


class SandboxRunner:
    """
    Wraps a DockerSandbox so commands run inside an isolated container.
    The repo is mounted (or copied) at /workspace inside the container.
    """

    WORKSPACE = "/workspace"

    def __init__(
        self,
        repo_path: str,
        image: str = DEFAULT_SANDBOX_IMAGE,
        config: SandboxConfig | None = None,
        use_pool: bool = False,
    ) -> None:
        self._repo_path = repo_path
        self._config = config or SandboxConfig(image=image, volumes={repo_path: self.WORKSPACE})
        self._image = self._config.image
        self._sandbox: Any = None
        self._manager: SandboxManager | None = None
        self._use_pool = use_pool

    @property
    def config(self) -> SandboxConfig:
        return self._config

    async def start(self) -> None:
        from src.sandbox.docker import DockerSandbox

        if self._use_pool:
            self._manager = await get_shared_sandbox_manager(self._repo_path, self._config)
            self._sandbox = await self._manager.acquire()
        else:
            self._sandbox = DockerSandbox(self._config)
            await self._sandbox.create()

        if self._config.preinstall_tools:
            output, rc = await self._sandbox.exec_command(
                "bash -c 'python -m pip install --quiet ruff pytest'", timeout=120
            )
            if rc != 0:
                logger.warning("sandbox_tools_install_failed", output=output[:300])
            else:
                logger.info("sandbox_tools_installed")
        logger.info(
            "sandbox_runner_started",
            repo=self._repo_path,
            image=self._image,
            mode=self._config.mode.value,
            network_enabled=self._config.network_enabled,
            allow_host_fallback=self._config.allow_host_fallback,
        )

    async def run(self, cmd: str, timeout: int = 120) -> tuple[str, int]:
        """Run cmd inside the container at /workspace. Returns (output, returncode)."""
        if self._sandbox is None:
            raise RuntimeError("SandboxRunner not started — call start() first")
        # Wrap in bash -c so shell features (pipes, &&, etc.) work
        full_cmd = f"bash -c 'cd {self.WORKSPACE} && {cmd}'"
        output, returncode = await self._sandbox.exec_command(full_cmd, timeout=timeout)
        if len(output) > 10000:
            output = output[:5000] + "\n... [truncated] ...\n" + output[-5000:]
        return output, returncode

    async def stop(self) -> None:
        if self._sandbox is not None:
            if self._manager is not None:
                await self._manager.release(self._sandbox)
            else:
                await self._sandbox.destroy()
            self._sandbox = None
            self._manager = None
            logger.info("sandbox_runner_stopped")
