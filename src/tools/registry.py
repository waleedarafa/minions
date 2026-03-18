from __future__ import annotations

import structlog

from src.agent.tools import Tool, ToolRegistry

logger = structlog.get_logger()


class GlobalToolRegistry:
    """Singleton-style global registry that auto-discovers builtin tools."""

    _instance: GlobalToolRegistry | None = None
    _registry: ToolRegistry | None = None

    def __new__(cls) -> GlobalToolRegistry:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @property
    def registry(self) -> ToolRegistry:
        if self._registry is None:
            self._registry = self.create_builtin_registry()
        return self._registry

    def create_builtin_registry(self, allowed_names: set[str] | None = None) -> ToolRegistry:
        """Create a fresh registry populated with builtin tools."""
        registry = ToolRegistry()
        from src.tools.builtin import ci, execution, file_ops, git, search

        for module in [file_ops, search, git, execution, ci]:
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                meta = getattr(attr, "_tool_meta", None)
                if meta is not None and isinstance(meta, Tool):
                    if allowed_names is not None and meta.name not in allowed_names:
                        continue
                    registry.register(meta)
                    logger.debug("builtin_tool_registered", name=meta.name)
        return registry

    def register_builtin_tools(self) -> None:
        """Populate the shared registry with all builtin tools."""
        self._registry = self.create_builtin_registry()

    def get_tools_for_step(self, tool_names: list[str]) -> list[Tool]:
        if not tool_names:
            return self.registry.list_tools()
        return self.registry.filter_tools(tool_names)

    def discover_tools(self) -> list[Tool]:
        return self.registry.list_tools()


def get_global_registry() -> GlobalToolRegistry:
    return GlobalToolRegistry()
