from __future__ import annotations

import os
from pathlib import Path

from taskgenie.config import CONFIG

WORK_TOOLS = [
    {
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
    },
    {
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
    },
    {
        "type": "function",
        "function": {
            "name": "exec_code",
            "description": (
                "STUB (v1): Execute a short Python or shell snippet. Currently returns a placeholder — "
                "wire up real sandboxed execution before relying on this."
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
    },
    {
        "type": "function",
        "function": {
            "name": "complete",
            "description": (
                "Return your final report to whoever assigned you this task. Ends your run. "
                "Be thorough — your parent only sees the report, not your internal narration."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "report": {
                        "type": "string",
                        "description": "The deliverable. Include sources and concrete findings.",
                    }
                },
                "required": ["report"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "report_blocker",
            "description": (
                "Declare that you cannot complete your task and need help to proceed. Ends your run. "
                "The blocker bubbles up to whoever assigned you. Use this when a tool you need "
                "doesn't work, you lack information, or the task is impossible with your current "
                "abilities. Do NOT fabricate results to avoid reporting a blocker — an honest "
                "blocker is more valuable than a fake completion."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "What is blocking you? Be specific (e.g., 'brave_search returns 401 on every query', 'I need to run python but exec_code is stubbed').",
                    },
                    "needs": {
                        "type": "string",
                        "description": "What specifically would unblock you? (e.g., 'a working brave_search', 'sandboxed python execution', 'clarification on X').",
                    },
                    "partial_progress": {
                        "type": "string",
                        "description": "What you did manage to accomplish before getting stuck. Concrete findings, not narration.",
                    },
                },
                "required": ["reason", "needs"],
            },
        },
    },
]


def _safe_path(scratch_dir: str, rel_path: str) -> Path:
    base = Path(scratch_dir).resolve()
    base.mkdir(parents=True, exist_ok=True)
    target = (base / rel_path).resolve()
    if not str(target).startswith(str(base)):
        raise ValueError(f"Path escapes scratch dir: {rel_path}")
    return target


def read_file(agent_scratch: str, path: str) -> str:
    p = _safe_path(agent_scratch, path)
    if not p.exists():
        return f"ERROR: file does not exist: {path}"
    return p.read_text()


def write_file(agent_scratch: str, path: str, content: str) -> str:
    p = _safe_path(agent_scratch, path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return f"wrote {len(content)} chars to {path}"


def exec_code(language: str, code: str) -> str:
    # TODO: real sandboxed execution. For v1 we just acknowledge the request so the
    # demo doesn't crash and the model knows this capability isn't live yet.
    preview = code[:200] + ("..." if len(code) > 200 else "")
    return (
        f"[exec_code STUB] Would run {language}:\n{preview}\n"
        "Real execution is not wired in v1 — the model should not rely on this output."
    )


def scratch_for(task_id: str) -> str:
    path = os.path.join(CONFIG.scratch_dir, task_id)
    os.makedirs(path, exist_ok=True)
    return path
