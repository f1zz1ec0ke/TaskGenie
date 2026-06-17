"""Tests for the round-three review fixes (R36-R90).

Round 3 surfaced four critical bugs (3 security + 1 broken-R18-fix) plus
a long tail of medium/low. This file covers the critical + high items
explicitly; medium/low fixes ride on coverage of the helpers they touched.

Run with: python -m tests.test_review_rt3
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from taskgenie import agent as agent_module
from taskgenie import archivist as archivist_module
from taskgenie.agent import Agent, _is_context_length_error
from taskgenie.archivist import (
    CompactionSkipped,
    CompactionTransientFailure,
    compact_messages,
)
from taskgenie.config import CONFIG
from taskgenie.event_logger import _truncate_for_log
from taskgenie.events import BUS, Event
from taskgenie.prompts import CEO_ROLE_PROMPT
from taskgenie.session import Session
from taskgenie.stats import compute as stats_compute
from taskgenie.tools.registry import REGISTRY
from taskgenie.tools.work import (
    _list_workspace,
    _normalize_workspace_path,
    _read_workspace,
    _safe_path,
    _UnsafeOpenError,
    _write_text_safe,
    _write_workspace,
)
from taskgenie.tui import _trim_for_activity


def _has_symlink_support() -> bool:
    if not hasattr(os, "symlink"):
        return False
    with tempfile.TemporaryDirectory() as d:
        try:
            os.symlink(d, os.path.join(d, "link"))
            return True
        except (OSError, NotImplementedError):
            return False


def _has_hardlink_support() -> bool:
    if not hasattr(os, "link"):
        return False
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "src")
        with open(src, "w") as f:
            f.write("x")
        try:
            os.link(src, os.path.join(d, "dst"))
            return True
        except (OSError, NotImplementedError):
            return False


def _new_ceo(scratch_root: str | None = None) -> Agent:
    Agent.REGISTRY.clear()
    if scratch_root is None:
        scratch_root = tempfile.mkdtemp(prefix="tg-test-rt3-")
    Agent.SCRATCH_ROOT = scratch_root
    Agent.WORKSPACE_ROOT = scratch_root
    return Agent(name="CEO", role_type="ceo", system_prompt=CEO_ROLE_PROMPT)


# ---------------------------------------------------------------------------
# R36 + R38: Hardlink protection on writes AND reads
# ---------------------------------------------------------------------------


async def test_hardlink_write_through_refused(tmp_path: Path) -> None:
    if not _has_hardlink_support():
        print("[hardlink_write_through_refused] SKIP (no hardlink support)")
        return

    base = tmp_path / "ws"
    base.mkdir()
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("ORIGINAL", encoding="utf-8")

    # Plant a HARDLINK (not symlink) -- shares inode, fresh dirent.
    target = base / "harmless.md"
    os.link(str(sentinel), str(target))

    class _FakeAgent:
        workspace_dir = str(base)
        scratch_dir = str(base)
        messages: list = []
        read_workspace_paths: set = set()

    # mode='overwrite' is the dangerous path (mode='create' would EEXIST).
    result = await _write_workspace(
        _FakeAgent(),
        {"path": "harmless.md", "content": "PWNED", "mode": "overwrite"},
    )
    assert "ERROR" in result and "hardlink" in result.lower(), result
    assert sentinel.read_text(encoding="utf-8") == "ORIGINAL", (
        f"sentinel was overwritten through hardlink! got "
        f"{sentinel.read_text(encoding='utf-8')!r}"
    )
    print("[hardlink_write_through_refused] OK")


async def test_hardlink_read_disclosure_refused(tmp_path: Path) -> None:
    if not _has_hardlink_support():
        print("[hardlink_read_disclosure_refused] SKIP (no hardlink support)")
        return

    base = tmp_path / "ws"
    base.mkdir()
    secret = tmp_path / "secret"
    secret.write_text("DO-NOT-READ", encoding="utf-8")

    target = base / "info.md"
    os.link(str(secret), str(target))

    class _FakeAgent:
        workspace_dir = str(base)
        scratch_dir = str(base)
        messages: list = []
        read_workspace_paths: set = set()

    result = await _read_workspace(_FakeAgent(), {"path": "info.md"})
    assert "ERROR" in result and "hardlink" in result.lower(), result
    assert "DO-NOT-READ" not in result, (
        f"hardlink target leaked into agent-visible read: {result!r}"
    )
    print("[hardlink_read_disclosure_refused] OK")


# ---------------------------------------------------------------------------
# R37: Parent-component TOCTOU via planted parent symlink
# ---------------------------------------------------------------------------


def test_parent_component_symlink_refused_by_realpath_check(tmp_path: Path) -> None:
    """Simulates the parent-symlink TOCTOU: validate `subdir/file.txt`,
    then plant `subdir -> outside`. The post-open realpath check catches
    the resulting fd being outside the base."""
    if not _has_symlink_support():
        print("[parent_component_symlink_refused] SKIP (no symlink support)")
        return

    base = tmp_path / "ws"
    base.mkdir()
    outside = tmp_path / "outside-dir"
    outside.mkdir()

    # Skip if /proc/self/fd is unavailable (non-Linux); the protection
    # there is degraded.
    if not os.path.exists("/proc/self/fd"):
        print("[parent_component_symlink_refused] SKIP (no /proc/self/fd)")
        return

    # Plant subdir as symlink to outside-dir.
    os.symlink(str(outside), str(base / "subdir"))

    # Call `_write_text_safe` directly with a leaf inside the planted-
    # symlink parent. _safe_path would have CAUGHT this in `.resolve()`
    # because the parent symlink exists at validation time; so to truly
    # simulate post-validation TOCTOU we bypass _safe_path and just go
    # straight to the open layer.
    leaf = base / "subdir" / "file.txt"
    try:
        _write_text_safe(leaf, base, "PWN", "create")
    except (_UnsafeOpenError, OSError):
        # Either ELOOP, EMLINK, or our custom error -- all acceptable
        # "I refused to write" outcomes.
        pass
    else:
        raise AssertionError("expected _write_text_safe to refuse parent-symlink path")

    # The sentinel directory should NOT contain a file.txt that we wrote.
    assert not (outside / "file.txt").exists(), (
        "wrote into outside dir via parent symlink!"
    )
    print("[parent_component_symlink_refused_by_realpath_check] OK")


# ---------------------------------------------------------------------------
# R39: Session.save tempfile leak on serialization error
# ---------------------------------------------------------------------------


def test_session_save_cleans_tempfile_on_json_dump_failure(tmp_path: Path) -> None:
    """R18 was supposed to clean up the tempfile on json.dump failure, but
    the previous impl captured tmp_path AFTER the write succeeded, so the
    cleanup branch was skipped on the failure it was meant to handle.
    Verify the round-three fix: tmp_path captured FIRST, finally runs."""
    object.__setattr__(CONFIG, "data_dir", str(tmp_path / "data"))
    try:
        session = Session.create("save fail test")

        # An object json can't natively serialise, where even
        # default=str fails (str raises). asdict happily passes the
        # whole thing through; json.dump then calls our `default=str`
        # on it, which raises, mid-write.
        class _UnserializableLooksLikeNothing:
            def __str__(self) -> str:
                raise RuntimeError("simulated str() failure mid-dump")

        session.data.history.append(
            {"kind": "task", "payload": _UnserializableLooksLikeNothing()}
        )

        try:
            session.save()
        except (RuntimeError, TypeError, ValueError):
            pass
        else:
            raise AssertionError("expected json.dump to raise on unserialisable")

        leftover = list(session.root_dir.glob(".state-*.json.tmp"))
        assert leftover == [], (
            f"R39 broken: temp files left in {session.root_dir}: {leftover}"
        )
        print("[session_save_cleans_tempfile_on_json_dump_failure] OK")
    finally:
        object.__setattr__(CONFIG, "data_dir", ".taskgenie")


# ---------------------------------------------------------------------------
# R40: _compaction_floor persisted across rehydrate
# ---------------------------------------------------------------------------


def test_compaction_floor_persists_through_rehydrate() -> None:
    """A compaction floor set just before save should survive a save/load
    round-trip so the repeat-compact guard isn't reset on resume."""
    Agent.REGISTRY.clear()
    Agent.SCRATCH_ROOT = tempfile.mkdtemp(prefix="tg-test-rt3-")
    Agent.WORKSPACE_ROOT = Agent.SCRATCH_ROOT
    agent = Agent(name="CEO", role_type="ceo", system_prompt="...")
    agent.messages.extend([{"role": "user", "content": "x"}] * 20)
    agent._compaction_floor = 17  # arbitrary marker
    d = agent.to_dict()
    assert d["compaction_floor"] == 17, d.get("compaction_floor")

    # Rehydrate via the classmethod.
    Agent.REGISTRY.clear()
    Agent.bulk_load({d["id"]: d})
    revived = Agent.REGISTRY[d["id"]]
    assert revived._compaction_floor == 17, revived._compaction_floor
    print("[compaction_floor_persists_through_rehydrate] OK")


def test_compaction_floor_old_snapshot_defaults_to_len_messages() -> None:
    """Pre-round-three snapshots have no compaction_floor field. Use
    len(messages) as default so a long-history resume doesn't immediately
    re-compact on the next turn."""
    Agent.REGISTRY.clear()
    Agent.SCRATCH_ROOT = tempfile.mkdtemp(prefix="tg-test-rt3-")
    Agent.WORKSPACE_ROOT = Agent.SCRATCH_ROOT
    # Hand-construct a dict with no compaction_floor (old format).
    d = {
        "id": "ceo-1",
        "name": "CEO",
        "role_type": "ceo",
        "system_prompt": "...",
        "parent_id": None,
        "child_ids": [],
        "depth": 0,
        "status": "idle",
        "messages": [{"role": "system", "content": "..."}]
        + [{"role": "user", "content": "x"} for _ in range(50)],
        "scratch_dir": Agent.SCRATCH_ROOT,
        "blocker_reason": "",
        "blocker_needs": "",
        "follow_ups": [],
    }
    Agent.bulk_load({"ceo-1": d})
    revived = Agent.REGISTRY["ceo-1"]
    # No floor in dict; default to len(messages).
    assert revived._compaction_floor == 51, revived._compaction_floor
    print("[compaction_floor_old_snapshot_defaults_to_len_messages] OK")


# ---------------------------------------------------------------------------
# R41: event_logger truncation recurses into dicts/lists
# ---------------------------------------------------------------------------


def test_truncate_for_log_recurses_into_nested_dicts() -> None:
    big = "x" * (32 * 1024)
    row = _truncate_for_log(
        {
            "tool": "write_workspace",
            "args": {"path": "game.py", "content": big},  # nested
        }
    )
    assert isinstance(row["args"], dict)
    assert len(row["args"]["content"]) < len(big), (
        "truncator didn't recurse into the args dict"
    )
    assert "truncated" in row["args"]["content"]
    # Untouched fields should pass through.
    assert row["args"]["path"] == "game.py"
    assert row["tool"] == "write_workspace"

    # Also recurse into lists.
    row = _truncate_for_log({"items": ["short", big, {"nested": big}]})
    assert len(row["items"][1]) < len(big)
    assert len(row["items"][2]["nested"]) < len(big)
    print("[truncate_for_log_recurses_into_nested_dicts] OK")


# ---------------------------------------------------------------------------
# R42: TUI tool_args truncation recurses
# ---------------------------------------------------------------------------


def test_tui_trim_for_activity_recurses() -> None:
    big = "x" * 500
    trimmed = _trim_for_activity({"a": big, "b": {"c": big}, "d": [big]})
    assert len(trimmed["a"]) < len(big)
    assert len(trimmed["b"]["c"]) < len(big)
    assert len(trimmed["d"][0]) < len(big)
    # Strings short of the threshold pass through.
    trimmed = _trim_for_activity({"short": "small string"})
    assert trimmed["short"] == "small string"
    print("[tui_trim_for_activity_recurses] OK")


# ---------------------------------------------------------------------------
# R43 + R44 + R47: archivist exception classes + caller floor policy
# ---------------------------------------------------------------------------


class _UnreachableLLM:
    """Mock LLM that fails the test if called. Used in tests where the
    expected path skips the LLM entirely."""

    async def model_name(self) -> str:
        return "unreachable"

    async def chat(self, messages, tools=None, **_kwargs):
        raise AssertionError(
            "archivist LLM should NOT be called when the history can't "
            "be safely compacted; expected an early-return path"
        )


async def test_archivist_raises_skipped_when_safe_recent_eats_middle() -> None:
    """Degenerate alternation: snap pushes recent so far forward that
    `len(messages) <= adjusted_keep + 2` -- raise Skipped without ever
    invoking the LLM."""
    msgs = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        # 2 assistant(tc)/tool pairs -- after snap keep_recent grows to 4
        # which is >= len(msgs) - 2.
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "t0",
                    "type": "function",
                    "function": {"name": "x", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "t0", "content": "ok"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "t1",
                    "type": "function",
                    "function": {"name": "x", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "t1", "content": "ok"},
    ]
    original = archivist_module.LLM
    archivist_module.LLM = _UnreachableLLM()
    try:
        try:
            await compact_messages("W", msgs, keep_recent=3)
        except CompactionSkipped:
            pass
        else:
            raise AssertionError(
                "expected CompactionSkipped for degenerate alternation"
            )
    finally:
        archivist_module.LLM = original
    print("[archivist_raises_skipped_when_safe_recent_eats_middle] OK")


async def test_archivist_raises_skipped_when_original_task_has_tool_calls() -> None:
    msgs = [
        {"role": "system", "content": "s"},
        # First non-system is assistant WITH tool_calls -- compaction
        # would orphan the tool_call_ids.
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "t0",
                    "type": "function",
                    "function": {"name": "x", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "t0", "content": "r"},
        *[{"role": "assistant", "content": f"step {i}"} for i in range(10)],
    ]
    original = archivist_module.LLM
    archivist_module.LLM = _UnreachableLLM()
    try:
        try:
            await compact_messages("W", msgs, keep_recent=2)
        except CompactionSkipped as e:
            assert "tool_call" in str(e).lower(), e
        else:
            raise AssertionError(
                "expected CompactionSkipped for assistant-with-tool_calls task"
            )
    finally:
        archivist_module.LLM = original
    print("[archivist_raises_skipped_when_original_task_has_tool_calls] OK")


async def test_maybe_compact_distinguishes_skip_vs_transient() -> None:
    """Skip advances the floor (don't keep retrying); transient does
    not (retry next threshold)."""
    Agent.REGISTRY.clear()
    Agent.SCRATCH_ROOT = tempfile.mkdtemp(prefix="tg-test-rt3-")
    Agent.WORKSPACE_ROOT = Agent.SCRATCH_ROOT
    agent = Agent(name="CEO", role_type="ceo", system_prompt="...")
    # Pad messages.
    agent.messages.extend([{"role": "user", "content": "x"}] * 20)

    # Force-compact with a stub that always SKIPS.
    async def _skip(name, messages, keep_recent=None):
        raise CompactionSkipped("test skip")

    original = agent_module.compact_messages
    agent_module.compact_messages = _skip
    try:
        result = await agent._maybe_compact(force=True)
        assert result is False
        assert agent._compaction_floor == len(agent.messages), (
            "skip should advance floor"
        )
    finally:
        agent_module.compact_messages = original

    # Reset and try with a stub that raises TransientFailure.
    agent._compaction_floor = 0
    pre_floor = agent._compaction_floor

    async def _transient(name, messages, keep_recent=None):
        raise CompactionTransientFailure("simulated blip")

    agent_module.compact_messages = _transient
    try:
        result = await agent._maybe_compact(force=True)
        assert result is False
        assert agent._compaction_floor == pre_floor, (
            "transient should NOT advance floor"
        )
    finally:
        agent_module.compact_messages = original
    print("[maybe_compact_distinguishes_skip_vs_transient] OK")


async def test_maybe_compact_always_emits_end_event_even_on_raise() -> None:
    """If compact_messages raises an unexpected exception (not Skipped /
    not Transient), the try/finally must still emit compaction_end."""
    Agent.REGISTRY.clear()
    Agent.SCRATCH_ROOT = tempfile.mkdtemp(prefix="tg-test-rt3-")
    Agent.WORKSPACE_ROOT = Agent.SCRATCH_ROOT
    agent = Agent(name="CEO", role_type="ceo", system_prompt="...")
    agent.messages.extend([{"role": "user", "content": "x"}] * 20)

    async def _explode(name, messages, keep_recent=None):
        raise RuntimeError("unexpected boom")

    probe = BUS.subscribe()
    original = agent_module.compact_messages
    agent_module.compact_messages = _explode
    try:
        try:
            await agent._maybe_compact(force=True)
        except RuntimeError:
            pass  # expected to propagate
        events: list[Event] = []
        while not probe.empty():
            events.append(probe.get_nowait())
    finally:
        agent_module.compact_messages = original
        BUS.unsubscribe(probe)

    starts = [e for e in events if e.kind == "compaction_start"]
    ends = [e for e in events if e.kind == "compaction_end"]
    assert len(starts) == 1, f"expected one start, got {len(starts)}"
    assert len(ends) == 1, f"expected one end (even on raise), got {len(ends)}"
    print("[maybe_compact_always_emits_end_event_even_on_raise] OK")


# ---------------------------------------------------------------------------
# R48: bulk_load tags replays; stats.compute skips them
# ---------------------------------------------------------------------------


def test_stats_skips_replay_events(tmp_path: Path) -> None:
    """A blocked event with replay=True must NOT bump blockers_total."""
    log = tmp_path / "events.jsonl"
    rows = [
        {
            "kind": "spawn",
            "agent_id": "a1",
            "agent_name": "CEO",
            "ts": time.time(),
            "data": {"role_type": "ceo"},
        },
        # Original blocker -- counts.
        {
            "kind": "blocked",
            "agent_id": "a1",
            "agent_name": "CEO",
            "ts": time.time(),
            "data": {"reason": "stuck", "needs": "help"},
        },
        # Replay from bulk_load -- must NOT count.
        {
            "kind": "blocked",
            "agent_id": "a1",
            "agent_name": "CEO",
            "ts": time.time(),
            "data": {"reason": "stuck", "needs": "help", "replay": True},
        },
    ]
    with log.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    s = stats_compute(log, ceo_id="a1")
    assert s.blockers_total == 1, (
        f"expected 1 blocker (replay should be ignored), got {s.blockers_total}"
    )
    assert s.blockers_escalated_to_user == 1
    print("[stats_skips_replay_events] OK")


# ---------------------------------------------------------------------------
# R52: _agent_read_workspace_paths drops the message-scan union
# ---------------------------------------------------------------------------


async def test_failed_read_does_not_trigger_lost_update_warning(tmp_path: Path) -> None:
    """Pre-fix: scanning agent.messages would pick up a read_workspace
    tool_call even if the read errored (file not found). That triggered
    a spurious lost-update warning on a fresh file."""
    ws = tmp_path / "ws"
    ws.mkdir()
    # File does NOT exist when we attempt to read.

    class _FakeAgent:
        workspace_dir = str(ws)
        scratch_dir = str(ws)
        # Note: persistent set stays empty because the read failed.
        read_workspace_paths: set = set()
        # But the messages contain a read_workspace tool_call that
        # PRE-fix would have triggered a false warning via the union.
        messages: list = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "tc-1",
                        "type": "function",
                        "function": {
                            "name": "read_workspace",
                            "arguments": json.dumps({"path": "fresh.md"}),
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "tc-1",
                "content": "ERROR: file does not exist in workspace: fresh.md",
            },
        ]

    result = await _write_workspace(
        _FakeAgent(),
        {"path": "fresh.md", "content": "hello", "mode": "overwrite"},
    )
    assert "WARNING" not in result, (
        f"failed read should not trigger lost-update warning: {result!r}"
    )
    print("[failed_read_does_not_trigger_lost_update_warning] OK")


# ---------------------------------------------------------------------------
# R53 + R54: list_workspace filters tg-edit-tmp and surfaces symlinks
# ---------------------------------------------------------------------------


async def test_list_workspace_filters_tempfiles_and_surfaces_symlinks(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "real.py").write_text("x", encoding="utf-8")
    (ws / ".real.py.abc.tg-edit-tmp").write_text("y", encoding="utf-8")

    if _has_symlink_support():
        outside = tmp_path / "outside.txt"
        outside.write_text("z", encoding="utf-8")
        os.symlink(str(outside), str(ws / "link.md"))

    class _FakeAgent:
        workspace_dir = str(ws)
        scratch_dir = str(ws)

    result = await _list_workspace(_FakeAgent(), {})
    assert "real.py" in result
    assert ".tg-edit-tmp" not in result, (
        f"tempfile leaked into list_workspace output: {result!r}"
    )
    if _has_symlink_support():
        assert "link.md" in result
        assert "symlinks" in result.lower() or "→" in result, (
            f"symlink not surfaced distinctly: {result!r}"
        )
    print("[list_workspace_filters_tempfiles_and_surfaces_symlinks] OK")


# ---------------------------------------------------------------------------
# R55: _is_context_length_error handles Anthropic body shape
# ---------------------------------------------------------------------------


def test_is_context_length_error_handles_anthropic_body() -> None:
    class _Anthropic(Exception):
        body = {
            "error": {
                "type": "invalid_request_error",
                "message": "prompt is too long: 200000 tokens, max is 200000",
            }
        }

    assert _is_context_length_error(_Anthropic("boom"))

    # A different invalid_request_error (e.g. bad model) must NOT match.
    class _NotCtx(Exception):
        body = {
            "error": {
                "type": "invalid_request_error",
                "message": "model 'foo' not found",
            }
        }

    assert not _is_context_length_error(_NotCtx("boom"))
    print("[is_context_length_error_handles_anthropic_body] OK")


# ---------------------------------------------------------------------------
# R56: _seal_tool_batch bounds check
# ---------------------------------------------------------------------------


def test_seal_tool_batch_rejects_out_of_range_index() -> None:
    agent = _new_ceo()
    try:
        agent._seal_tool_batch(
            [{"id": "x", "name": "y"}], finishing_index=5, finishing_content="c"
        )
    except ValueError as e:
        assert "out of range" in str(e), e
    else:
        raise AssertionError("expected ValueError for invalid index")
    print("[seal_tool_batch_rejects_out_of_range_index] OK")


# ---------------------------------------------------------------------------
# R58: bulk_load sorts by depth so parents arrive before children
# ---------------------------------------------------------------------------


def test_bulk_load_emits_spawns_depth_ordered(tmp_path: Path) -> None:
    """A snapshot dict with a child saved BEFORE its parent (by insertion
    order) must re-emit spawns parent-first so the TUI's root_id ends up
    on the depth-0 CEO, not the depth-1 child."""
    Agent.REGISTRY.clear()
    Agent.SCRATCH_ROOT = str(tmp_path)
    Agent.WORKSPACE_ROOT = str(tmp_path)

    agents = {
        "child-id": {
            "id": "child-id",
            "name": "Child",
            "role_type": "worker",
            "system_prompt": "...",
            "parent_id": "ceo-id",
            "child_ids": [],
            "depth": 1,
            "status": "idle",
            "messages": [{"role": "system", "content": "..."}],
            "scratch_dir": str(tmp_path / "child"),
            "blocker_reason": "",
            "blocker_needs": "",
            "follow_ups": [],
        },
        "ceo-id": {
            "id": "ceo-id",
            "name": "CEO",
            "role_type": "ceo",
            "system_prompt": "...",
            "parent_id": None,
            "child_ids": ["child-id"],
            "depth": 0,
            "status": "idle",
            "messages": [{"role": "system", "content": "..."}],
            "scratch_dir": str(tmp_path / "ceo"),
            "blocker_reason": "",
            "blocker_needs": "",
            "follow_ups": [],
        },
    }

    probe = BUS.subscribe()
    try:
        Agent.bulk_load(agents)
        events: list[Event] = []
        while not probe.empty():
            events.append(probe.get_nowait())
    finally:
        BUS.unsubscribe(probe)

    spawn_order = [e.agent_id for e in events if e.kind == "spawn"]
    assert spawn_order.index("ceo-id") < spawn_order.index("child-id"), (
        f"depth-0 CEO should spawn before depth-1 child; got {spawn_order}"
    )
    print("[bulk_load_emits_spawns_depth_ordered] OK")


# ---------------------------------------------------------------------------
# R59: path normalisation makes ./foo == foo for the lost-update set
# ---------------------------------------------------------------------------


def test_normalize_workspace_path_collapses_dot_segments() -> None:
    assert _normalize_workspace_path("./game.py") == "game.py"
    assert _normalize_workspace_path("dir/../game.py") == "game.py"
    assert _normalize_workspace_path("game.py") == "game.py"
    print("[normalize_workspace_path_collapses_dot_segments] OK")


async def test_lost_update_warning_survives_path_form_drift(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "game.py").write_text("v1", encoding="utf-8")

    class _FakeAgent:
        workspace_dir = str(ws)
        scratch_dir = str(ws)
        messages: list = []
        # Read under one form, write under another -- must still warn.
        read_workspace_paths: set = {"game.py"}

    result = await _write_workspace(
        _FakeAgent(),
        {"path": "./game.py", "content": "v2", "mode": "overwrite"},
    )
    assert "WARNING" in result, f"normalisation broken; warning didn't fire: {result!r}"
    print("[lost_update_warning_survives_path_form_drift] OK")


# ---------------------------------------------------------------------------
# R60: diff_tools_against ignores missing buckets (pre-ceo-bucket sessions)
# ---------------------------------------------------------------------------


def test_diff_tools_against_skips_missing_bucket(tmp_path: Path) -> None:
    object.__setattr__(CONFIG, "data_dir", str(tmp_path / "data"))
    try:
        session = Session.create("pre-r2-sim")
        session.save()
        # Simulate a pre-round-two snapshot by removing the ceo bucket.
        session.data.tool_signature.pop("ceo", None)

        diff = session.diff_tools_against(
            REGISTRY.signature("management"),
            REGISTRY.signature("work"),
            REGISTRY.signature("ceo"),
        )
        # The mgmt + work buckets WERE saved (they always have been) and
        # match current, so no diff. The missing ceo bucket should NOT
        # report all current ceo tools as new.
        assert diff is None or "ask_user" not in diff, (
            f"pre-r2 session resume triggered false-positive ceo diff: {diff!r}"
        )
        print("[diff_tools_against_skips_missing_bucket] OK")
    finally:
        object.__setattr__(CONFIG, "data_dir", ".taskgenie")


# ---------------------------------------------------------------------------
# R68: control chars in paths rejected
# ---------------------------------------------------------------------------


def test_safe_path_rejects_newline(tmp_path: Path) -> None:
    base = tmp_path / "scratch"
    base.mkdir()
    try:
        _safe_path(str(base), "foo\nbar")
    except ValueError as e:
        assert "control character" in str(e), e
    else:
        raise AssertionError("expected ValueError for newline in path")
    print("[safe_path_rejects_newline] OK")


# ---------------------------------------------------------------------------
# R75: input helpers re-raise KeyboardInterrupt
# ---------------------------------------------------------------------------


def test_read_unblock_input_lets_keyboard_interrupt_propagate() -> None:
    from rich.console import Console as _Console

    from taskgenie.__main__ import _read_unblock_input

    class _RaisingConsole(_Console):
        def print(self, *args, **kwargs):  # type: ignore[override]
            pass

        def input(self, *args, **kwargs):  # type: ignore[override]
            raise KeyboardInterrupt()

    c = _RaisingConsole()
    try:
        _read_unblock_input(c)
    except KeyboardInterrupt:
        print("[read_unblock_input_lets_keyboard_interrupt_propagate] OK")
        return
    raise AssertionError("expected KeyboardInterrupt to propagate")


# ---------------------------------------------------------------------------
# Tool exposure: exec_workspace is worker-only
# ---------------------------------------------------------------------------


def test_exec_workspace_is_worker_only() -> None:
    """If someone changes exec_workspace's `kind` from "work" to "both",
    the CEO/managers would gain exec ability silently -- catch that
    regression by asserting the current bucketing."""
    work_sig = REGISTRY.signature("work")
    mgmt_sig = REGISTRY.signature("management")
    ceo_sig = REGISTRY.signature("ceo")
    assert "exec_workspace" in work_sig
    assert "exec_workspace" not in mgmt_sig
    assert "exec_workspace" not in ceo_sig
    # Same for exec_code.
    assert "exec_code" in work_sig
    assert "exec_code" not in mgmt_sig
    print("[exec_workspace_is_worker_only] OK")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def _async_tests(tmp_root: Path) -> None:
    for sub in (
        "r36",
        "r38",
        "r53",
        "r52",
        "r59",
        "r43-eats",
        "r43-tc",
        "r45",
        "r47",
        "r41-tui",
    ):
        (tmp_root / sub).mkdir()
    await test_hardlink_write_through_refused(tmp_root / "r36")
    await test_hardlink_read_disclosure_refused(tmp_root / "r38")
    await test_archivist_raises_skipped_when_safe_recent_eats_middle()
    await test_archivist_raises_skipped_when_original_task_has_tool_calls()
    await test_maybe_compact_distinguishes_skip_vs_transient()
    await test_maybe_compact_always_emits_end_event_even_on_raise()
    await test_failed_read_does_not_trigger_lost_update_warning(tmp_root / "r52")
    await test_list_workspace_filters_tempfiles_and_surfaces_symlinks(tmp_root / "r53")
    await test_lost_update_warning_survives_path_form_drift(tmp_root / "r59")


def _sync_tests(tmp_root: Path) -> None:
    for sub in (
        "r37",
        "r39",
        "r48",
        "r58",
        "r60",
        "r68",
        "r41",
    ):
        (tmp_root / sub).mkdir()
    test_parent_component_symlink_refused_by_realpath_check(tmp_root / "r37")
    test_session_save_cleans_tempfile_on_json_dump_failure(tmp_root / "r39")
    test_compaction_floor_persists_through_rehydrate()
    test_compaction_floor_old_snapshot_defaults_to_len_messages()
    test_truncate_for_log_recurses_into_nested_dicts()
    test_tui_trim_for_activity_recurses()
    test_stats_skips_replay_events(tmp_root / "r48")
    test_is_context_length_error_handles_anthropic_body()
    test_seal_tool_batch_rejects_out_of_range_index()
    test_bulk_load_emits_spawns_depth_ordered(tmp_root / "r58")
    test_normalize_workspace_path_collapses_dot_segments()
    test_diff_tools_against_skips_missing_bucket(tmp_root / "r60")
    test_safe_path_rejects_newline(tmp_root / "r68")
    test_read_unblock_input_lets_keyboard_interrupt_propagate()
    test_exec_workspace_is_worker_only()


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        asyncio.run(_async_tests(root))
        _sync_tests(root)
    print("\nAll round-three regression tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
