from __future__ import annotations

import asyncio
from typing import Any

import structlog

from src.sandbox.config import SandboxConfig

logger = structlog.get_logger()

try:
    import docker

    _DOCKER_AVAILABLE = True
except ImportError:
    _DOCKER_AVAILABLE = False


class DockerSandbox:
    """Docker-based sandbox for executing commands in isolation.

    Wraps the Docker SDK in async helpers.  If the ``docker`` package is not
    installed, :meth:`create` will raise :class:`RuntimeError`.
    """

    def __init__(self, config: SandboxConfig) -> None:
        self._config = config
        self._container: Any = None
        self._container_id: str = ""
        self._client: Any = None

    @property
    def container_id(self) -> str:
        return self._container_id

    async def create(self) -> str:
        """Create and start the container.  Returns the container id."""
        if not _DOCKER_AVAILABLE:
            raise RuntimeError(
                "docker SDK is not installed. Install it with: pip install docker"
            )

        loop = asyncio.get_event_loop()

        # Try standard Docker socket first, then Colima's socket path
        import os
        docker_host = os.environ.get("DOCKER_HOST")
        if docker_host:
            self._client = docker.DockerClient(base_url=docker_host)
        else:
            colima_sock = os.path.expanduser("~/.colima/default/docker.sock")
            if os.path.exists(colima_sock):
                self._client = docker.DockerClient(base_url=f"unix://{colima_sock}")
            else:
                self._client = docker.from_env()

        # Build container kwargs
        kwargs: dict[str, Any] = {
            "image": self._config.image,
            "command": "sleep infinity",
            "detach": True,
            "cpu_count": int(self._config.cpu_limit),
            "mem_limit": self._config.memory_limit,
            "environment": self._config.env_vars,
            "network_disabled": not self._config.network_enabled,
        }

        if self._config.volumes:
            kwargs["volumes"] = {
                src: {"bind": dst, "mode": "rw"}
                for src, dst in self._config.volumes.items()
            }

        self._container = await loop.run_in_executor(
            None, lambda: self._client.containers.run(**kwargs)
        )
        self._container_id = self._container.id
        logger.info("sandbox_created", container_id=self._container_id[:12])
        return self._container_id

    async def exec_command(self, cmd: str, timeout: int | None = None) -> tuple[str, int]:
        """Execute *cmd* inside the container and return ``(output, exit_code)``."""
        if self._container is None:
            raise RuntimeError("Sandbox has not been created yet")

        timeout = timeout or self._config.timeout
        loop = asyncio.get_event_loop()

        def _exec() -> tuple[str, int]:
            exit_code, output = self._container.exec_run(
                cmd, demux=False, workdir="/workspace"
            )
            text = output.decode("utf-8", errors="replace") if isinstance(output, bytes) else str(output)
            return text, exit_code

        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(None, _exec),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning("sandbox_exec_timeout", cmd=cmd[:80], timeout=timeout)
            return f"Command timed out after {timeout}s", 1

        return result

    async def copy_to(self, local_path: str, container_path: str) -> None:
        """Copy a local file/directory into the container."""
        if self._container is None:
            raise RuntimeError("Sandbox has not been created yet")

        import io
        import os
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            tar.add(local_path, arcname=os.path.basename(local_path))
        buf.seek(0)

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, lambda: self._container.put_archive(container_path, buf)
        )
        logger.debug("sandbox_copy", src=local_path, dst=container_path)

    async def destroy(self) -> None:
        """Stop and remove the container."""
        if self._container is None:
            return

        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                None, lambda: self._container.remove(force=True)
            )
            logger.info("sandbox_destroyed", container_id=self._container_id[:12])
        except Exception as exc:
            logger.warning("sandbox_destroy_error", error=str(exc))
        finally:
            self._container = None
            self._container_id = ""
