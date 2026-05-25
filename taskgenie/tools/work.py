"""Built-in worker tool schemas + dispatchers.

Dispatchers are registered with the global ToolRegistry and look up the
calling agent's scratch directory to scope file operations.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from taskgenie.config import CONFIG
from taskgenie.tools.registry import REGISTRY

# Env vars that always pass through to subprocesses. Everything else is stripped
# so secrets (API keys, etc.) don't leak into untrusted-by-default exec_code.
# Users can add more via TASKGENIE_EXEC_ENV_PASSTHROUGH=KEY1,KEY2.
_DEFAULT_PASSTHROUGH = {"PATH", "HOME", "LANG", "LC_ALL", "TZ", "TMPDIR", "TEMP", "TMP"}

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

_READ_WORKSPACE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_workspace",
        "description": (
            "Read a file from the SHARED WORKSPACE. The workspace is where the team's "
            "deliverable artifacts live -- code, docs, art, anything the user will keep. "
            "Use this to verify what other agents have produced, or to extend an existing "
            "file. Distinct from `read_file` (your private scratch)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path relative to the workspace root.",
                }
            },
            "required": ["path"],
        },
    },
}

_WRITE_WORKSPACE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "write_workspace",
        "description": (
            "Write a file into the SHARED WORKSPACE -- the directory of REAL deliverables "
            "the user will end up with (e.g. game.py, index.html, README.md, assets/...). "
            "Workers use this when the task is producing software, documents, art, or any "
            "file artifact. Creates parent directories as needed. Distinct from "
            "`write_file` (your private scratch for intermediate work). Use scratch for "
            "thinking, workspace for shipping. "
            "\n\n"
            "MODES (chooses how to handle an existing file at the same path):\n"
            "  - 'create' (default): FAILS if the file already exists. Use for new files. "
            "Catches accidental clobbers loud rather than silent. If you got 'file exists', "
            "you probably want `edit_workspace` instead -- not a brute overwrite.\n"
            "  - 'overwrite': replaces existing content. Use ONLY when you genuinely "
            "intend to discard whatever was there.\n"
            "  - 'append': appends to the existing file (creates if missing). Safe for "
            "log-style additions."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path relative to the workspace root, e.g. 'src/game.py' or 'README.md'.",
                },
                "content": {"type": "string"},
                "mode": {
                    "type": "string",
                    "enum": ["create", "overwrite", "append"],
                    "description": "How to handle an existing file. Defaults to 'create'.",
                },
            },
            "required": ["path", "content"],
        },
    },
}

_EDIT_WORKSPACE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "edit_workspace",
        "description": (
            "Make a SURGICAL edit to an existing workspace file: replace exactly one "
            "occurrence of `old_text` with `new_text`. Reads the file, validates that "
            "`old_text` appears EXACTLY ONCE, then atomically writes the modified content "
            "back. \n\n"
            "Use this for: modifying existing files (a teammate's draft, a config, a "
            "code section). Always preferable to `write_workspace(mode='overwrite')` for "
            "edits because it can't accidentally discard work you didn't read.\n\n"
            "FAILURE MODES:\n"
            "  - file doesn't exist -> use `write_workspace(mode='create')`\n"
            "  - old_text not found -> the file changed since you read it (another worker "
            "edited it), OR your old_text is wrong. Re-read the file and try again.\n"
            "  - old_text found multiple times -> ambiguous. Include more surrounding "
            "context in old_text to make it unique.\n\n"
            "This is your safe tool for read-modify-write cycles; concurrent edits to the "
            "same file will surface as conflict errors (you re-read, you retry) rather "
            "than as silent data loss."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path relative to the workspace root.",
                },
                "old_text": {
                    "type": "string",
                    "description": "Exact text to find. Must appear in the file exactly once.",
                },
                "new_text": {
                    "type": "string",
                    "description": "Replacement text.",
                },
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
}

_LIST_WORKSPACE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_workspace",
        "description": (
            "List every file currently in the shared workspace, with byte sizes. Use this "
            "to discover what teammates have produced, to verify a subordinate actually "
            "delivered files (not just claimed to in their report), or to plan how a new "
            "file should fit alongside the existing ones."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}

_EXEC_CODE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "exec_code",
        "description": (
            "Execute a short Python or shell snippet with a timeout. Working directory is "
            "your scratch dir; env is scrubbed (only PATH/HOME/LANG-style vars present). "
            "Returns exit code + captured stdout + stderr. State does NOT persist between "
            "calls — write to a file via write_file if you need to keep something. "
            "This is timeout-and-cwd protection, NOT a security sandbox; the operator runs "
            "TaskGenie on their own machine."
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
    return p.read_text(encoding="utf-8")


async def _write_file(agent: Any, args: dict[str, Any]) -> str:
    p = _safe_path(agent.scratch_dir, args["path"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(args["content"], encoding="utf-8")
    return f"wrote {len(args['content'])} chars to {args['path']}"


def _workspace_or_error(agent: Any) -> Path | str:
    if not getattr(agent, "workspace_dir", None):
        return "ERROR: workspace is not configured for this session"
    base = Path(agent.workspace_dir)
    base.mkdir(parents=True, exist_ok=True)
    return base


async def _read_workspace(agent: Any, args: dict[str, Any]) -> str:
    base = _workspace_or_error(agent)
    if isinstance(base, str):
        return base
    p = _safe_path(str(base), args["path"])
    if not p.exists():
        return f"ERROR: file does not exist in workspace: {args['path']}"
    return p.read_text(encoding="utf-8")


async def _write_workspace(agent: Any, args: dict[str, Any]) -> str:
    base = _workspace_or_error(agent)
    if isinstance(base, str):
        return base
    p = _safe_path(str(base), args["path"])
    p.parent.mkdir(parents=True, exist_ok=True)
    content = args["content"]
    mode = args.get("mode", "create")

    if mode == "create":
        if p.exists():
            return (
                f"ERROR: workspace/{args['path']} already exists. "
                "Use `edit_workspace` to modify it, or pass mode='overwrite' if you "
                "really intend to discard the current contents."
            )
        p.write_text(content, encoding="utf-8")
        return f"wrote (created) {len(content)} chars to workspace/{args['path']}"

    if mode == "overwrite":
        p.write_text(content, encoding="utf-8")
        return f"wrote (overwrote) {len(content)} chars to workspace/{args['path']}"

    if mode == "append":
        # Open in append mode so concurrent appends from multiple agents
        # interleave cleanly at the OS write boundary instead of stomping
        # each other.
        with open(p, "a", encoding="utf-8") as f:
            f.write(content)
        return f"appended {len(content)} chars to workspace/{args['path']}"

    return f"ERROR: unknown mode {mode!r}; expected one of create / overwrite / append"


async def _edit_workspace(agent: Any, args: dict[str, Any]) -> str:
    base = _workspace_or_error(agent)
    if isinstance(base, str):
        return base
    p = _safe_path(str(base), args["path"])
    if not p.exists():
        return (
            f"ERROR: workspace/{args['path']} does not exist. "
            "Use `write_workspace(mode='create')` for a new file."
        )
    current = p.read_text(encoding="utf-8")
    old = args["old_text"]
    new = args["new_text"]
    count = current.count(old)
    if count == 0:
        return (
            f"ERROR: old_text not found in workspace/{args['path']}. "
            "The file may have been modified by another agent since you last read it -- "
            "re-read the file and retry with the actual current text."
        )
    if count > 1:
        return (
            f"ERROR: old_text appears {count} times in workspace/{args['path']}; "
            "the edit is ambiguous. Include more surrounding context in old_text so "
            "it matches exactly once."
        )
    updated = current.replace(old, new, 1)
    # Atomic-ish replace: write to a sibling temp then rename. Avoids leaving a
    # half-written file if the process dies mid-write.
    tmp = p.with_suffix(p.suffix + ".tg-edit-tmp")
    tmp.write_text(updated, encoding="utf-8")
    tmp.replace(p)
    return (
        f"edited workspace/{args['path']} "
        f"({len(old)} chars -> {len(new)} chars)"
    )


async def _list_workspace(agent: Any, args: dict[str, Any]) -> str:
    base = _workspace_or_error(agent)
    if isinstance(base, str):
        return base
    files = sorted(p for p in base.rglob("*") if p.is_file())
    if not files:
        return "(workspace is empty)"
    lines = [f"workspace root: {base}"]
    for p in files:
        rel = p.relative_to(base)
        lines.append(f"  {rel}  ({p.stat().st_size} bytes)")
    return "\n".join(lines)


def _scrubbed_env() -> dict[str, str]:
    extras = os.environ.get("TASKGENIE_EXEC_ENV_PASSTHROUGH", "")
    passthrough = _DEFAULT_PASSTHROUGH | {k.strip() for k in extras.split(",") if k.strip()}
    return {k: v for k, v in os.environ.items() if k in passthrough}


def _resolve_argv(language: str, code: str) -> list[str] | None:
    if language == "python":
        return [sys.executable, "-c", code]
    if language == "shell":
        shell = shutil.which("bash") or shutil.which("sh")
        if shell:
            return [shell, "-c", code]
        if os.name == "nt":
            return [os.environ.get("COMSPEC", "cmd.exe"), "/c", code]
        return None
    return None


async def _exec_code(agent: Any, args: dict[str, Any]) -> str:
    language = args["language"]
    code = args["code"]
    argv = _resolve_argv(language, code)
    if argv is None:
        return f"[exec_code error] no interpreter found for language={language!r}"

    cwd = agent.scratch_dir
    Path(cwd).mkdir(parents=True, exist_ok=True)
    env = _scrubbed_env()

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        return f"[exec_code error] could not launch {argv[0]}: {e}"
    except OSError as e:
        return f"[exec_code error] {type(e).__name__}: {e}"

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=CONFIG.exec_timeout_seconds
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        return (
            f"[exec_code TIMEOUT after {CONFIG.exec_timeout_seconds}s] "
            "process killed. Consider shorter / chunked work."
        )

    def _decode(b: bytes) -> str:
        try:
            return b.decode("utf-8", errors="replace")
        except Exception:
            return repr(b)

    out = _decode(stdout)
    err = _decode(stderr)
    return (
        f"exit_code={proc.returncode}\n"
        f"--- stdout ---\n{out or '(empty)'}\n"
        f"--- stderr ---\n{err or '(empty)'}"
    )


def scratch_for(agent_id: str, root: str | None = None) -> str:
    base = root or str(CONFIG.scratch_dir)
    path = os.path.join(base, agent_id)
    os.makedirs(path, exist_ok=True)
    return path


def register_builtin_work_tools() -> None:
    REGISTRY.register(_READ_FILE_SCHEMA, kind="work", version="1.0", dispatcher=_read_file)
    REGISTRY.register(_WRITE_FILE_SCHEMA, kind="work", version="1.0", dispatcher=_write_file)
    REGISTRY.register(_EXEC_CODE_SCHEMA, kind="work", version="0.1", dispatcher=_exec_code)
    # Workspace is shared across all agents in a session: workers write/edit,
    # managers and CEO read to verify deliverables.
    REGISTRY.register(_WRITE_WORKSPACE_SCHEMA, kind="work", version="1.1", dispatcher=_write_workspace)
    REGISTRY.register(_EDIT_WORKSPACE_SCHEMA, kind="work", version="1.0", dispatcher=_edit_workspace)
    REGISTRY.register(_READ_WORKSPACE_SCHEMA, kind="both", version="1.0", dispatcher=_read_workspace)
    REGISTRY.register(_LIST_WORKSPACE_SCHEMA, kind="both", version="1.0", dispatcher=_list_workspace)


register_builtin_work_tools()
