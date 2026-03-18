from __future__ import annotations

import asyncio
import inspect
import json
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
from typing import Any

import structlog

from src.tools.security import get_security_checker

logger = structlog.get_logger()


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Any]

    def to_openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolResult:
    output: str
    error: str | None = None
    success: bool = True


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            logger.warning("tool_overwrite", name=tool.name)
        self._tools[tool.name] = tool
        logger.debug("tool_registered", name=tool.name)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def list_tools(self) -> list[Tool]:
        return list(self._tools.values())

    def filter_tools(self, names: list[str]) -> list[Tool]:
        return [self._tools[n] for n in names if n in self._tools]

    def to_openai_schemas(self) -> list[dict]:
        return [t.to_openai_schema() for t in self._tools.values()]

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)


class ToolExecutor:
    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        tool = self._registry.get(tool_name)
        if tool is None:
            return ToolResult(
                output="",
                error=f"Unknown tool: {tool_name}",
                success=False,
            )

        logger.info("tool_execute", name=tool_name, arguments=arguments)

        checker = get_security_checker()
        if not checker.check_tool_call(tool_name, arguments):
            return ToolResult(
                output="",
                error=f"Security policy blocked tool call: {tool_name}",
                success=False,
            )

        try:
            if inspect.iscoroutinefunction(tool.handler):
                result = await tool.handler(**arguments)
            else:
                result = await asyncio.to_thread(tool.handler, **arguments)

            output = result if isinstance(result, str) else json.dumps(result, default=str)
            return ToolResult(output=output, success=True)
        except Exception as exc:
            tb = traceback.format_exc()
            logger.error("tool_error", name=tool_name, error=str(exc))
            return ToolResult(
                output="",
                error=f"{type(exc).__name__}: {exc}\n{tb}",
                success=False,
            )


def tool(
    name: str,
    description: str,
    parameters: dict[str, Any] | None = None,
) -> Callable:
    if parameters is None:
        parameters = {"type": "object", "properties": {}}

    def decorator(func: Callable) -> Callable:
        func._tool_meta = Tool(  # type: ignore[attr-defined]
            name=name,
            description=description,
            parameters=parameters,
            handler=func,
        )

        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return func(*args, **kwargs)

        wrapper._tool_meta = func._tool_meta  # type: ignore[attr-defined]
        return wrapper

    return decorator


def register_decorated(registry: ToolRegistry, *funcs: Callable) -> None:
    for func in funcs:
        meta: Tool | None = getattr(func, "_tool_meta", None)
        if meta is None:
            raise ValueError(f"{func.__name__} is not decorated with @tool")
        registry.register(meta)
