from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import structlog
import yaml

logger = structlog.get_logger()


@dataclass
class RuleSet:
    scope: str = "**"
    rules: list[str] = field(default_factory=list)
    source: str = ""

    def to_text(self) -> str:
        if not self.rules:
            return ""
        return "\n".join(f"- {r}" for r in self.rules)


class RuleLoader:
    """Discovers and loads rule files from the project directory."""

    RULE_FILES = [
        ".minion/rules.yaml",
        ".cursorrules",
        "CLAUDE.md",
        ".minionrules",
    ]

    def __init__(self, root_dir: str) -> None:
        self._root = Path(root_dir)
        self._rulesets: list[RuleSet] = []

    def load(self) -> list[RuleSet]:
        """Scan directory tree and load all rule files."""
        self._rulesets.clear()
        self._scan_directory(self._root)
        logger.info("rules_loaded", count=len(self._rulesets), root=str(self._root))
        return self._rulesets

    def _scan_directory(self, directory: Path, depth: int = 0) -> None:
        if depth > 10:
            return
        for filename in self.RULE_FILES:
            rule_path = directory / filename
            if rule_path.exists():
                rs = self._load_file(rule_path)
                if rs:
                    self._rulesets.append(rs)

        try:
            for entry in directory.iterdir():
                if entry.is_dir() and not entry.name.startswith(".") and entry.name not in (
                    "node_modules", "__pycache__", ".git", "venv", ".venv"
                ):
                    self._scan_directory(entry, depth + 1)
        except PermissionError:
            pass

    def _load_file(self, path: Path) -> RuleSet | None:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            rel_path = str(path.relative_to(self._root))
            scope = str(path.parent.relative_to(self._root)) + "/**"
            if scope == "./**":
                scope = "**"

            if path.name.endswith((".yaml", ".yml")):
                return self._parse_yaml(content, scope, rel_path)
            elif path.name in ("CLAUDE.md", ".cursorrules", ".minionrules"):
                return self._parse_markdown(content, scope, rel_path)
        except Exception as exc:
            logger.warning("rule_load_error", file=str(path), error=str(exc))
        return None

    def _parse_yaml(self, content: str, default_scope: str, source: str) -> RuleSet | None:
        data = yaml.safe_load(content)
        if not data or not isinstance(data, dict):
            return None
        rules = data.get("rules", [])
        scope = data.get("scope", default_scope)
        if not rules:
            return None
        return RuleSet(scope=scope, rules=rules, source=source)

    def _parse_markdown(self, content: str, scope: str, source: str) -> RuleSet | None:
        if not content.strip():
            return None
        # Treat the whole file as a single rule context
        rules = [content.strip()]
        return RuleSet(scope=scope, rules=rules, source=source)
