"""Built-in worker tool schemas + dispatchers.

Dispatchers are registered with the global ToolRegistry and look up the
calling agent's scratch directory to scope file operations.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from taskgenie.config import CONFIG
from taskgenie.tools.registry import REGISTRY

_READ_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read a file from your scratch directory. Returns the file contents as a string.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path relative to your scratch directory.",
                }
            },
            "required": ["path"],
        },
    },
}

_WRITE_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "Write content to a file in your scratch directory. Creates parent directories as needed.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to your scratch directory."},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
}

_EXEC_CODE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "exec_code",
        "description": (
            "Execute a short Python or shell snippet in a per-agent scratch sandbox with a "
            "timeout. Returns stdout + stderr + exit code. Useful for quick computations, "
            "file processing, or invoking installed CLIs. State does NOT persist between "
            "calls; if you need to keep something, write it to a file."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "language": {"type": "string", "enum": ["python", "shell"]},
                "code": {"type": "string"},
            },
            "required": ["language", "code"],
        },
    },
}


def _safe_path(scratch_dir: str, rel_path: str) -> Path:
    base = Path(scratch_dir).resolve()
    base.mkdir(parents=True, exist_ok=True)
    target = (base / rel_path).resolve()
    if not str(target).startswith(str(base)):
        raise ValueError(f"Path escapes scratch dir: {rel_path}")
    return target


async def _read_file(agent: Any, args: dict[str, Any]) -> str:
    p = _safe_path(agent.scratch_dir, args["path"])
    if not p.exists():
        return f"ERROR: file does not exist: {args['path']}"
    return p.read_text()


async def _write_file(agent: Any, args: dict[str, Any]) -> str:
    p = _safe_path(agent.scratch_dir, args["path"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(args["content"])
    return f"wrote {len(args['content'])} chars to {args['path']}"


async def _exec_code(agent: Any, args: dict[str, Any]) -> str:
    # Real implementation lands in a later pass; v1 returns a stub so the wiring
    # is testable and the model gets a clear signal that this is unavailable.
    preview = args["code"][:200] + ("..." if len(args["code"]) > 200 else "")
    return (
        f"[exec_code STUB] Would run {args['language']}:\n{preview}\n"
        "Real execution is not wired in v1 — call report_blocker if you need this."
    )


def scratch_for(agent_id: str) -> str:
    path = os.path.join(str(CONFIG.scratch_dir), agent_id)
    os.makedirs(path, exist_ok=True)
    return path


def register_builtin_work_tools() -> None:
    REGISTRY.register(_READ_FILE_SCHEMA, kind="work", version="1.0", dispatcher=_read_file)
    REGISTRY.register(_WRITE_FILE_SCHEMA, kind="work", version="1.0", dispatcher=_write_file)
    REGISTRY.register(_EXEC_CODE_SCHEMA, kind="work", version="0.1", dispatcher=_exec_code)


register_builtin_work_tools()
