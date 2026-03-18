from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class FailureInfo:
    test_name: str
    error_type: str
    message: str
    file_path: str = ""
    line_number: int | None = None


def parse_pytest_output(output: str) -> list[FailureInfo]:
    failures = []
    lines = output.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        # Match FAILED test lines like: FAILED tests/test_foo.py::TestClass::test_method
        m = re.match(r"FAILED\s+([\w/.]+)::(.+)", line)
        if m:
            failures.append(FailureInfo(
                test_name=m.group(2).strip(),
                error_type="TestFailure",
                message="",
                file_path=m.group(1),
            ))
        # Match error lines
        m2 = re.match(r"E\s+(.+Error|.+Exception):\s*(.*)", line)
        if m2 and failures:
            failures[-1].error_type = m2.group(1)
            failures[-1].message = m2.group(2)
        i += 1
    return failures


def parse_ruff_output(output: str) -> list[FailureInfo]:
    failures = []
    for line in output.splitlines():
        m = re.match(r"(.+?):(\d+):\d+:\s+([A-Z]\d+)\s+(.*)", line)
        if m:
            failures.append(FailureInfo(
                test_name=f"{m.group(3)} at {m.group(1)}:{m.group(2)}",
                error_type=m.group(3),
                message=m.group(4),
                file_path=m.group(1),
                line_number=int(m.group(2)),
            ))
    return failures


def summarize_failures(failures: list[FailureInfo]) -> str:
    if not failures:
        return "No failures found."
    lines = [f"Found {len(failures)} failure(s):"]
    for f in failures[:20]:
        loc = f"{f.file_path}:{f.line_number}" if f.line_number else f.file_path
        lines.append(f"  - [{f.error_type}] {f.test_name}: {f.message} ({loc})")
    if len(failures) > 20:
        lines.append(f"  ... and {len(failures) - 20} more")
    return "\n".join(lines)
