from __future__ import annotations

import os
import re
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger()


@dataclass
class SecurityPolicy:
    allowed_commands: list[str] = field(default_factory=list)
    blocked_paths: list[str] = field(default_factory=lambda: ["/etc", "/usr", "/bin", "/sbin", "/var", "/root"])
    allow_network: bool = False
    allow_write: bool = True
    max_file_size_mb: int = 10
    working_dir: str = "."


DEFAULT_POLICY = SecurityPolicy(
    allowed_commands=["git", "python", "pytest", "ruff", "mypy", "npm", "yarn", "make"],
    blocked_paths=["/etc/passwd", "/etc/shadow", "/root", "~/.ssh"],
    allow_network=False,
    allow_write=True,
)

RESTRICTIVE_POLICY = SecurityPolicy(
    allowed_commands=["git", "python", "pytest"],
    blocked_paths=["/etc", "/usr/bin", "/bin/sh", "/root", "~/.ssh", "~/.aws"],
    allow_network=False,
    allow_write=True,
)

_current_checker: SecurityChecker | None = None
_SHELL_CONTROL_PATTERN = re.compile(r"(\|\||&&|[|;`]|>\s*|<\s*|\$\()")
_DESTRUCTIVE_COMMAND_PATTERN = re.compile(
    r"\b(rm\s+-rf|mkfs|dd\s+if=|shutdown|reboot|poweroff|halt)\b",
    re.IGNORECASE,
)
_NETWORK_COMMAND_PATTERN = re.compile(r"\b(curl|wget|nc|ncat|telnet|ssh|scp)\b", re.IGNORECASE)
_security_events_var: ContextVar[list[dict[str, Any]]] = ContextVar("security_events", default=[])


def _record_security_event(event_type: str, **payload: Any) -> None:
    events = list(_security_events_var.get())
    events.append({"type": event_type, **payload})
    _security_events_var.set(events)


class SecurityChecker:
    def __init__(self, policy: SecurityPolicy = DEFAULT_POLICY) -> None:
        self._policy = policy

    def check_command(self, cmd: str) -> bool:
        """Return True if command is allowed."""
        stripped = cmd.strip()
        if not stripped:
            logger.warning("security_blocked_empty_command")
            _record_security_event("blocked_command", reason="empty_command", command=stripped)
            return False
        if _SHELL_CONTROL_PATTERN.search(stripped):
            logger.warning("security_blocked_shell_control", cmd=stripped)
            _record_security_event("blocked_command", reason="shell_control", command=stripped)
            return False
        if _DESTRUCTIVE_COMMAND_PATTERN.search(stripped):
            logger.warning("security_blocked_destructive_command", cmd=stripped)
            _record_security_event("blocked_command", reason="destructive_command", command=stripped)
            return False
        if not self._policy.allow_network and _NETWORK_COMMAND_PATTERN.search(stripped):
            logger.warning("security_blocked_network_command", cmd=stripped)
            _record_security_event("blocked_command", reason="network_command", command=stripped)
            return False
        cmd_base = stripped.split()[0]
        cmd_base = os.path.basename(cmd_base)
        if not self._policy.allowed_commands:
            return True
        allowed = any(
            cmd_base == allowed or cmd_base.startswith(allowed)
            for allowed in self._policy.allowed_commands
        )
        if not allowed:
            logger.warning("security_blocked_command", cmd=cmd_base)
            _record_security_event("blocked_command", reason="disallowed_command", command=stripped, command_base=cmd_base)
        return allowed

    def check_file_access(self, path: str, mode: str = "r") -> bool:
        """Return True if file access is allowed."""
        candidate = path
        if not os.path.isabs(candidate):
            candidate = os.path.join(self._policy.working_dir, candidate)
        resolved = os.path.realpath(candidate)
        for blocked in self._policy.blocked_paths:
            expanded = os.path.expanduser(blocked)
            if resolved.startswith(expanded):
                logger.warning("security_blocked_path", path=path, blocked=blocked)
                _record_security_event("blocked_path", reason="blocked_path", path=path, blocked=blocked)
                return False
        if mode in ("w", "a", "x") and not self._policy.allow_write:
            logger.warning("security_write_denied", path=path)
            _record_security_event("blocked_path", reason="write_denied", path=path)
            return False
        # Prevent path traversal outside working dir unless it's a read
        if mode in ("w", "a", "x"):
            working = os.path.realpath(self._policy.working_dir)
            if not resolved.startswith(working):
                logger.warning("security_write_outside_workdir", path=path, working=working)
                _record_security_event("blocked_path", reason="outside_workdir", path=path, working_dir=working)
                return False
        return True

    def check_tool_call(self, tool_name: str, args: dict) -> bool:
        """Return True if the tool call is permitted under the current policy."""
        allowed = True
        if tool_name == "run_command":
            cmd = args.get("command", "")
            allowed = self.check_command(cmd)
        elif tool_name in ("read_file", "edit_file", "create_file", "delete_file"):
            path = args.get("path", "")
            mode = "r" if tool_name == "read_file" else "w"
            allowed = self.check_file_access(path, mode)
        if not allowed:
            _record_security_event("blocked_tool_call", tool_name=tool_name, arguments=args)
        return allowed


def get_security_checker() -> SecurityChecker:
    global _current_checker
    if _current_checker is None:
        _current_checker = SecurityChecker(DEFAULT_POLICY)
    return _current_checker


def set_security_policy(policy: SecurityPolicy) -> None:
    global _current_checker
    _current_checker = SecurityChecker(policy)


def reset_security_events() -> None:
    _security_events_var.set([])


def get_security_events() -> list[dict[str, Any]]:
    return list(_security_events_var.get())
