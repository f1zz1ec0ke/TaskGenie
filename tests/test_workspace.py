"""Workspace tooling: workers write real artifacts to a shared dir,
managers (and CEO) verify by listing/reading.

Run with: python -m tests.test_workspace
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from taskgenie.agent import Agent
from taskgenie.config import CONFIG
from taskgenie.prompts import worker_prompt, manager_prompt
from taskgenie.session import Session
from taskgenie.tools.registry import REGISTRY


async def test_worker_writes_workspace(tmp_path: Path) -> None:
    object.__setattr__(CONFIG, "data_dir", str(tmp_path))
    CONFIG.sessions_dir.mkdir(parents=True, exist_ok=True)
    Agent.REGISTRY.clear()

    session = Session.create("workspace task")
    Agent.SCRATCH_ROOT = str(session.scratch_root)
    Agent.WORKSPACE_ROOT = str(session.workspace_dir)

    worker = Agent(
        name="W",
        role_type="worker",
        system_prompt=worker_prompt("W", "Mgr", "produce game.py"),
    )
    mgr = Agent(
        name="M",
        role_type="manager",
        system_prompt=manager_prompt("M", "CEO", "supervise"),
    )

    # Worker writes a real file via write_workspace
    dispatch_write = REGISTRY.dispatcher("write_workspace")
    assert dispatch_write is not None
    res = await dispatch_write(worker, {"path": "game.py", "content": "print('pew')\n"})
    assert "wrote" in res and "game.py" in res

    # File exists at the expected path
    expected = session.workspace_dir / "game.py"
    assert expected.exists()
    assert expected.read_text() == "print('pew')\n"

    # Manager can list_workspace and see it
    dispatch_list = REGISTRY.dispatcher("list_workspace")
    assert dispatch_list is not None
    listing = await dispatch_list(mgr, {})
    assert "game.py" in listing
    assert "(13 bytes)" in listing

    # Manager can read_workspace
    dispatch_read = REGISTRY.dispatcher("read_workspace")
    assert dispatch_read is not None
    contents = await dispatch_read(mgr, {"path": "game.py"})
    assert contents == "print('pew')\n"

    # Workspace tool restricts traversal: ../ escape attempt raises
    # (the agent loop catches & wraps this into a tool_error string)
    try:
        await dispatch_write(worker, {"path": "../escape.py", "content": "x"})
        assert False, "expected path traversal to be rejected"
    except ValueError as e:
        assert "escapes" in str(e).lower()
    assert not (tmp_path / "escape.py").exists()

    print("[worker_writes_workspace] OK")


async def test_workspace_tool_kinds() -> None:
    """write_workspace = work-only; read/list = both."""
    Agent.REGISTRY.clear()
    Agent.WORKSPACE_ROOT = "/tmp/whatever"
    worker = Agent(name="W", role_type="worker", system_prompt="...")
    mgr = Agent(name="M", role_type="manager", system_prompt="...")

    worker_names = {t["function"]["name"] for t in worker.tools}
    mgr_names = {t["function"]["name"] for t in mgr.tools}

    assert "write_workspace" in worker_names
    assert "write_workspace" not in mgr_names  # managers don't produce
    assert "read_workspace" in worker_names
    assert "read_workspace" in mgr_names
    assert "list_workspace" in worker_names
    assert "list_workspace" in mgr_names

    print("[workspace_tool_kinds] OK")


async def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        await test_worker_writes_workspace(Path(d))
    await test_workspace_tool_kinds()
    print("\nAll workspace tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
