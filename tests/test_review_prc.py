"""Tests for PR C -- resource safety.

C1. MCP _call_tool wraps session.call_tool in asyncio.wait_for; a hung
    MCP server returns a specific timeout error string instead of
    blocking forever.

C2. exec_code subprocess is launched with start_new_session=True on
    POSIX so we can kill the whole process group on timeout. Snippets
    that background work (e.g. `bash -c "sleep 9999 &"`) no longer
    orphan child processes when we time out.

C3. MCPClient.close() logs cleanup failures to stderr instead of
    silently swallowing them.

C4. edit_workspace cleans up its .tg-edit-tmp sibling if the rename
    fails (cross-device, permission, etc.) so a workspace doesn't
    accumulate stale temp files.

Run with: python -m tests.test_review_prc
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from taskgenie.config import CONFIG
from taskgenie.mcp_client import MCPClient
from taskgenie.tools import work as work_module


# ---------------------------------------------------------------------------
# C1: MCP timeout
# ---------------------------------------------------------------------------


class _HungSession:
    """Mock ClientSession whose call_tool never returns."""

    async def call_tool(self, name: str, arguments: dict) -> None:
        # Sleep way past any reasonable timeout. The test uses a tiny
        # CONFIG.mcp_call_timeout_seconds so wait_for will fire quickly.
        await asyncio.sleep(60.0)
        raise AssertionError("call_tool should not have completed")


async def test_mcp_call_tool_timeout_returns_specific_error() -> None:
    """A hung MCP server must produce the timeout marker, not block."""
    client = MCPClient()
    client._sessions["fake"] = _HungSession()  # type: ignore[assignment]
    original_timeout = CONFIG.mcp_call_timeout_seconds
    object.__setattr__(CONFIG, "mcp_call_timeout_seconds", 1)
    try:
        result = await client._call_tool("fake", "do_thing", {"arg": "x"})
    finally:
        object.__setattr__(CONFIG, "mcp_call_timeout_seconds", original_timeout)

    assert "[MCP_ERROR]" in result, result
    assert "timed out" in result, result
    assert "fake.do_thing" in result, result
    print("[mcp_call_tool_timeout_returns_specific_error] OK")


# ---------------------------------------------------------------------------
# C2: exec_code subprocess group
# ---------------------------------------------------------------------------


async def test_exec_code_uses_start_new_session_on_posix(tmp_path: Path) -> None:
    """exec_code must pass start_new_session=True to create_subprocess_exec
    so the child becomes its own process group leader. Without that, killpg
    on timeout has nothing to target. Windows can't do this -- skip there."""
    if sys.platform == "win32":
        print("[exec_code_uses_start_new_session_on_posix] SKIP (Windows)")
        return

    captured_kwargs: dict = {}
    original = asyncio.create_subprocess_exec

    async def spy(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return await original(*args, **kwargs)

    class _DummyAgent:
        scratch_dir = str(tmp_path)

    with patch.object(asyncio, "create_subprocess_exec", spy):
        await work_module._exec_code(
            _DummyAgent(),
            {"language": "python", "code": "print('hi')"},
        )
    assert captured_kwargs.get("start_new_session") is True, captured_kwargs
    print("[exec_code_uses_start_new_session_on_posix] OK")


async def test_exec_code_timeout_kills_cleanly(tmp_path: Path) -> None:
    """End-to-end: a snippet that sleeps longer than the timeout returns
    the TIMEOUT marker and doesn't hang. (Doesn't directly verify killpg's
    effect on orphans -- that's hard to test portably -- but does verify
    the timeout path runs without raising.)"""

    class _DummyAgent:
        scratch_dir = str(tmp_path)

    original_timeout = CONFIG.exec_timeout_seconds
    object.__setattr__(CONFIG, "exec_timeout_seconds", 1)
    try:
        result = await work_module._exec_code(
            _DummyAgent(),
            {"language": "python", "code": "import time; time.sleep(10)"},
        )
    finally:
        object.__setattr__(CONFIG, "exec_timeout_seconds", original_timeout)

    assert "TIMEOUT" in result, result
    assert "process killed" in result, result
    print("[exec_code_timeout_kills_cleanly] OK")


def test_kill_proc_tree_handles_dead_pid() -> None:
    """_kill_proc_tree must not raise if the pid is already gone."""

    class _DeadProc:
        pid = 999999  # extremely unlikely to be a live process

        def kill(self) -> None:
            raise ProcessLookupError

    # Should not raise.
    work_module._kill_proc_tree(_DeadProc(), on_posix=True)
    work_module._kill_proc_tree(_DeadProc(), on_posix=False)
    print("[kill_proc_tree_handles_dead_pid] OK")


# ---------------------------------------------------------------------------
# C3: MCP close() logs errors instead of swallowing
# ---------------------------------------------------------------------------


class _ExplodingStack:
    """AsyncExitStack stand-in whose aclose() raises."""

    async def __aenter__(self):
        return self

    async def aclose(self) -> None:
        raise RuntimeError("simulated cleanup failure")


async def test_mcp_close_logs_cleanup_errors_to_stderr() -> None:
    """A cleanup failure during close() used to vanish silently. Now it
    surfaces on stderr so a hung shutdown is debuggable."""
    client = MCPClient()
    client._stack = _ExplodingStack()  # type: ignore[assignment]
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        await client.close()
    output = buf.getvalue()
    assert "mcp_client" in output, output
    assert "cleanup error" in output, output
    assert "RuntimeError" in output, output
    assert "simulated cleanup failure" in output, output
    print("[mcp_close_logs_cleanup_errors_to_stderr] OK")


async def test_mcp_close_is_idempotent_after_error() -> None:
    """Second close() after a failed first close() must not double-log
    or re-raise -- _closed guards it."""
    client = MCPClient()
    client._stack = _ExplodingStack()  # type: ignore[assignment]
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        await client.close()
        await client.close()
    # Only one "cleanup error" line.
    assert buf.getvalue().count("cleanup error") == 1, buf.getvalue()
    print("[mcp_close_is_idempotent_after_error] OK")


# ---------------------------------------------------------------------------
# C4: edit_workspace cleans up its temp on rename failure
# ---------------------------------------------------------------------------


async def test_edit_workspace_cleans_up_temp_on_rename_failure(
    tmp_path: Path,
) -> None:
    """If tmp.replace(p) raises, we used to leave a stale .tg-edit-tmp
    behind. Verify it's cleaned up now.

    We patch the Path.replace method to raise so we don't need a real
    cross-device move."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / "file.txt"
    target.write_text("hello world", encoding="utf-8")

    class _Agent:
        workspace_dir = str(workspace)
        messages: list = []

    # Patch os.replace to raise. The new _edit_workspace uses tempfile
    # + os.replace (was Path.replace before; mkstemp gave us O_NOFOLLOW
    # for free), so we intercept at the actual call site.
    original_replace = work_module.os.replace

    def boom(src, dst):  # noqa: ARG001
        raise OSError(18, "cross-device link not permitted")

    work_module.os.replace = boom
    try:
        try:
            await work_module._edit_workspace(
                _Agent(),
                {"path": "file.txt", "old_text": "hello", "new_text": "HELLO"},
            )
        except OSError:
            pass  # expected -- we re-raise after cleanup
    finally:
        work_module.os.replace = original_replace

    # The crucial assertion: no stale .tg-edit-tmp left behind.
    stale = list(workspace.glob("*.tg-edit-tmp"))
    assert stale == [], f"stale temp files: {stale}"
    # Original target should still be intact (the edit never landed).
    assert target.read_text(encoding="utf-8") == "hello world"
    print("[edit_workspace_cleans_up_temp_on_rename_failure] OK")


async def test_edit_workspace_happy_path_still_works(tmp_path: Path) -> None:
    """Sanity: the cleanup wrapping didn't break the normal path."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / "file.txt"
    target.write_text("hello world", encoding="utf-8")

    class _Agent:
        workspace_dir = str(workspace)
        messages: list = []

    result = await work_module._edit_workspace(
        _Agent(),
        {"path": "file.txt", "old_text": "hello", "new_text": "HELLO"},
    )
    assert "edited" in result, result
    assert target.read_text(encoding="utf-8") == "HELLO world"
    # No stale temps after a successful edit either.
    assert list(workspace.glob("*.tg-edit-tmp")) == []
    print("[edit_workspace_happy_path_still_works] OK")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def _async_main() -> int:
    await test_mcp_call_tool_timeout_returns_specific_error()
    with tempfile.TemporaryDirectory() as d:
        await test_exec_code_uses_start_new_session_on_posix(Path(d))
    with tempfile.TemporaryDirectory() as d:
        await test_exec_code_timeout_kills_cleanly(Path(d))
    await test_mcp_close_logs_cleanup_errors_to_stderr()
    await test_mcp_close_is_idempotent_after_error()
    with tempfile.TemporaryDirectory() as d:
        await test_edit_workspace_cleans_up_temp_on_rename_failure(Path(d))
    with tempfile.TemporaryDirectory() as d:
        await test_edit_workspace_happy_path_still_works(Path(d))
    return 0


def main() -> int:
    asyncio.run(_async_main())
    test_kill_proc_tree_handles_dead_pid()
    print("\nAll PR C regression tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
