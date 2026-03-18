from __future__ import annotations

from pathlib import Path

import structlog

from src.agent.tools import tool

logger = structlog.get_logger()

_WORKING_DIR: str = "."


def set_working_dir(path: str) -> None:
    global _WORKING_DIR
    _WORKING_DIR = path


def _resolve(path: str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = Path(_WORKING_DIR) / p
    return p.resolve()


@tool(
    name="read_file",
    description="Read the contents of a file.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to read"},
            "start_line": {"type": "integer", "description": "Optional start line (1-indexed)"},
            "end_line": {"type": "integer", "description": "Optional end line (inclusive)"},
        },
        "required": ["path"],
    },
)
def read_file(path: str, start_line: int | None = None, end_line: int | None = None) -> str:
    resolved = _resolve(path)
    if not resolved.exists():
        return f"Error: File not found: {path}"
    if not resolved.is_file():
        return f"Error: Not a file: {path}"
    content = resolved.read_text(encoding="utf-8", errors="replace")
    if start_line is not None or end_line is not None:
        lines = content.splitlines(keepends=True)
        s = (start_line or 1) - 1
        e = end_line or len(lines)
        content = "".join(lines[s:e])
    return content


@tool(
    name="edit_file",
    description="Replace exact text in a file. The old_text must match exactly.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to file to edit"},
            "old_text": {"type": "string", "description": "Exact text to replace"},
            "new_text": {"type": "string", "description": "Replacement text"},
        },
        "required": ["path", "old_text", "new_text"],
    },
)
def edit_file(path: str, old_text: str, new_text: str) -> str:
    resolved = _resolve(path)
    if not resolved.exists():
        return f"Error: File not found: {path}"
    content = resolved.read_text(encoding="utf-8", errors="replace")
    if old_text not in content:
        return f"Error: old_text not found in {path}"
    updated = content.replace(old_text, new_text, 1)
    resolved.write_text(updated, encoding="utf-8")
    return f"Successfully edited {path}"


@tool(
    name="create_file",
    description="Create a new file with the given content. Fails if file already exists unless overwrite is True.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path for the new file"},
            "content": {"type": "string", "description": "File content"},
            "overwrite": {"type": "boolean", "description": "Overwrite if exists (default false)"},
        },
        "required": ["path", "content"],
    },
)
def create_file(path: str, content: str, overwrite: bool = False) -> str:
    resolved = _resolve(path)
    if resolved.exists() and not overwrite:
        return f"Error: File already exists: {path}. Use overwrite=true to replace."
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content, encoding="utf-8")
    return f"Created {path}"


@tool(
    name="delete_file",
    description="Delete a file.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to file to delete"},
        },
        "required": ["path"],
    },
)
def delete_file(path: str) -> str:
    resolved = _resolve(path)
    if not resolved.exists():
        return f"Error: File not found: {path}"
    resolved.unlink()
    return f"Deleted {path}"


@tool(
    name="list_files",
    description="List files in a directory, optionally filtered by glob pattern.",
    parameters={
        "type": "object",
        "properties": {
            "directory": {"type": "string", "description": "Directory to list"},
            "pattern": {"type": "string", "description": "Glob pattern (default: *)"},
        },
        "required": ["directory"],
    },
)
def list_files(directory: str, pattern: str = "*") -> str:
    resolved = _resolve(directory)
    if not resolved.exists():
        return f"Error: Directory not found: {directory}"
    if not resolved.is_dir():
        return f"Error: Not a directory: {directory}"
    files = sorted(resolved.glob(pattern))
    if not files:
        return f"No files matching {pattern} in {directory}"
    return "\n".join(str(f.relative_to(resolved)) for f in files)
