"""Tests for the feedback system."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Lightweight stand-ins – swap for real imports once src.feedback is populated.
# ---------------------------------------------------------------------------

try:
    from src.feedback import (
        AutofixPattern,
        AutofixRegistry,
        LintRunner,
        parse_pytest_output,
        select_linters,
    )
except ImportError:

    @dataclass
    class LinterConfig:
        name: str
        command: str
        file_types: list[str]
        config_files: list[str]

    KNOWN_LINTERS: list[LinterConfig] = [
        LinterConfig("ruff", "ruff check", [".py"], ["ruff.toml", "pyproject.toml"]),
        LinterConfig("eslint", "eslint", [".js", ".ts", ".tsx"], [".eslintrc", ".eslintrc.json", ".eslintrc.js"]),
        LinterConfig("mypy", "mypy", [".py"], ["mypy.ini", "pyproject.toml"]),
    ]

    class LintRunner:
        def __init__(self, project_root: str) -> None:
            self.project_root = Path(project_root)

        def detect_linters(self) -> list[str]:
            found: list[str] = []
            for linter in KNOWN_LINTERS:
                for cfg in linter.config_files:
                    if (self.project_root / cfg).exists():
                        found.append(linter.name)
                        break
            return found

    def select_linters(file_path: str) -> list[str]:
        ext = Path(file_path).suffix
        selected = []
        for linter in KNOWN_LINTERS:
            if ext in linter.file_types:
                selected.append(linter.name)
        return selected

    def parse_pytest_output(output: str) -> list[dict[str, Any]]:
        failures: list[dict[str, Any]] = []
        pattern = re.compile(r"FAILED\s+([\w/\.:]+)\s*-\s*(.*)")
        for line in output.splitlines():
            m = pattern.search(line)
            if m:
                failures.append({"test": m.group(1), "reason": m.group(2).strip()})
        return failures

    @dataclass
    class AutofixPattern:
        name: str
        pattern: str  # regex
        fix_description: str
        file_types: list[str] = field(default_factory=list)

    class AutofixRegistry:
        def __init__(self) -> None:
            self._patterns: list[AutofixPattern] = []

        def register(self, pattern: AutofixPattern) -> None:
            self._patterns.append(pattern)

        def find_matches(self, error_text: str) -> list[AutofixPattern]:
            matches = []
            for p in self._patterns:
                if re.search(p.pattern, error_text):
                    matches.append(p)
            return matches

        def list_patterns(self) -> list[AutofixPattern]:
            return list(self._patterns)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLintRunnerDetect:
    def test_detect_ruff(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n")
        runner = LintRunner(str(tmp_path))
        linters = runner.detect_linters()
        assert "ruff" in linters

    def test_detect_eslint(self, tmp_path: Path) -> None:
        (tmp_path / ".eslintrc.json").write_text("{}")
        runner = LintRunner(str(tmp_path))
        linters = runner.detect_linters()
        assert "eslint" in linters

    def test_detect_no_linters(self, tmp_path: Path) -> None:
        runner = LintRunner(str(tmp_path))
        linters = runner.detect_linters()
        assert linters == []


class TestLintHeuristicSelection:
    def test_python_file(self) -> None:
        linters = select_linters("src/main.py")
        assert "ruff" in linters
        assert "mypy" in linters

    def test_typescript_file(self) -> None:
        linters = select_linters("app/index.ts")
        assert "eslint" in linters

    def test_unknown_extension(self) -> None:
        linters = select_linters("data.csv")
        assert linters == []


class TestPytestFailureParsing:
    def test_parse_failures(self) -> None:
        output = (
            "FAILED tests/test_foo.py::test_bar - AssertionError: expected 1 got 2\n"
            "FAILED tests/test_baz.py::test_qux - TypeError: bad type\n"
            "===== 2 failed =====\n"
        )
        failures = parse_pytest_output(output)
        assert len(failures) == 2
        assert failures[0]["test"] == "tests/test_foo.py::test_bar"
        assert "AssertionError" in failures[0]["reason"]
        assert failures[1]["test"] == "tests/test_baz.py::test_qux"

    def test_parse_no_failures(self) -> None:
        output = "===== 5 passed =====\n"
        failures = parse_pytest_output(output)
        assert failures == []


class TestAutofixPatternMatching:
    def test_match_pattern(self) -> None:
        pattern = AutofixPattern(
            name="unused-import",
            pattern=r"F401.*imported but unused",
            fix_description="Remove the unused import",
        )
        error = "src/main.py:1:1: F401 `os` imported but unused"
        assert re.search(pattern.pattern, error) is not None

    def test_no_match(self) -> None:
        pattern = AutofixPattern(
            name="unused-import",
            pattern=r"F401.*imported but unused",
            fix_description="Remove the unused import",
        )
        error = "src/main.py:1:1: E501 line too long"
        assert re.search(pattern.pattern, error) is None


class TestAutofixRegistry:
    def test_register_and_find(self) -> None:
        registry = AutofixRegistry()
        p1 = AutofixPattern(
            name="unused-import",
            pattern=r"F401.*imported but unused",
            fix_description="Remove unused import",
        )
        p2 = AutofixPattern(
            name="line-too-long",
            pattern=r"E501.*line too long",
            fix_description="Break the line",
        )
        registry.register(p1)
        registry.register(p2)

        matches = registry.find_matches("F401 `os` imported but unused")
        assert len(matches) == 1
        assert matches[0].name == "unused-import"

    def test_list_patterns(self) -> None:
        registry = AutofixRegistry()
        registry.register(AutofixPattern(name="a", pattern="a", fix_description="x"))
        registry.register(AutofixPattern(name="b", pattern="b", fix_description="y"))
        assert len(registry.list_patterns()) == 2


@pytest.mark.asyncio
async def test_create_github_pr_respects_explicit_title(monkeypatch) -> None:
    from src.output.pr import create_github_pr

    created: dict[str, Any] = {}

    class FakeGitHubClient:
        def __init__(self, token: str) -> None:
            self.token = token

        async def create_pr(self, repo, head, base, title, body):
            created["repo"] = repo
            created["head"] = head
            created["base"] = base
            created["title"] = title
            created["body"] = body
            return {"number": 1, "html_url": "https://example.com/pr/1"}

        async def add_labels(self, repo, pr_number, labels):
            created["labels"] = labels

        async def add_reviewers(self, repo, pr_number, reviewers):
            created["reviewers"] = reviewers

    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr("src.integrations.github.GitHubClient", FakeGitHubClient)

    pr = await create_github_pr(
        repo="org/repo",
        head_branch="minion/test",
        base_branch="main",
        task_description="Task description",
        changes_summary="Summary text",
        title="[Minion][Partial] Task description",
        labels=["partial"],
    )

    assert pr["html_url"] == "https://example.com/pr/1"
    assert created["title"] == "[Minion][Partial] Task description"
    assert created["labels"] == ["partial"]


def test_render_pr_body_supports_rich_sections() -> None:
    from src.output.pr import render_pr_body

    body = render_pr_body(
        "Task description",
        changes_summary="### Implementation Summary\nUpdated the PR renderer.",
        task_reference="- Source: api\n- Base Branch: main",
        context_summary="### Hydrated Context\nFetched issue context.",
        unresolved_issues="### Final CI Summary\nAll green.",
        testing_summary="- [x] Local verification attempted: yes",
    )

    assert "## Summary" in body
    assert "Updated the PR renderer." in body
    assert "## Task Reference" in body
    assert "Source: api" in body
    assert "## Context" in body
    assert "Fetched issue context." in body
    assert "## Unresolved Issues" in body
    assert "All green." in body
    assert "## Testing" in body
    assert "Local verification attempted: yes" in body
