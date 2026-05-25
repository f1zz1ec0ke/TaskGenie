"""Workspace tooling: workers write real artifacts to a shared dir,
managers (and CEO) verify by listing/reading.

Run with: python -m tests.test_workspace
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from taskgenie.agent import Agent
from taskgenie.config import CONFIG
from taskgenie.prompts import worker_prompt, manager_prompt
from taskgenie.session import Session, PROJECTS_ROOT_ENV
from taskgenie.tools.registry import REGISTRY


async def test_worker_writes_workspace(tmp_path: Path) -> None:
    object.__setattr__(CONFIG, "data_dir", str(tmp_path))
    CONFIG.sessions_dir.mkdir(parents=True, exist_ok=True)
    os.environ[PROJECTS_ROOT_ENV] = str(tmp_path / "projects")
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


async def test_write_modes(tmp_path: Path) -> None:
    """write_workspace mode flag: create / overwrite / append all behave correctly."""
    object.__setattr__(CONFIG, "data_dir", str(tmp_path))
    CONFIG.sessions_dir.mkdir(parents=True, exist_ok=True)
    os.environ[PROJECTS_ROOT_ENV] = str(tmp_path / "projects")
    Agent.REGISTRY.clear()
    session = Session.create("modes task")
    Agent.SCRATCH_ROOT = str(session.scratch_root)
    Agent.WORKSPACE_ROOT = str(session.project_dir)

    worker = Agent(name="W", role_type="worker", system_prompt="...")
    write = REGISTRY.dispatcher("write_workspace")
    assert write is not None

    # mode='create' (default): writes a new file
    res = await write(worker, {"path": "game.py", "content": "print(1)\n"})
    assert "created" in res

    # mode='create' on existing file FAILS loudly
    res = await write(worker, {"path": "game.py", "content": "print(99)\n"})
    assert "already exists" in res
    # And the original content is preserved
    assert (session.project_dir / "game.py").read_text() == "print(1)\n"

    # mode='overwrite' explicitly replaces
    res = await write(
        worker, {"path": "game.py", "content": "print(2)\n", "mode": "overwrite"}
    )
    assert "overwrote" in res
    assert (session.project_dir / "game.py").read_text() == "print(2)\n"

    # mode='append' adds to existing
    res = await write(
        worker, {"path": "log.txt", "content": "line1\n", "mode": "append"}
    )
    res2 = await write(
        worker, {"path": "log.txt", "content": "line2\n", "mode": "append"}
    )
    assert "appended" in res and "appended" in res2
    assert (session.project_dir / "log.txt").read_text() == "line1\nline2\n"

    # Unknown mode error
    res = await write(
        worker, {"path": "x.txt", "content": "x", "mode": "smash"}
    )
    assert "unknown mode" in res.lower()

    print("[write_modes] OK")


async def test_edit_workspace(tmp_path: Path) -> None:
    """edit_workspace: exactly-once matching, conflict detection, file-not-found."""
    object.__setattr__(CONFIG, "data_dir", str(tmp_path))
    CONFIG.sessions_dir.mkdir(parents=True, exist_ok=True)
    os.environ[PROJECTS_ROOT_ENV] = str(tmp_path / "projects-edit")
    Agent.REGISTRY.clear()
    session = Session.create("edit task")
    Agent.SCRATCH_ROOT = str(session.scratch_root)
    Agent.WORKSPACE_ROOT = str(session.project_dir)

    worker = Agent(name="W", role_type="worker", system_prompt="...")
    write = REGISTRY.dispatcher("write_workspace")
    edit = REGISTRY.dispatcher("edit_workspace")
    assert edit is not None

    # Edit on missing file fails with a helpful message
    res = await edit(
        worker, {"path": "missing.py", "old_text": "x", "new_text": "y"}
    )
    assert "does not exist" in res

    # Set up a file
    await write(
        worker,
        {
            "path": "game.py",
            "content": "def fire():\n    return 'pew'\n\ndef move():\n    return 'wobble'\n",
        },
    )

    # Successful edit: old_text appears exactly once
    res = await edit(
        worker,
        {
            "path": "game.py",
            "old_text": "return 'pew'",
            "new_text": "return 'BANG'",
        },
    )
    assert "edited" in res
    contents = (session.project_dir / "game.py").read_text()
    assert "'BANG'" in contents
    assert "'pew'" not in contents
    # Other function untouched
    assert "return 'wobble'" in contents

    # old_text not found: conflict-style error
    res = await edit(
        worker,
        {"path": "game.py", "old_text": "this is not in the file", "new_text": "x"},
    )
    assert "not found" in res

    # old_text ambiguous (appears more than once)
    await write(
        worker,
        {
            "path": "dup.py",
            "content": "return 1\nreturn 1\n",
            "mode": "overwrite",
        },
    )
    res = await edit(
        worker, {"path": "dup.py", "old_text": "return 1", "new_text": "return 2"}
    )
    assert "ambiguous" in res or "appears 2 times" in res

    print("[edit_workspace] OK")


def test_project_dir_layout(tmp_path: Path) -> None:
    """Sessions land workspaces in ./TaskGenie/<project>/, not in the session dir."""
    object.__setattr__(CONFIG, "data_dir", str(tmp_path / "data"))
    os.environ[PROJECTS_ROOT_ENV] = str(tmp_path / "TaskGenie")
    Agent.REGISTRY.clear()

    # Default project name derived from the task slug
    s1 = Session.create("Build a physics game")
    assert s1.project_dir.parent == (tmp_path / "TaskGenie").resolve()
    assert s1.project_dir.name == "build-a-physics-game"
    assert s1.project_dir.exists()
    # Session metadata lives separately, under data_dir
    assert s1.root_dir.exists()
    assert str(s1.root_dir) != str(s1.project_dir)

    # Collision suffix when the dir already exists
    s2 = Session.create("Build a physics game")
    assert s2.project_dir.name == "build-a-physics-game-2"
    assert s2.project_dir.exists()
    assert s2.project_dir != s1.project_dir

    # Explicit --project override
    s3 = Session.create("Whatever the task is", project="MyGame")
    assert s3.project_dir.name == "MyGame"

    # project_dir survives reload (round-trip through state.json)
    s1.save()
    reloaded = Session.load(s1.session_id)
    assert reloaded.project_dir == s1.project_dir
    # And workspace_dir is now an alias for project_dir
    assert reloaded.workspace_dir == reloaded.project_dir

    print("[project_dir_layout] OK")


async def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        await test_worker_writes_workspace(Path(d))
    await test_workspace_tool_kinds()
    with tempfile.TemporaryDirectory() as d:
        await test_write_modes(Path(d))
    with tempfile.TemporaryDirectory() as d:
        await test_edit_workspace(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_project_dir_layout(Path(d))
    print("\nAll workspace tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
