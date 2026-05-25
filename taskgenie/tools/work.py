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
    return p.read_text()


async def _write_file(agent: Any, args: dict[str, Any]) -> str:
    p = _safe_path(agent.scratch_dir, args["path"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(args["content"])
    return f"wrote {len(args['content'])} chars to {args['path']}"


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


register_builtin_work_tools()
