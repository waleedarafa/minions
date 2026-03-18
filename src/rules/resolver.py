from __future__ import annotations

import fnmatch
from pathlib import Path

import structlog

from src.rules.loader import RuleLoader, RuleSet

logger = structlog.get_logger()


class RuleResolver:
    """Resolves rules contextually based on active files."""

    def __init__(self, root_dir: str) -> None:
        self._root = root_dir
        self._loader = RuleLoader(root_dir)
        self._rulesets: list[RuleSet] | None = None

    def _ensure_loaded(self) -> None:
        if self._rulesets is None:
            self._rulesets = self._loader.load()

    def get_rules_for_files(self, file_paths: list[str]) -> str:
        """Return aggregated rules text relevant to the given files."""
        self._ensure_loaded()
        assert self._rulesets is not None
        return self._format_rules(self.get_matching_rulesets(file_paths))

    def get_matching_rulesets(self, file_paths: list[str]) -> list[RuleSet]:
        """Return global rules plus scoped rules that match the given files."""
        self._ensure_loaded()
        assert self._rulesets is not None
        matching: list[RuleSet] = []
        for rs in self._rulesets:
            if rs.scope == "**":
                matching.append(rs)
                continue
            if file_paths and self._matches_any(rs.scope, file_paths):
                matching.append(rs)
        return matching

    def get_all_rules(self) -> str:
        """Return all rules as a single text block."""
        self._ensure_loaded()
        assert self._rulesets is not None
        parts = [rs.to_text() for rs in self._rulesets if rs.rules]
        return "\n\n".join(filter(None, parts))

    def get_global_rules(self) -> str:
        """Return only globally-scoped rules."""
        self._ensure_loaded()
        assert self._rulesets is not None
        global_rules = [rs for rs in self._rulesets if rs.scope == "**"]
        return self._format_rules(global_rules)

    @staticmethod
    def _format_rules(rulesets: list[RuleSet]) -> str:
        if not rulesets:
            return ""
        parts = []
        for rs in rulesets:
            text = rs.to_text()
            if text:
                parts.append(f"[Rules from {rs.source}]\n{text}")
        return "\n\n".join(parts)

    def _matches_any(self, scope: str, file_paths: list[str]) -> bool:
        if scope == "**" or not file_paths:
            return True
        root = Path(self._root)
        for fp in file_paths:
            try:
                rel = str(Path(fp).relative_to(root))
            except ValueError:
                rel = fp
            if fnmatch.fnmatch(rel, scope):
                return True
        return False
