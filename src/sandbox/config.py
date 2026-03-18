from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

DEFAULT_SANDBOX_IMAGE = "minions-sandbox:latest"
DEFAULT_SANDBOX_CPU_LIMIT = "2"
DEFAULT_SANDBOX_MEMORY_LIMIT = "4g"
DEFAULT_SANDBOX_TIMEOUT = 600


class SandboxMode(str, Enum):
    production = "production"
    development = "development"
    disabled = "disabled"


class SandboxConfig(BaseModel):
    """Configuration for a single sandbox container."""

    image: str = DEFAULT_SANDBOX_IMAGE
    cpu_limit: str = DEFAULT_SANDBOX_CPU_LIMIT
    memory_limit: str = DEFAULT_SANDBOX_MEMORY_LIMIT
    timeout: int = DEFAULT_SANDBOX_TIMEOUT
    network_enabled: bool = False
    mode: SandboxMode = SandboxMode.production
    allow_host_fallback: bool = False
    preinstall_tools: bool = False
    volumes: dict[str, str] = Field(default_factory=dict)
    env_vars: dict[str, str] = Field(default_factory=dict)


class SandboxPoolConfig(BaseModel):
    """Configuration for the sandbox pool manager."""

    pool_size: int = 3
    pre_warm: bool = True
    base_image: str = DEFAULT_SANDBOX_IMAGE
    mode: SandboxMode = SandboxMode.production
    network_enabled: bool = False
    allow_host_fallback: bool = False
    preinstall_tools: bool = False
