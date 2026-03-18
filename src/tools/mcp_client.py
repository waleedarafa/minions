from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

from src.agent.tools import Tool, ToolResult

logger = structlog.get_logger()


@dataclass
class MCPServerConfig:
    name: str
    # Transport: "stdio" or "http"
    transport: str = "stdio"
    # For stdio: the command to spawn (e.g. ["uvx", "mcp-atlassian"])
    command: list[str] = field(default_factory=list)
    # For http: the base URL (e.g. "http://localhost:3000")
    url: str = ""
    env: dict[str, str] = field(default_factory=dict)
    timeout: int = 30


class MCPClient:
    """Client for MCP servers. Supports stdio (local subprocess) and HTTP transports."""

    def __init__(self, config: MCPServerConfig) -> None:
        self._config = config
        self._tools: list[Tool] = []
        self._session: Any = None
        self._exit_stack: Any = None

    async def connect(self) -> None:
        if self._config.transport == "stdio":
            await self._connect_stdio()
        else:
            await self._connect_http()

    async def discover_tools(self) -> list[Tool]:
        if self._config.transport == "stdio":
            return await self._discover_stdio()
        return await self._discover_http()

    async def call_tool(self, name: str, args: dict[str, Any]) -> ToolResult:
        if self._config.transport == "stdio":
            return await self._call_stdio(name, args)
        return await self._call_http(name, args)

    async def close(self) -> None:
        if self._exit_stack is not None:
            await self._exit_stack.aclose()
            self._exit_stack = None
            self._session = None

    # -- stdio transport -------------------------------------------------------

    async def _connect_stdio(self) -> None:
        import os
        from contextlib import AsyncExitStack

        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        if not self._config.command:
            raise ValueError(f"MCP server '{self._config.name}' has no command configured")

        env = {**os.environ, **self._config.env}
        params = StdioServerParameters(
            command=self._config.command[0],
            args=self._config.command[1:],
            env=env,
        )

        self._exit_stack = AsyncExitStack()
        read, write = await self._exit_stack.enter_async_context(stdio_client(params))
        self._session = await self._exit_stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()
        logger.info("mcp_connected", server=self._config.name, transport="stdio")

    async def _discover_stdio(self) -> list[Tool]:
        response = await self._session.list_tools()
        self._tools = [self._convert_mcp_tool(t) for t in response.tools]
        logger.info("mcp_tools_discovered", server=self._config.name, count=len(self._tools))
        return self._tools

    async def _call_stdio(self, name: str, args: dict[str, Any]) -> ToolResult:
        try:
            response = await self._session.call_tool(name, args)
            output = "\n".join(
                block.text for block in response.content if hasattr(block, "text")
            )
            return ToolResult(output=output, success=True)
        except Exception as exc:
            logger.error("mcp_tool_call_failed", name=name, error=str(exc))
            return ToolResult(output="", error=str(exc), success=False)

    # -- HTTP transport --------------------------------------------------------

    async def _connect_http(self) -> None:
        import httpx
        self._http_client = httpx.AsyncClient(
            base_url=self._config.url, timeout=self._config.timeout
        )
        resp = await self._http_client.get("/health")
        resp.raise_for_status()
        logger.info("mcp_connected", server=self._config.name, transport="http", url=self._config.url)

    async def _discover_http(self) -> list[Tool]:
        resp = await self._http_client.get("/tools")
        resp.raise_for_status()
        mcp_tools: list[dict[str, Any]] = resp.json().get("tools", [])
        self._tools = [self._convert_http_tool(t) for t in mcp_tools]
        logger.info("mcp_tools_discovered", server=self._config.name, count=len(self._tools))
        return self._tools

    async def _call_http(self, name: str, args: dict[str, Any]) -> ToolResult:
        try:
            resp = await self._http_client.post("/tools/call", json={"name": name, "arguments": args})
            resp.raise_for_status()
            data = resp.json()
            return ToolResult(output=str(data.get("result", "")), success=True)
        except Exception as exc:
            logger.error("mcp_tool_call_failed", name=name, error=str(exc))
            return ToolResult(output="", error=str(exc), success=False)

    # -- conversion ------------------------------------------------------------

    def _convert_mcp_tool(self, mcp_tool: Any) -> Tool:
        """Convert an MCP SDK Tool object (stdio) to internal Tool."""
        name = mcp_tool.name
        client_ref = self

        async def handler(**kwargs: Any) -> str:
            result = await client_ref.call_tool(name, kwargs)
            return result.output if result.success else f"Error: {result.error}"

        return Tool(
            name=name,
            description=mcp_tool.description or "",
            parameters=mcp_tool.inputSchema if hasattr(mcp_tool, "inputSchema") else {"type": "object", "properties": {}},
            handler=handler,
        )

    def _convert_http_tool(self, mcp_tool: dict[str, Any]) -> Tool:
        """Convert an HTTP MCP tool dict to internal Tool."""
        name = mcp_tool["name"]
        client_ref = self

        async def handler(**kwargs: Any) -> str:
            result = await client_ref.call_tool(name, kwargs)
            return result.output if result.success else f"Error: {result.error}"

        return Tool(
            name=name,
            description=mcp_tool.get("description", ""),
            parameters=mcp_tool.get("inputSchema", {"type": "object", "properties": {}}),
            handler=handler,
        )
