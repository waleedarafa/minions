from __future__ import annotations

import pytest

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


def test_sandbox_config_defaults() -> None:
    cfg = SandboxConfig()
    assert cfg.image == DEFAULT_SANDBOX_IMAGE
    assert cfg.memory_limit == DEFAULT_SANDBOX_MEMORY_LIMIT
    assert cfg.cpu_limit == DEFAULT_SANDBOX_CPU_LIMIT
    assert cfg.timeout == DEFAULT_SANDBOX_TIMEOUT
    assert cfg.network_enabled is False
    assert cfg.mode == SandboxMode.production
    assert cfg.allow_host_fallback is False


def test_sandbox_config_custom_values() -> None:
    cfg = SandboxConfig(
        image="custom:v1",
        memory_limit="1g",
        cpu_limit="4",
        timeout=900,
        network_enabled=True,
        mode=SandboxMode.development,
        allow_host_fallback=True,
    )
    assert cfg.image == "custom:v1"
    assert cfg.memory_limit == "1g"
    assert cfg.cpu_limit == "4"
    assert cfg.timeout == 900
    assert cfg.mode == SandboxMode.development
    assert cfg.allow_host_fallback is True


@pytest.mark.asyncio
async def test_sandbox_manager_acquire_creates_new_when_pool_empty(monkeypatch) -> None:
    created: list[str] = []

    class FakeSandbox:
        def __init__(self, config: SandboxConfig) -> None:
            self.config = config
            self.destroyed = False

        async def create(self) -> None:
            created.append(self.config.image)

        async def destroy(self) -> None:
            self.destroyed = True

    monkeypatch.setattr("src.sandbox.manager.DockerSandbox", FakeSandbox)

    manager = SandboxManager(SandboxPoolConfig(pool_size=2, pre_warm=False))
    sandbox = await manager.acquire()

    assert created == [DEFAULT_SANDBOX_IMAGE]
    assert sandbox in manager._active


@pytest.mark.asyncio
async def test_sandbox_manager_release_returns_to_pool(monkeypatch) -> None:
    class FakeSandbox:
        def __init__(self, config: SandboxConfig) -> None:
            self.config = config
            self.destroyed = False

        async def create(self) -> None:
            return None

        async def destroy(self) -> None:
            self.destroyed = True

    monkeypatch.setattr("src.sandbox.manager.DockerSandbox", FakeSandbox)

    manager = SandboxManager(SandboxPoolConfig(pool_size=1, pre_warm=False))
    sandbox = await manager.acquire()
    await manager.release(sandbox)
    sandbox2 = await manager.acquire()

    assert sandbox2 is sandbox


@pytest.mark.asyncio
async def test_sandbox_manager_release_destroys_when_pool_full(monkeypatch) -> None:
    class FakeSandbox:
        def __init__(self, config: SandboxConfig) -> None:
            self.config = config
            self.destroyed = False

        async def create(self) -> None:
            return None

        async def destroy(self) -> None:
            self.destroyed = True

    monkeypatch.setattr("src.sandbox.manager.DockerSandbox", FakeSandbox)

    manager = SandboxManager(SandboxPoolConfig(pool_size=1, pre_warm=False))
    first = await manager.acquire()
    await manager.release(first)

    second = FakeSandbox(SandboxConfig())
    await manager.release(second)

    assert second.destroyed is True


@pytest.mark.asyncio
async def test_sandbox_manager_cleanup_destroys_all(monkeypatch) -> None:
    class FakeSandbox:
        def __init__(self, config: SandboxConfig) -> None:
            self.config = config
            self.destroyed = False

        async def create(self) -> None:
            return None

        async def destroy(self) -> None:
            self.destroyed = True

    monkeypatch.setattr("src.sandbox.manager.DockerSandbox", FakeSandbox)

    manager = SandboxManager(SandboxPoolConfig(pool_size=2, pre_warm=False))
    pooled = FakeSandbox(SandboxConfig())
    active = FakeSandbox(SandboxConfig())
    manager._pool.append(pooled)
    manager._active.append(active)

    await manager.cleanup()

    assert pooled.destroyed is True
    assert active.destroyed is True
    assert list(manager._pool) == []
    assert manager._active == []


@pytest.mark.asyncio
async def test_sandbox_manager_uses_template_config(monkeypatch) -> None:
    seen: list[SandboxConfig] = []

    class FakeSandbox:
        def __init__(self, config: SandboxConfig) -> None:
            seen.append(config)
            self.config = config

        async def create(self) -> None:
            return None

        async def destroy(self) -> None:
            return None

    monkeypatch.setattr("src.sandbox.manager.DockerSandbox", FakeSandbox)

    template = SandboxConfig(
        image="custom-sandbox:v2",
        network_enabled=True,
        mode=SandboxMode.development,
        allow_host_fallback=True,
    )
    manager = SandboxManager(
        SandboxPoolConfig(pool_size=1, pre_warm=False, base_image="ignored"),
        sandbox_template=template,
    )

    await manager.acquire()

    assert seen[0].image == "custom-sandbox:v2"
    assert seen[0].network_enabled is True
    assert seen[0].mode == SandboxMode.development
