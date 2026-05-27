"""Built-in worker tool schemas + dispatchers.

Dispatchers are registered with the global ToolRegistry and look up the
calling agent's scratch directory to scope file operations.
"""
from __future__ import annotations

import asyncio
import errno
import json
import os
import re
import shutil
import signal
import stat
import sys
import tempfile
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
        "description": (
            "Read a file from your PRIVATE scratch directory -- no other agent "
            "sees these. Returns the file contents as a string. For SHARED "
            "deliverables, use read_workspace instead."
        ),
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
        "description": (
            "Write content to a file in your PRIVATE scratch directory -- no "
            "other agent sees these. Creates parent directories as needed. "
            "For SHARED deliverables, use write_workspace instead."
        ),
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
            "Write a BRAND-NEW file into the SHARED WORKSPACE -- the directory of REAL "
            "deliverables the user will end up with (e.g. game.py, index.html, README.md, "
            "assets/...). Workers use this when CREATING a new file artifact. Creates "
            "parent directories as needed. Distinct from `write_file` (your private "
            "scratch for intermediate work).\n\n"
            "IF YOU ARE MODIFYING AN EXISTING FILE, USE `edit_workspace` INSTEAD, NOT "
            "THIS TOOL. A read_workspace + write_workspace(mode='overwrite') sequence is "
            "the textbook lost-update bug; another agent's changes silently disappear. "
            "edit_workspace can't do that.\n\n"
            "MODES (chooses how to handle an existing file at the same path):\n"
            "  - 'create' (DEFAULT, almost always the right choice): FAILS if the file "
            "already exists. Catches accidental clobbers loud rather than silent. If you "
            "got 'file exists', SWITCH TO `edit_workspace` -- do NOT retry with overwrite.\n"
            "  - 'overwrite': replaces existing content WHOLESALE. Use ONLY when you are "
            "regenerating a file from scratch with NO regard for prior content (e.g. you "
            "did NOT call read_workspace on this path). If you read the file first, you "
            "should be editing, not overwriting.\n"
            "  - 'append': appends to the existing file (creates if missing). Safe for "
            "log-style additions where order matters and no rewriting is needed."
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
            "Execute a short Python or shell snippet IN YOUR SCRATCH directory (CWD = "
            "scratch). Use this for intermediate computation that produces no shared "
            "artifact -- quick calculations, validating a regex, scratch-only file "
            "transforms.\n\n"
            "If you want to RUN a deliverable file you wrote to the shared workspace "
            "via `write_workspace` (e.g. test that game.py runs), use `exec_workspace` "
            "instead -- its CWD is the workspace so `python game.py` just works.\n\n"
            "Env is scrubbed (PATH/HOME/LANG-style only, plus $TASKGENIE_WORKSPACE / "
            "$TASKGENIE_SCRATCH as escape hatches if you need to reach the other dir). "
            "State does NOT persist between calls — write to a file via write_file if "
            "you need to keep something. This is timeout-and-cwd protection, NOT a "
            "security sandbox; the operator runs TaskGenie on their own machine."
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

_EXEC_WORKSPACE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "exec_workspace",
        "description": (
            "Execute a short Python or shell snippet IN THE SHARED WORKSPACE directory "
            "(CWD = workspace). Use this for any work where the snippet must SEE or "
            "PRODUCE shared deliverables:\n"
            "  - run/test existing artifacts: `python game.py`, `pytest`, `node server.js`\n"
            "  - generate scaffolding INTO workspace: `npm init -y`, `cookiecutter ...`, "
            "`git clone ... .`, `cargo init`\n"
            "Relative paths in your snippet resolve against the workspace root, so "
            "portable commands just work.\n\n"
            "The mirror of `exec_code` (which runs in scratch). Same timeout, same "
            "env-scrub semantics. State does NOT persist between calls. Env vars "
            "`$TASKGENIE_WORKSPACE` (the CWD's absolute path) and `$TASKGENIE_SCRATCH` "
            "(your private dir) are BOTH exposed, so a snippet that reads from one and "
            "writes to the other can.\n\n"
            "Use `exec_code` instead when your snippet doesn't touch workspace at all "
            "-- quick math, regex check, scratch-only file transform."
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


_PATH_FORBIDDEN_RE = re.compile(r"[\x00-\x1f\x7f]")


def _safe_path(scratch_dir: str, rel_path: str) -> Path:
    bad = _PATH_FORBIDDEN_RE.search(rel_path)
    if bad is not None:
        raise ValueError(
            f"path contains control character {bad.group()!r} (0x"
            f"{ord(bad.group()):02x}): {rel_path!r}"
        )
    base = Path(scratch_dir).resolve()
    base.mkdir(parents=True, exist_ok=True)
    target = (base / rel_path).resolve()
    # Use parents-containment (NOT str.startswith) so a sibling whose
    # name shares a prefix with base -- e.g. /tmp/scratch-evil vs
    # /tmp/scratch -- is correctly rejected. target == base is also fine.
    if target != base and base not in target.parents:
        raise ValueError(f"Path escapes scratch dir: {rel_path}")
    return target


class _UnsafeOpenError(OSError):
    """Raised by `_safe_open` when the post-open verification detects a
    hardlink, a parent-component symlink traversal, or any other path
    escape. Distinct from ELOOP (leaf symlink, caught by O_NOFOLLOW) so
    dispatchers can produce a precise error message for the agent.
    """


def _safe_open(
    p: Path, base: Path, base_flags: int, perm: int = 0o600
) -> int:
    """Open `p` defensively against the full path-safety attack surface:

      1. Leaf symlink -- `O_NOFOLLOW` on the open call (kernel-level).
      2. Hardlink -- after open, `fstat` the fd and refuse if
         `st_nlink > 1`. `O_NOFOLLOW` does NOT cover this; a hardlink
         shares an inode with another path but has its own dirent that
         isn't a symlink, so the open succeeds.
      3. Parent-component symlink (TOCTOU race where an attacker plants
         `subdir` as a symlink between `_safe_path`'s validation and the
         open) -- after open, read the fd's true path via
         `/proc/self/fd/N` (Linux) and re-verify containment against
         `base.resolve()`. `O_NOFOLLOW` only protects the LEAF, not
         intermediate components, so the kernel happily traverses
         `subdir -> /tmp` and opens `/tmp/file.txt`.

    On non-Linux systems without `/proc/self/fd`, the parent-component
    check is skipped; we still get hardlink + leaf-symlink protection.
    Windows has no `O_NOFOLLOW` and no `st_nlink` semantics; the open
    proceeds with whatever flags are available. (The threat model is
    "operator runs TaskGenie on their own machine", so this is best-
    effort hardening, not a security boundary.)
    """
    flags = base_flags
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY

    # Defer O_TRUNC until AFTER the post-open validation. If we let the
    # kernel truncate at open time, a hardlink-to-/etc/passwd would be
    # truncated to zero BEFORE we got a chance to refuse the write -- our
    # "refused" return value would arrive with /etc/passwd already empty.
    # The caller's `os.write` (or `ftruncate(fd, 0)` for overwrite) does
    # the actual content change, after validation has cleared it.
    defer_trunc = bool(flags & os.O_TRUNC)
    if defer_trunc:
        flags &= ~os.O_TRUNC
    # If we're using O_CREAT|O_EXCL (mode='create'), the kernel atomically
    # creates the file at the leaf BEFORE our post-open validation runs.
    # If validation then rejects (e.g. parent symlink escaped), we want to
    # rollback the create so we don't leave zero-byte detritus in the
    # attacker-controlled location. Safe to unlink because EXCL guarantees
    # WE created the file just now.
    created_excl = bool(flags & os.O_EXCL) and bool(flags & os.O_CREAT)

    fd = os.open(str(p), flags, perm)
    try:
        st = os.fstat(fd)
        if hasattr(st, "st_nlink") and st.st_nlink > 1:
            raise _UnsafeOpenError(
                errno.EMLINK,
                f"refused: {p!r} is a hardlink (st_nlink={st.st_nlink}); "
                "multiple paths reference this inode, distrust for I/O",
            )

        # Parent-component TOCTOU re-check. /proc/self/fd/N gives the
        # CANONICAL path of the open fd, after all symlink resolution.
        # If it lands outside the base, the kernel followed a planted
        # parent symlink between validation and open.
        proc_link = f"/proc/self/fd/{fd}"
        if os.path.exists(proc_link):
            try:
                actual = os.readlink(proc_link)
            except OSError:
                actual = None
            if actual is not None:
                actual_p = Path(actual)
                base_resolved = Path(base).resolve()
                if (
                    actual_p != base_resolved
                    and base_resolved not in actual_p.parents
                ):
                    raise _UnsafeOpenError(
                        errno.ENOTRECOVERABLE
                        if hasattr(errno, "ENOTRECOVERABLE")
                        else errno.EACCES,
                        f"refused: {p!r} resolves to {actual!r} which "
                        f"escapes base {str(base_resolved)!r} (likely a "
                        "parent-component symlink planted after validation)",
                    )
        # All checks passed -- now safe to apply the deferred truncate.
        if defer_trunc:
            os.ftruncate(fd, 0)
    except BaseException:
        if created_excl:
            # Roll back the empty file we just atomically created via
            # O_EXCL so the attacker-controlled location (if any) is
            # left clean. Best-effort; ignore if unlink fails.
            try:
                os.unlink(str(p))
            except OSError:
                pass
        os.close(fd)
        raise

    return fd


def _write_text_safe(p: Path, base: Path, content: str, mode: str) -> None:
    """UTF-8 write defended against symlink + hardlink + parent-TOCTOU.

    `mode` chooses the open semantics:
      - 'create'    -> O_CREAT|O_EXCL  (fails if exists, atomically)
      - 'overwrite' -> O_CREAT|O_TRUNC
      - 'append'    -> O_CREAT|O_APPEND
    """
    if mode == "create":
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    elif mode == "overwrite":
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    elif mode == "append":
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    else:
        raise ValueError(f"unknown write mode: {mode!r}")

    fd = _safe_open(p, base, flags)
    try:
        data = content.encode("utf-8")
        view = memoryview(data)
        while view:
            n = os.write(fd, view)
            view = view[n:]
    finally:
        os.close(fd)


def _read_text_safe(p: Path, base: Path) -> str:
    """UTF-8 read with the same safety guarantees as `_write_text_safe`.

    Raises FileNotFoundError when `p` doesn't exist (caller maps to a
    friendly tool error string); raises `_UnsafeOpenError` on hardlink
    or parent-symlink traversal.
    """
    fd = _safe_open(p, base, os.O_RDONLY)
    try:
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(fd)
    return b"".join(chunks).decode("utf-8")


def _path_safety_error(label: str, e: OSError) -> str | None:
    """Map an OSError from `_safe_open` to a friendly agent-facing error
    string, or None if it's not a path-safety error (caller should re-raise).
    """
    if isinstance(e, _UnsafeOpenError):
        return f"ERROR: refused unsafe path at {label!r}: {e.strerror}"
    if e.errno == errno.ELOOP:
        return (
            f"ERROR: refused to follow symlink at {label!r}. "
            "TaskGenie writes never traverse symlinks at the leaf path."
        )
    return None


def _scratch_base(agent: Any) -> Path:
    return Path(agent.scratch_dir)


async def _read_file(agent: Any, args: dict[str, Any]) -> str:
    base = _scratch_base(agent)
    p = _safe_path(str(base), args["path"])
    try:
        return _read_text_safe(p, base)
    except FileNotFoundError:
        return f"ERROR: file does not exist: {args['path']}"
    except OSError as e:
        msg = _path_safety_error(args["path"], e)
        if msg is not None:
            return msg
        raise


async def _write_file(agent: Any, args: dict[str, Any]) -> str:
    base = _scratch_base(agent)
    p = _safe_path(str(base), args["path"])
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        _write_text_safe(p, base, args["content"], "overwrite")
    except OSError as e:
        msg = _path_safety_error(args["path"], e)
        if msg is not None:
            return msg
        raise
    return f"wrote {len(args['content'])} chars to {args['path']}"


def _workspace_or_error(agent: Any) -> Path | str:
    if not getattr(agent, "workspace_dir", None):
        return "ERROR: workspace is not configured for this session"
    base = Path(agent.workspace_dir)
    base.mkdir(parents=True, exist_ok=True)
    return base


def _normalize_workspace_path(p: str) -> str:
    """Canonicalise a workspace-relative path so `./game.py`, `game.py`,
    and `dir/../game.py` all hash to the same key in `read_workspace_paths`."""
    return os.path.normpath(p)


async def _read_workspace(agent: Any, args: dict[str, Any]) -> str:
    base = _workspace_or_error(agent)
    if isinstance(base, str):
        return base
    p = _safe_path(str(base), args["path"])
    try:
        text = _read_text_safe(p, base)
    except FileNotFoundError:
        return f"ERROR: file does not exist in workspace: {args['path']}"
    except OSError as e:
        msg = _path_safety_error(f"workspace/{args['path']}", e)
        if msg is not None:
            return msg
        raise
    # Record so write_workspace(mode='overwrite') can warn the agent about
    # read-then-overwrite (lost-update anti-pattern) even after the
    # Archivist has compacted the original tool_call out of agent.messages.
    # Normalised so `./foo` and `foo` are the same lookup key.
    paths = getattr(agent, "read_workspace_paths", None)
    if paths is not None:
        paths.add(_normalize_workspace_path(args["path"]))
    return text


def _agent_read_workspace_paths(agent: Any) -> set[str]:
    """Paths this agent has read from workspace this run, as a normalised
    set. Source of truth is `agent.read_workspace_paths` (populated by
    `_read_workspace` on SUCCESSFUL reads only -- failed reads do NOT
    populate). This is a persistent set that survives Archivist
    compaction.

    Before round-three we also scanned `agent.messages` for read_workspace
    tool_calls; that union false-positived on FAILED reads (the message
    scan saw the tool_call but not whether the response was an ERROR),
    spuriously warning on fresh files.
    """
    explicit = getattr(agent, "read_workspace_paths", None)
    if explicit is None:
        return set()
    return {_normalize_workspace_path(p) for p in explicit}


async def _write_workspace(agent: Any, args: dict[str, Any]) -> str:
    base = _workspace_or_error(agent)
    if isinstance(base, str):
        return base
    p = _safe_path(str(base), args["path"])
    p.parent.mkdir(parents=True, exist_ok=True)
    content = args["content"]
    mode = args.get("mode", "create")
    norm_path = _normalize_workspace_path(args["path"])

    if mode == "create":
        try:
            _write_text_safe(p, base, content, "create")
        except FileExistsError:
            return (
                f"ERROR: workspace/{args['path']} already exists. "
                "Use `edit_workspace` to modify it surgically. If you genuinely "
                "want to discard the current contents (rare), retry with "
                "mode='overwrite'."
            )
        except OSError as e:
            msg = _path_safety_error(f"workspace/{args['path']}", e)
            if msg is not None:
                return msg
            raise
        return f"wrote (created) {len(content)} chars to workspace/{args['path']}"

    if mode == "overwrite":
        # Lost-update detector: if this agent previously read this path,
        # appending a warning to the tool response makes the model aware
        # they're about to make the read+overwrite mistake. We still write
        # (overwrite is a legitimate sometimes-tool), but the warning steers
        # next-turn decisions toward edit_workspace.
        previously_read = norm_path in _agent_read_workspace_paths(agent)
        try:
            _write_text_safe(p, base, content, "overwrite")
        except OSError as e:
            msg = _path_safety_error(f"workspace/{args['path']}", e)
            if msg is not None:
                return msg
            raise
        msg = f"wrote (overwrote) {len(content)} chars to workspace/{args['path']}"
        if previously_read:
            msg += (
                "\nWARNING: you previously read this file and then overwrote it. "
                "If you intended to MODIFY it, you just may have lost another "
                "agent's concurrent changes. Use `edit_workspace(old_text, "
                "new_text)` for read-then-modify cycles -- it surfaces conflicts "
                "as errors instead of as silent data loss."
            )
        return msg

    if mode == "append":
        # Single os.write() of the encoded payload. POSIX guarantees this is
        # atomic for sizes <= PIPE_BUF (4096); above that, two concurrent
        # writers may interleave. We document the limit in the schema rather
        # than block large appends.
        try:
            _write_text_safe(p, base, content, "append")
        except OSError as e:
            msg = _path_safety_error(f"workspace/{args['path']}", e)
            if msg is not None:
                return msg
            raise
        return f"appended {len(content)} chars to workspace/{args['path']}"

    return f"ERROR: unknown mode {mode!r}; expected one of create / overwrite / append"


async def _edit_workspace(agent: Any, args: dict[str, Any]) -> str:
    base = _workspace_or_error(agent)
    if isinstance(base, str):
        return base
    p = _safe_path(str(base), args["path"])
    try:
        current = _read_text_safe(p, base)
    except FileNotFoundError:
        return (
            f"ERROR: workspace/{args['path']} does not exist. "
            "Use `write_workspace(mode='create')` for a new file."
        )
    except OSError as e:
        msg = _path_safety_error(f"workspace/{args['path']}", e)
        if msg is not None:
            return msg
        raise
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

    # mkstemp opens with O_CREAT|O_EXCL (and O_NOFOLLOW on POSIX), with a
    # random suffix so concurrent edits don't collide on a deterministic
    # `.foo.py.tg-edit-tmp` path. tempfile.mkstemp uses _bin_openflags which
    # includes O_NOFOLLOW on platforms that support it.
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=str(p.parent),
        prefix=f".{p.name}.",
        suffix=".tg-edit-tmp",
    )
    consumed_by_rename = False
    try:
        data = updated.encode("utf-8")
        view = memoryview(data)
        while view:
            n = os.write(tmp_fd, view)
            view = view[n:]
        os.close(tmp_fd)
        tmp_fd = -1
        os.replace(tmp_path, str(p))
        consumed_by_rename = True
    finally:
        if tmp_fd != -1:
            try:
                os.close(tmp_fd)
            except OSError:
                pass
        if not consumed_by_rename:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    return (
        f"edited workspace/{args['path']} "
        f"({len(old)} chars -> {len(new)} chars)"
    )


async def _list_workspace(agent: Any, args: dict[str, Any]) -> str:
    base = _workspace_or_error(agent)
    if isinstance(base, str):
        return base
    entries: list[tuple[str, str]] = []  # (relative_path, annotation)
    symlinks: list[tuple[str, str]] = []
    for p in sorted(base.rglob("*")):
        rel = p.relative_to(base)
        # Filter edit_workspace tempfiles -- they're internal artifacts
        # only left behind by SIGKILL mid-edit; shouldn't pollute the
        # agent's view of what they produced.
        if p.name.endswith(".tg-edit-tmp"):
            continue
        # lstat avoids following symlinks; surface them explicitly so a
        # planted link is visible rather than blending in as a regular
        # file. Also recon-resistant -- the agent can't use list_workspace
        # to confirm a planted symlink's target is readable.
        try:
            st = p.lstat()
        except OSError:
            continue
        if stat.S_ISLNK(st.st_mode):
            try:
                target = os.readlink(str(p))
            except OSError:
                target = "?"
            symlinks.append((str(rel), f"→ {target}"))
            continue
        if stat.S_ISREG(st.st_mode):
            entries.append((str(rel), f"{st.st_size} bytes"))
            continue
        # Dirs / sockets / pipes / etc. are ignored for the listing.
    if not entries and not symlinks:
        return "(workspace is empty)"
    lines = [f"workspace root: {base}"]
    for rel, annot in entries:
        lines.append(f"  {rel}  ({annot})")
    if symlinks:
        lines.append("")
        lines.append("symlinks (not opened by read/write tools):")
        for rel, annot in symlinks:
            lines.append(f"  {rel}  {annot}")
    return "\n".join(lines)


def _scrubbed_env(agent: Any | None = None) -> dict[str, str]:
    extras = os.environ.get("TASKGENIE_EXEC_ENV_PASSTHROUGH", "")
    passthrough = _DEFAULT_PASSTHROUGH | {k.strip() for k in extras.split(",") if k.strip()}
    env = {k: v for k, v in os.environ.items() if k in passthrough}
    # Expose the workspace + scratch paths so exec_code snippets can act on
    # files the worker wrote with `write_workspace`. Without this the agent
    # has no way to run their own deliverable (cwd is scratch, the workspace
    # path is never named anywhere they can see).
    if agent is not None:
        if getattr(agent, "workspace_dir", None):
            env["TASKGENIE_WORKSPACE"] = str(agent.workspace_dir)
        if getattr(agent, "scratch_dir", None):
            env["TASKGENIE_SCRATCH"] = str(agent.scratch_dir)
    return env


def _kill_proc_tree(proc: asyncio.subprocess.Process, on_posix: bool) -> None:
    """Kill a subprocess and any children it spawned.

    On POSIX (after start_new_session=True) the subprocess is the leader
    of its own process group; killpg targets the whole group. On Windows
    or if killpg fails for any reason, fall back to killing the parent
    directly -- best-effort.
    """
    if on_posix:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            # Group already gone, or we never managed to put the child in
            # its own group; fall through to the direct kill below.
            pass
    try:
        proc.kill()
    except ProcessLookupError:
        pass


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


async def _run_subprocess(
    agent: Any, args: dict[str, Any], cwd: str, label: str
) -> str:
    language = args["language"]
    code = args["code"]
    argv = _resolve_argv(language, code)
    if argv is None:
        return f"[{label} error] no interpreter found for language={language!r}"

    Path(cwd).mkdir(parents=True, exist_ok=True)
    env = _scrubbed_env(agent)

    # start_new_session puts the child in its own process group on POSIX so
    # we can kill the whole group on timeout. Without this, a snippet like
    # `bash -c "sleep 9999 &"` would leave the inner sleep orphaned even
    # after we kill the shell. Windows doesn't support it (or process groups
    # in the same way) -- fall back to plain proc.kill() there.
    on_posix = os.name != "nt"
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=on_posix,
        )
    except FileNotFoundError as e:
        return f"[{label} error] could not launch {argv[0]}: {e}"
    except OSError as e:
        return f"[{label} error] {type(e).__name__}: {e}"

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=CONFIG.exec_timeout_seconds
        )
    except asyncio.TimeoutError:
        _kill_proc_tree(proc, on_posix)
        await proc.wait()
        return (
            f"[{label} TIMEOUT after {CONFIG.exec_timeout_seconds}s] "
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


async def _exec_code(agent: Any, args: dict[str, Any]) -> str:
    """Run the snippet with CWD = scratch."""
    return await _run_subprocess(agent, args, cwd=agent.scratch_dir, label="exec_code")


async def _exec_workspace(agent: Any, args: dict[str, Any]) -> str:
    """Run the snippet with CWD = workspace. Mirrors `exec_code` but
    relative paths resolve against the shared workspace root, so a
    worker who wrote `game.py` via `write_workspace` can immediately
    `python game.py` to verify it runs."""
    base = _workspace_or_error(agent)
    if isinstance(base, str):
        return base
    return await _run_subprocess(agent, args, cwd=str(base), label="exec_workspace")


def scratch_for(agent_id: str, root: str | None = None) -> str:
    base = root or str(CONFIG.scratch_dir)
    path = os.path.join(base, agent_id)
    os.makedirs(path, exist_ok=True)
    return path


def register_builtin_work_tools() -> None:
    # Order matters for the agent's reading: present scratch tools first
    # (read/write/exec triplet on scratch), then the workspace tools
    # (list/read/write/edit/exec quintet). Cross-references in tool
    # descriptions resolve in reading order this way.
    REGISTRY.register(_READ_FILE_SCHEMA, kind="work", version="1.0", dispatcher=_read_file)
    REGISTRY.register(_WRITE_FILE_SCHEMA, kind="work", version="1.0", dispatcher=_write_file)
    REGISTRY.register(_EXEC_CODE_SCHEMA, kind="work", version="0.3", dispatcher=_exec_code)
    # Workspace is shared across all agents in a session: workers write/edit/exec,
    # managers and CEO read to verify deliverables.
    REGISTRY.register(_LIST_WORKSPACE_SCHEMA, kind="both", version="1.0", dispatcher=_list_workspace)
    REGISTRY.register(_READ_WORKSPACE_SCHEMA, kind="both", version="1.0", dispatcher=_read_workspace)
    REGISTRY.register(_WRITE_WORKSPACE_SCHEMA, kind="work", version="1.1", dispatcher=_write_workspace)
    REGISTRY.register(_EDIT_WORKSPACE_SCHEMA, kind="work", version="1.0", dispatcher=_edit_workspace)
    REGISTRY.register(
        _EXEC_WORKSPACE_SCHEMA, kind="work", version="1.0", dispatcher=_exec_workspace
    )


register_builtin_work_tools()
