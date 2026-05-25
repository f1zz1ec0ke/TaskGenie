"""Workspace tooling: workers write real artifacts to a shared dir,
managers (and CEO) verify by listing/reading.

Run with: python -m tests.test_workspace
"""
from __future__ import annotations

import asyncio
import json
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


async def test_utf8_roundtrip(tmp_path: Path) -> None:
    """All workspace/scratch reads & writes are explicit UTF-8 so non-ASCII
    content (accents, math symbols, CJK, emoji) survives the round-trip.

    The bug being prevented: Path.write_text / read_text without an explicit
    encoding falls back to locale.getpreferredencoding() which on Windows is
    cp1252 and can't encode anything outside Latin-1 -- so any agent writing
    a file containing e.g. an arrow character would silently fail."""
    object.__setattr__(CONFIG, "data_dir", str(tmp_path))
    CONFIG.sessions_dir.mkdir(parents=True, exist_ok=True)
    os.environ[PROJECTS_ROOT_ENV] = str(tmp_path / "projects-utf8")
    Agent.REGISTRY.clear()
    session = Session.create("utf8 task")
    Agent.SCRATCH_ROOT = str(session.scratch_root)
    Agent.WORKSPACE_ROOT = str(session.project_dir)

    worker = Agent(name="W", role_type="worker", system_prompt="...")
    write = REGISTRY.dispatcher("write_workspace")
    read = REGISTRY.dispatcher("read_workspace")
    edit = REGISTRY.dispatcher("edit_workspace")
    write_scratch = REGISTRY.dispatcher("write_file")
    read_scratch = REGISTRY.dispatcher("read_file")

    # A spread of non-ASCII Unicode that locale=cp1252 would reject:
    # arrows, math symbols, accented Latin, CJK, an emoji.
    payload = (
        "# Résumé\n"
        "Flow: A → B → C (∑ steps = ∞)\n"
        "Approx π ≈ 3.14159, σ² = variance\n"
        "Greeting: 你好 / こんにちは / 안녕\n"
        "Emoji though discouraged: 🎮\n"
        "Currency: €100, ¥500\n"
    )

    # write -> file content matches exactly
    res = await write(worker, {"path": "doc.md", "content": payload})
    assert "created" in res, res
    assert (session.project_dir / "doc.md").read_text(encoding="utf-8") == payload

    # read -> string matches what we wrote
    got = await read(worker, {"path": "doc.md"})
    assert got == payload

    # edit on unicode old_text + unicode new_text
    res = await edit(
        worker,
        {
            "path": "doc.md",
            "old_text": "Flow: A → B → C",
            "new_text": "Flow: α → β → γ",
        },
    )
    assert "edited" in res, res
    got2 = await read(worker, {"path": "doc.md"})
    assert "α → β → γ" in got2
    assert "A → B → C" not in got2

    # append mode preserves utf-8 too
    res = await write(
        worker,
        {"path": "log.txt", "content": "ñoño\n", "mode": "append"},
    )
    assert "appended" in res
    res = await write(
        worker,
        {"path": "log.txt", "content": "Größe\n", "mode": "append"},
    )
    assert "appended" in res
    assert (session.project_dir / "log.txt").read_text(encoding="utf-8") == "ñoño\nGröße\n"

    # Scratch file ops use utf-8 too
    res = await write_scratch(worker, {"path": "notes.txt", "content": payload})
    assert "wrote" in res
    scratch_back = await read_scratch(worker, {"path": "notes.txt"})
    assert scratch_back == payload

    print("[utf8_roundtrip] OK")


async def test_overwrite_after_read_warns(tmp_path: Path) -> None:
    """When an agent does read_workspace(path) then write_workspace(path,
    mode='overwrite'), the dispatcher appends a lost-update warning to the
    tool response so the model sees it before its next decision."""
    object.__setattr__(CONFIG, "data_dir", str(tmp_path))
    CONFIG.sessions_dir.mkdir(parents=True, exist_ok=True)
    os.environ[PROJECTS_ROOT_ENV] = str(tmp_path / "projects-warn")
    Agent.REGISTRY.clear()
    session = Session.create("warn task")
    Agent.SCRATCH_ROOT = str(session.scratch_root)
    Agent.WORKSPACE_ROOT = str(session.project_dir)

    worker = Agent(name="W", role_type="worker", system_prompt="...")
    write = REGISTRY.dispatcher("write_workspace")
    assert write is not None

    # Set up an existing file the worker will later read + overwrite.
    (session.project_dir / "config.py").write_text("VERSION = 1\n", encoding="utf-8")

    # Simulate the read_workspace call sitting in the worker's message history.
    worker.messages.append(
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "tc-1",
                    "type": "function",
                    "function": {
                        "name": "read_workspace",
                        "arguments": json.dumps({"path": "config.py"}),
                    },
                }
            ],
        }
    )

    # Overwrite after read -> warning in response
    res = await write(
        worker,
        {
            "path": "config.py",
            "content": "VERSION = 2\n",
            "mode": "overwrite",
        },
    )
    assert "wrote (overwrote)" in res
    assert "WARNING" in res
    assert "edit_workspace" in res

    # Overwrite WITHOUT a prior read of that exact path -> no warning
    res = await write(
        worker,
        {
            "path": "newfile.txt",
            "content": "hello",
            "mode": "overwrite",
        },
    )
    assert "WARNING" not in res

    print("[overwrite_after_read_warns] OK")


async def test_tui_sees_ceo(tmp_path: Path) -> None:
    """The CEO is created before tui.run() is scheduled, so its spawn
    event fires before any TUI subscriber exists. Without the startup
    snapshot, the CEO is invisible in the hierarchy panel and the first
    manager becomes the de-facto root.
    """
    object.__setattr__(CONFIG, "data_dir", str(tmp_path))
    CONFIG.sessions_dir.mkdir(parents=True, exist_ok=True)
    os.environ[PROJECTS_ROOT_ENV] = str(tmp_path / "projects-tui")
    Agent.REGISTRY.clear()

    # Spin up a CEO + manager + worker, then construct the TUI
    # AFTERWARDS to mimic the production order in __main__.py.
    session = Session.create("tui ceo task")
    Agent.SCRATCH_ROOT = str(session.scratch_root)
    Agent.WORKSPACE_ROOT = str(session.project_dir)
    ceo = Agent(name="CEO", role_type="ceo", system_prompt="...")
    mgr = Agent(
        name="VisualsPolishManager",
        role_type="manager",
        system_prompt="...",
        parent=ceo,
        depth=1,
    )
    ceo.children.append(mgr)
    worker = Agent(
        name="UIOverlayWorker",
        role_type="worker",
        system_prompt="...",
        parent=mgr,
        depth=2,
    )
    mgr.children.append(worker)

    from taskgenie.tui import TUI

    tui = TUI(task="dummy")
    # All spawn events fired BEFORE tui exists; they're lost to BUS.
    assert tui.root_id is None
    assert tui.agents == {}

    # The snapshot call is what tui.run() runs first; verify it
    # populates self.agents from REGISTRY and sets root to the CEO.
    tui._snapshot_initial_agents()
    assert tui.root_id == ceo.id
    assert ceo.id in tui.agents
    assert mgr.id in tui.agents
    assert worker.id in tui.agents
    assert tui.agents[ceo.id].children_ids == [mgr.id]
    assert tui.agents[mgr.id].children_ids == [worker.id]
    # Idempotent on re-call -- doesn't double-link
    tui._snapshot_initial_agents()
    assert tui.agents[ceo.id].children_ids == [mgr.id]

    print("[tui_sees_ceo] OK")


def test_safe_jsonable() -> None:
    """The artifact json writer must survive bundles that contain tuple
    keys, datetimes, sets, and other JSON-hostile objects -- final.json
    is a logging artifact, never worth crashing the run over."""
    from taskgenie.artifacts import _safe_jsonable

    # Plain JSON-friendly content survives unchanged
    plain = {"a": [1, 2, "three"], "b": {"c": None, "d": True}}
    assert _safe_jsonable(plain) == plain

    # Tuple key (the bug from the real run) gets stringified
    tricky = {("foo", "bar"): 5}
    out = _safe_jsonable(tricky)
    assert list(out.keys()) == ["('foo', 'bar')"]
    assert out["('foo', 'bar')"] == 5

    # Tuple values become lists; sets become lists too
    out = _safe_jsonable({"t": (1, 2, 3), "s": {7, 8}})
    assert out["t"] == [1, 2, 3]
    assert sorted(out["s"]) == [7, 8]

    # Nested counter-style dict with mixed key types
    nested = {"agents": {"id-1": {("a", "b"): 3, "plain": 4}}}
    out = _safe_jsonable(nested)
    assert "('a', 'b')" in out["agents"]["id-1"]
    assert out["agents"]["id-1"]["plain"] == 4

    # Round-trip through json.dumps without exception
    big = {
        "stats": {"agents": {"x": {("tuple", "key"): 1, "normal": 2}}},
        "list": [{"a": (1, 2)}],
        "set": {1, 2, 3},
        "raw": object(),  # exotic
    }
    s = json.dumps(_safe_jsonable(big), indent=2)
    assert "tuple" in s and "normal" in s and "object" in s

    print("[safe_jsonable] OK")


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
    with tempfile.TemporaryDirectory() as d:
        await test_utf8_roundtrip(Path(d))
    with tempfile.TemporaryDirectory() as d:
        await test_overwrite_after_read_warns(Path(d))
    with tempfile.TemporaryDirectory() as d:
        await test_tui_sees_ceo(Path(d))
    test_safe_jsonable()
    print("\nAll workspace tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
