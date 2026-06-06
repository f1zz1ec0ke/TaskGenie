"""Tests for the round-two review fixes.

One test per non-trivial round-two finding (R1-R35 in REVIEW_NOTES.md).
Run with: python -m tests.test_review_rt2
"""
from __future__ import annotations

import asyncio
import errno
import json
import os
import re
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from taskgenie import agent as agent_module
from taskgenie import context as context_module
from taskgenie.agent import Agent, _is_context_length_error
from taskgenie.archivist import _safe_recent_keep
from taskgenie.config import CONFIG
from taskgenie.context import dump_large_tool_result
from taskgenie.event_logger import _truncate_for_log
from taskgenie.events import BUS, Event
from taskgenie.prompts import CEO_ROLE_PROMPT
from taskgenie.stats import compute as stats_compute
from taskgenie.tools.work import (
    _exec_code,
    _exec_workspace,
    _read_file,
    _read_workspace,
    _safe_path,
    _scrubbed_env,
    _write_file,
    _write_workspace,
)


def _tc(call_id: str, tool_name: str, **args):
    return {"id": call_id, "name": tool_name, "arguments": json.dumps(args)}


def _new_ceo(scratch_root: str | None = None) -> Agent:
    """Build a fresh CEO for a test. Uses mkdtemp (not TemporaryDirectory)
    because the directory must outlive the call -- the old fixture-style
    `with ... return` deleted the tempdir before the test actually ran."""
    Agent.REGISTRY.clear()
    if scratch_root is None:
        scratch_root = tempfile.mkdtemp(prefix="tg-test-")
    Agent.SCRATCH_ROOT = scratch_root
    Agent.WORKSPACE_ROOT = scratch_root
    return Agent(name="CEO", role_type="ceo", system_prompt=CEO_ROLE_PROMPT)


# ---------------------------------------------------------------------------
# R1: Symlink TOCTOU -- _safe_path validation + O_NOFOLLOW open refuses to
# follow planted symlinks for both create and overwrite modes.
# ---------------------------------------------------------------------------


def _has_symlink_support() -> bool:
    if not hasattr(os, "symlink"):
        return False
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "src")
        with open(src, "w") as f:
            f.write("x")
        try:
            os.symlink(src, os.path.join(d, "link"))
            return True
        except (OSError, NotImplementedError):
            return False


async def test_write_workspace_refuses_to_follow_planted_symlink(tmp_path: Path) -> None:
    """Two layers of defense:

    1. `_safe_path`'s .resolve() chases a pre-existing symlink and, if it
       resolves outside `base`, rejects with 'Path escapes scratch dir'.
    2. If a symlink is planted AFTER validation passes (the TOCTOU race),
       the actual open call uses O_NOFOLLOW and refuses to follow it.

    Either path is acceptable as long as the sentinel file outside the
    workspace is NOT overwritten. The second-layer behavior is tested
    explicitly below in test_write_text_safe_refuses_symlink_at_leaf.
    """
    if not _has_symlink_support():
        print("[symlink_planted] SKIP (no symlink support)")
        return

    base = tmp_path / "ws"
    base.mkdir()
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("OUTSIDE-DO-NOT-OVERWRITE", encoding="utf-8")

    # Plant a symlink at the target path BEFORE the write call.
    target_path = base / "victim.txt"
    os.symlink(str(sentinel), str(target_path))

    class _FakeAgent:
        workspace_dir = str(base)
        scratch_dir = str(base)
        messages: list = []
        read_workspace_paths: set = set()

    # _safe_path resolves through the existing symlink, target lands
    # outside base, ValueError is the surface. The dispatcher wraps that
    # — for now we accept it propagating out, but verify sentinel
    # survives either way.
    try:
        result = await _write_workspace(
            _FakeAgent(), {"path": "victim.txt", "content": "owned", "mode": "create"}
        )
        # If we got here, _safe_path didn't reject (target was inside
        # base after all) -- but the open must have refused.
        assert "ERROR" in result, result
    except ValueError as e:
        assert "escape" in str(e).lower(), e
    assert sentinel.read_text(encoding="utf-8") == "OUTSIDE-DO-NOT-OVERWRITE", (
        f"sentinel was overwritten via symlink! contents={sentinel.read_text()!r}"
    )
    print("[write_workspace_refuses_to_follow_planted_symlink] OK")


async def test_write_text_safe_refuses_symlink_at_leaf(tmp_path: Path) -> None:
    """Direct test of the O_NOFOLLOW guard in _write_text_safe.

    This is the layer that catches a true TOCTOU race -- a symlink
    planted at the leaf path AFTER _safe_path validated it. We simulate
    by planting a symlink at a path within the base (so .resolve() of
    the target leads to /etc/passwd which IS outside, BUT we bypass
    _safe_path here to test the open layer alone).
    """
    if not _has_symlink_support():
        print("[write_text_safe_refuses_symlink_at_leaf] SKIP (no symlink support)")
        return

    from taskgenie.tools.work import _write_text_safe

    base = tmp_path / "ws"
    base.mkdir()
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("DO-NOT-TOUCH", encoding="utf-8")

    leaf = base / "leaf.txt"
    os.symlink(str(sentinel), str(leaf))

    for mode in ("create", "overwrite", "append"):
        try:
            _write_text_safe(leaf, base, "boom", mode)
        except (FileExistsError, OSError) as e:
            # Both are acceptable -- create gets EEXIST (the symlink IS
            # a file from open()'s POV); overwrite/append get ELOOP.
            if isinstance(e, OSError) and e.errno not in (errno.ELOOP, errno.EEXIST):
                raise
        else:
            raise AssertionError(
                f"_write_text_safe({mode!r}) wrote through the symlink!"
            )
    assert sentinel.read_text(encoding="utf-8") == "DO-NOT-TOUCH"
    print("[write_text_safe_refuses_symlink_at_leaf] OK")


def test_safe_path_rejects_nul_byte(tmp_path: Path) -> None:
    """R33: NUL bytes (and other control chars; R68) in paths should be
    rejected upfront rather than surfacing as confusing OSError further
    down the stack."""
    base = tmp_path / "scratch"
    base.mkdir()
    try:
        _safe_path(str(base), "foo\x00bar")
    except ValueError as e:
        assert "control character" in str(e) and "\\x00" in str(e), e
    else:
        raise AssertionError("expected ValueError for NUL byte")

    # Round-three R68: newlines and other control chars are also rejected.
    try:
        _safe_path(str(base), "foo\nbar")
    except ValueError as e:
        assert "control character" in str(e), e
    else:
        raise AssertionError("expected ValueError for newline in path")

    print("[safe_path_rejects_nul_byte] OK")


# ---------------------------------------------------------------------------
# R2: Tool pipeline guard -- a failure in dump_large_tool_result must NOT
# leave a dangling tool_call_id in the agent's messages.
# ---------------------------------------------------------------------------


class _LLMOneListWorkspaceCall:
    """Calls list_workspace once. After that the agent runs to exhaustion
    (no further tool_calls). Used to test that a downstream dump failure
    still seals the tool_call_id."""

    def __init__(self) -> None:
        self.turn = 0

    async def model_name(self) -> str:
        return "scripted"

    async def chat(self, messages, tools=None, **_kwargs):
        self.turn += 1
        if self.turn == 1:
            return {
                "content": "listing",
                "tool_calls": [_tc("call_lsws_1", "list_workspace")],
            }
        return {"content": "", "tool_calls": []}


async def test_tool_pipeline_guard_seals_on_dump_failure() -> None:
    """If dump_large_tool_result raises (e.g. disk full mid-write), the
    agent must still emit a `tool` reply for the tool_call_id and return
    an error RunResult rather than leaving the conversation dangling."""
    ceo = _new_ceo()
    agent_module.LLM = _LLMOneListWorkspaceCall()

    # Monkeypatch dump_large_tool_result to raise. Reference both modules
    # because agent.py does `from taskgenie.context import dump_large_tool_result`
    # so the name in agent_module's namespace must be replaced too.
    def _boom(*args, **kwargs):
        raise OSError(errno.ENOSPC, "disk full (synthetic)")

    original = agent_module.dump_large_tool_result
    agent_module.dump_large_tool_result = _boom
    try:
        result = await ceo.run("trip the dump")
    finally:
        agent_module.dump_large_tool_result = original

    assert result.kind == "error", result
    # Verify NO dangling tool_call_ids.
    for i, msg in enumerate(ceo.messages):
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            ok = any(
                later.get("role") == "tool"
                and later.get("tool_call_id") == tc["id"]
                for later in ceo.messages[i + 1 :]
            )
            assert ok, f"dangling tool_call_id {tc['id']!r} after pipeline error"
    # Specifically: the call_lsws_1 reply should be [tool_pipeline_error].
    pipeline_replies = [
        m for m in ceo.messages
        if m.get("role") == "tool" and m.get("tool_call_id") == "call_lsws_1"
    ]
    assert pipeline_replies, "no reply for call_lsws_1"
    assert "[tool_pipeline_error]" in pipeline_replies[0]["content"]
    print("[tool_pipeline_guard_seals_on_dump_failure] OK")


# ---------------------------------------------------------------------------
# R4: exec_code exposes TASKGENIE_WORKSPACE so workers can run their own
# deliverables.
# ---------------------------------------------------------------------------


def test_scrubbed_env_exposes_workspace_and_scratch(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    sc = tmp_path / "sc"
    ws.mkdir()
    sc.mkdir()

    class _FakeAgent:
        workspace_dir = str(ws)
        scratch_dir = str(sc)

    env = _scrubbed_env(_FakeAgent())
    assert env.get("TASKGENIE_WORKSPACE") == str(ws), env
    assert env.get("TASKGENIE_SCRATCH") == str(sc), env
    # Sanity: a secret env var that isn't in passthrough must NOT leak.
    assert "TASKGENIE_PROBABLY_NOT_PRESENT" not in env
    print("[scrubbed_env_exposes_workspace_and_scratch] OK")


async def test_exec_code_can_read_workspace_via_env_var(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    sc = tmp_path / "sc"
    ws.mkdir()
    sc.mkdir()
    (ws / "marker.txt").write_text("HELLO-FROM-WORKSPACE", encoding="utf-8")

    class _FakeAgent:
        workspace_dir = str(ws)
        scratch_dir = str(sc)

    result = await _exec_code(
        _FakeAgent(),
        {
            "language": "shell",
            "code": 'cat "$TASKGENIE_WORKSPACE/marker.txt"',
        },
    )
    assert "HELLO-FROM-WORKSPACE" in result, result
    assert "exit_code=0" in result, result
    print("[exec_code_can_read_workspace_via_env_var] OK")


async def test_exec_workspace_runs_with_workspace_cwd(tmp_path: Path) -> None:
    """exec_workspace's CWD is the workspace, so a snippet can reference
    a file by its relative name without constructing an absolute path."""
    ws = tmp_path / "ws"
    sc = tmp_path / "sc"
    ws.mkdir()
    sc.mkdir()
    (ws / "game.py").write_text("print('LAUNCHED:', 42)\n", encoding="utf-8")

    class _FakeAgent:
        workspace_dir = str(ws)
        scratch_dir = str(sc)

    result = await _exec_workspace(
        _FakeAgent(),
        {"language": "shell", "code": "python game.py"},
    )
    assert "exit_code=0" in result, result
    assert "LAUNCHED: 42" in result, result

    # And scratch is reachable from the workspace cwd via the escape-hatch env var.
    result = await _exec_workspace(
        _FakeAgent(),
        {"language": "shell", "code": 'echo "$TASKGENIE_SCRATCH"'},
    )
    assert str(sc) in result, result
    print("[exec_workspace_runs_with_workspace_cwd] OK")


async def test_exec_workspace_errors_when_no_workspace(tmp_path: Path) -> None:
    """If workspace_dir isn't configured, exec_workspace returns the
    standard 'workspace not configured' error rather than running with
    an empty/wrong cwd."""
    class _NoWsAgent:
        workspace_dir = None
        scratch_dir = str(tmp_path / "sc")

    Path(_NoWsAgent.scratch_dir).mkdir(exist_ok=True)
    result = await _exec_workspace(
        _NoWsAgent(),
        {"language": "shell", "code": "echo hi"},
    )
    assert "ERROR" in result and "workspace" in result.lower(), result
    print("[exec_workspace_errors_when_no_workspace] OK")


# ---------------------------------------------------------------------------
# R5: Archivist slice boundary -- snap forward so recent band never starts
# with a `tool` whose matching assistant is in the summarised middle.
# ---------------------------------------------------------------------------


def test_archivist_snaps_recent_off_tool_boundary() -> None:
    # [s, u, assistant(tc), tool] -- keep_recent=1 would land on tool,
    # snap to 2 to include the assistant.
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {
            "role": "assistant",
            "content": "a",
            "tool_calls": [
                {"id": "x", "type": "function", "function": {"name": "f", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "x", "content": "t"},
    ]
    assert _safe_recent_keep(messages, 1) == 2, "should snap to include assistant"

    # Multiple tool replies after one assistant: snap past all of them.
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {
            "role": "assistant",
            "content": "a",
            "tool_calls": [
                {"id": "x", "type": "function", "function": {"name": "f", "arguments": "{}"}},
                {"id": "y", "type": "function", "function": {"name": "g", "arguments": "{}"}},
                {"id": "z", "type": "function", "function": {"name": "h", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "x", "content": "1"},
        {"role": "tool", "tool_call_id": "y", "content": "2"},
        {"role": "tool", "tool_call_id": "z", "content": "3"},
    ]
    assert _safe_recent_keep(messages, 2) == 4, "should snap back to assistant"

    # When the recent band already starts with assistant, no change.
    assert _safe_recent_keep(messages, 4) == 4

    print("[archivist_snaps_recent_off_tool_boundary] OK")


# ---------------------------------------------------------------------------
# R7: Compaction emits start/end events on the TARGET agent (not as a
# depth-99 ghost spawn). R15 (stats: spawn-replay safe) tested here too.
# ---------------------------------------------------------------------------


async def test_compaction_emits_events_on_target_agent(tmp_path: Path) -> None:
    """Force a compaction and verify the events come through on the
    target agent's id, with no rogue spawn for an Archivist node."""
    ceo = _new_ceo(str(tmp_path))

    # Subscribe a probe queue so we can read the bus directly.
    probe = BUS.subscribe()
    try:
        # Stub the archivist's compaction to "succeed" with a smaller
        # message list so _maybe_compact emits a successful end event.
        async def _stub_compact(name, messages, keep_recent=None):
            # Return [system, user, summary] -- definitely shorter.
            return messages[:2] + [
                {"role": "user", "content": "[archived summary]: ..."}
            ]

        original = agent_module.compact_messages
        agent_module.compact_messages = _stub_compact
        try:
            # Fill agent with enough messages that should_compact triggers.
            # We bypass should_compact by calling _maybe_compact(force=True).
            ceo.messages.extend(
                [
                    {"role": "user", "content": "fill"} for _ in range(10)
                ]
            )
            await ceo._maybe_compact(force=True)
        finally:
            agent_module.compact_messages = original

        events: list[Event] = []
        while not probe.empty():
            events.append(probe.get_nowait())
    finally:
        BUS.unsubscribe(probe)

    # We expect exactly one compaction_start and one compaction_end on
    # CEO's id. NO `spawn` event for an Archivist-* agent.
    starts = [e for e in events if e.kind == "compaction_start"]
    ends = [e for e in events if e.kind == "compaction_end"]
    archivist_spawns = [
        e for e in events
        if e.kind == "spawn" and "Archivist" in e.agent_name
    ]
    assert len(starts) == 1, starts
    assert len(ends) == 1, ends
    assert starts[0].agent_id == ceo.id
    assert ends[0].agent_id == ceo.id
    assert ends[0].data.get("success") is True, ends[0].data
    assert not archivist_spawns, archivist_spawns
    print("[compaction_emits_events_on_target_agent] OK")


# ---------------------------------------------------------------------------
# R8: read_workspace_paths persistent set -- the lost-update warning
# survives a compaction that wipes the original read_workspace tool_call
# from agent.messages.
# ---------------------------------------------------------------------------


async def test_lost_update_warning_survives_compaction(tmp_path: Path) -> None:
    """Set up an agent that READ a workspace path then has its messages
    cleared (simulating compaction). The overwrite warning must still fire
    because the persistent set carried the path forward."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "doc.md").write_text("v1", encoding="utf-8")

    class _FakeAgent:
        workspace_dir = str(ws)
        scratch_dir = str(ws)
        messages: list = []  # empty -- the original read_workspace call is GONE
        read_workspace_paths: set = {"doc.md"}  # but the set survived

    result = await _write_workspace(
        _FakeAgent(),
        {"path": "doc.md", "content": "v2", "mode": "overwrite"},
    )
    assert "WARNING" in result, (
        f"lost-update warning didn't fire even though path was in the "
        f"persistent set; tool result was: {result!r}"
    )
    print("[lost_update_warning_survives_compaction] OK")


async def test_read_workspace_records_path_in_persistent_set(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "doc.md").write_text("v1", encoding="utf-8")

    class _FakeAgent:
        workspace_dir = str(ws)
        scratch_dir = str(ws)
        messages: list = []
        read_workspace_paths: set = set()

    a = _FakeAgent()
    await _read_workspace(a, {"path": "doc.md"})
    assert "doc.md" in a.read_workspace_paths, a.read_workspace_paths
    print("[read_workspace_records_path_in_persistent_set] OK")


# ---------------------------------------------------------------------------
# R9: _maybe_compact doesn't fire every turn near threshold.
# ---------------------------------------------------------------------------


async def test_maybe_compact_repeat_guard(tmp_path: Path) -> None:
    """After a compact that returns the same length (no-op), subsequent
    calls within a few messages of the floor should NOT re-trigger the
    archivist."""
    ceo = _new_ceo(str(tmp_path))

    call_count = [0]

    async def _noop_compact(name, messages, keep_recent=None):
        call_count[0] += 1
        return messages  # no shrink

    agent_module.compact_messages = _noop_compact
    try:
        ceo.messages.extend(
            [{"role": "user", "content": "x"} for _ in range(10)]
        )
        # First call (forced) -> invokes compact, no shrink, sets floor.
        await ceo._maybe_compact(force=True)
        first_call = call_count[0]
        assert first_call >= 1

        # Add 2 more messages (below the 4-msg guard threshold).
        ceo.messages.extend([{"role": "user", "content": "y"} for _ in range(2)])

        # Stub `should_compact` to return True so the threshold gate
        # would let it through; the repeat guard should still block it.
        original_should = agent_module.should_compact
        agent_module.should_compact = lambda *a, **kw: True
        try:
            await ceo._maybe_compact()  # not forced
        finally:
            agent_module.should_compact = original_should
        assert call_count[0] == first_call, (
            f"expected no extra compact call (guard should block) "
            f"but got {call_count[0]} (was {first_call})"
        )
        print("[maybe_compact_repeat_guard] OK")
    finally:
        # Restore the real one for other tests.
        from taskgenie.archivist import compact_messages as real
        agent_module.compact_messages = real


# ---------------------------------------------------------------------------
# R10: _is_context_length_error tightened -- matches real wordings, not
# innocuous noise.
# ---------------------------------------------------------------------------


def test_is_context_length_error_matches_real_wordings() -> None:
    positive = [
        Exception("Error code: 400 - {'error': {'code': 'context_length_exceeded'}}"),
        Exception("This model's maximum context length is 8192 tokens"),
        Exception("Request too large: prompt is too long for the model"),
        Exception("too many input tokens"),
    ]
    for e in positive:
        assert _is_context_length_error(e), f"missed: {e}"
    print("[is_context_length_error_matches_real_wordings] OK")


def test_is_context_length_error_ignores_noise() -> None:
    negative = [
        Exception("context length check passed; ok"),
        Exception("warn: context length cache hit 0%"),
        Exception("connection reset"),
        Exception("rate limit exceeded"),  # different limit
        Exception("invalid api key"),
        Exception(""),
    ]
    for e in negative:
        assert not _is_context_length_error(e), f"false positive on: {e!r}"

    # ALSO matches when the exception has a structured `code` attr
    # (OpenAI client). Synthesise.
    class _Synth(Exception):
        code = "context_length_exceeded"

    assert _is_context_length_error(_Synth("boom"))
    print("[is_context_length_error_ignores_noise] OK")


# ---------------------------------------------------------------------------
# R11: _tool_fire emits a `state` event (not just direct mutation).
# ---------------------------------------------------------------------------


async def test_tool_fire_emits_state_event(tmp_path: Path) -> None:
    ceo = _new_ceo(str(tmp_path))
    # Hire a child first so we have someone to fire.
    res = ceo._tool_hire(name="Hand", role_type="worker", role_prompt="do stuff")
    child_id = res["agent_id"]

    probe = BUS.subscribe()
    try:
        ceo._tool_fire(agent_id=child_id, reason="not needed")
        # Drain
        events: list[Event] = []
        while not probe.empty():
            events.append(probe.get_nowait())
    finally:
        BUS.unsubscribe(probe)

    state_events = [
        e for e in events
        if e.kind == "state" and e.agent_id == child_id
    ]
    assert state_events, f"no state event for {child_id}; got {events}"
    fired = [e for e in state_events if e.data.get("status") == "fired"]
    assert fired, f"no fired-state event; events were: {state_events}"
    print("[tool_fire_emits_state_event] OK")


# ---------------------------------------------------------------------------
# R14: append-mode atomicity for ≤PIPE_BUF payloads -- two concurrent
# small appends should produce a file whose two payloads are contiguous
# (no interleaving).
# ---------------------------------------------------------------------------


async def test_append_mode_atomic_for_small_payloads(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()

    class _FakeAgent:
        workspace_dir = str(ws)
        scratch_dir = str(ws)
        messages: list = []
        read_workspace_paths: set = set()

    payload_a = "A" * 1024 + "\n"
    payload_b = "B" * 1024 + "\n"

    # Run both appends concurrently.
    a = _FakeAgent()
    b = _FakeAgent()
    await asyncio.gather(
        _write_workspace(a, {"path": "log.txt", "content": payload_a, "mode": "append"}),
        _write_workspace(b, {"path": "log.txt", "content": payload_b, "mode": "append"}),
    )

    content = (ws / "log.txt").read_text(encoding="utf-8")
    # Both payloads must be present in full, in some order, without
    # interleaving (a runs of all-A then all-B, or all-B then all-A).
    assert content in (payload_a + payload_b, payload_b + payload_a), (
        f"payloads interleaved; got: {content!r}"
    )
    print("[append_mode_atomic_for_small_payloads] OK")


# ---------------------------------------------------------------------------
# R15: stats.compute preserves per-agent counts across a spawn replay.
# ---------------------------------------------------------------------------


def test_stats_preserves_counts_across_spawn_replay(tmp_path: Path) -> None:
    """Emulate a session that spawned, ran a few LLM calls, was saved,
    then resumed (which re-publishes a spawn event for the same id).
    Without the fix, stats.compute would reset per-agent counts on the
    replay and the panel would lie."""
    log = tmp_path / "events.jsonl"
    rows = [
        {
            "kind": "spawn",
            "agent_id": "a1",
            "agent_name": "CEO",
            "ts": time.time(),
            "data": {"role_type": "ceo"},
        },
        {
            "kind": "llm_call",
            "agent_id": "a1",
            "agent_name": "CEO",
            "ts": time.time(),
            "data": {"prompt_tokens": 100, "completion_tokens": 50},
        },
        {
            "kind": "tool_call",
            "agent_id": "a1",
            "agent_name": "CEO",
            "ts": time.time(),
            "data": {"tool": "hire"},
        },
        # Replay (resume) — same id.
        {
            "kind": "spawn",
            "agent_id": "a1",
            "agent_name": "CEO",
            "ts": time.time(),
            "data": {"role_type": "ceo"},
        },
        {
            "kind": "llm_call",
            "agent_id": "a1",
            "agent_name": "CEO",
            "ts": time.time(),
            "data": {"prompt_tokens": 30, "completion_tokens": 20},
        },
    ]
    with log.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")

    s = stats_compute(log, ceo_id="a1")
    assert "a1" in s.agents
    a = s.agents["a1"]
    # Before the fix, replay would wipe llm_calls back to 0 and we'd
    # only count the second call (1). With the fix, both count.
    assert a.llm_calls == 2, f"expected 2 LLM calls preserved, got {a.llm_calls}"
    assert a.prompt_tokens == 130
    assert a.completion_tokens == 70
    assert a.tool_calls.get("hire") == 1
    # Aggregated total also.
    assert s.llm_calls_total == 2
    print("[stats_preserves_counts_across_spawn_replay] OK")


# ---------------------------------------------------------------------------
# R16: event_logger truncates oversized data fields.
# ---------------------------------------------------------------------------


def test_event_logger_truncates_large_string_fields() -> None:
    big = "x" * (20 * 1024)
    row = _truncate_for_log({"args": big, "tool": "write_workspace"})
    assert len(row["args"]) < len(big), "no truncation happened"
    assert "truncated" in row["args"]
    # Small fields untouched.
    assert row["tool"] == "write_workspace"
    print("[event_logger_truncates_large_string_fields] OK")


# ---------------------------------------------------------------------------
# R23: empty narrate event does NOT reset thinking timer (touch).
# ---------------------------------------------------------------------------


def test_empty_narrate_does_not_touch_view() -> None:
    from taskgenie.tui import TUI

    tui = TUI(task="t")
    tui._handle_event(
        Event("spawn", "a1", "Worker", data={"role_type": "worker", "depth": 1})
    )
    view = tui.agents["a1"]
    # Manually set last_activity into the past so we can detect a reset.
    view.last_activity = time.time() - 100.0
    view.status = "working"
    old_activity = view.last_activity

    tui._handle_event(Event("narrate", "a1", "Worker", data={"text": ""}))
    assert view.last_activity == old_activity, (
        "empty narrate should NOT have called _touch (which resets last_activity); "
        "the thinking-N-s indicator depends on this"
    )
    # And a non-empty narrate should still touch.
    tui._handle_event(Event("narrate", "a1", "Worker", data={"text": "hello"}))
    assert view.last_activity > old_activity, "non-empty narrate should _touch"
    print("[empty_narrate_does_not_touch_view] OK")


# ---------------------------------------------------------------------------
# R29: _seal_tool_batch trailing-call branch -- complete is FIRST in the
# batch with another tool_call AFTER.
# ---------------------------------------------------------------------------


class _CompleteFirstThenTrailing:
    async def model_name(self) -> str:
        return "scripted"

    async def chat(self, messages, tools=None, **_kwargs):
        return {
            "content": "early complete",
            "tool_calls": [
                _tc("call_complete_first", "complete", report="done early"),
                _tc(
                    "call_trailing_fu",
                    "suggest_follow_up",
                    what_is_needed="x",
                    rationale="y",
                    who_should_do_it="z",
                ),
            ],
        }


async def test_complete_first_seals_trailing_call() -> None:
    """The real trailing-seal test: complete is FIRST in the batch with
    another tool_call after. The trailing call must get a [skipped:]
    sentinel reply so the conversation isn't dangling."""
    ceo = _new_ceo()
    agent_module.LLM = _CompleteFirstThenTrailing()
    result = await ceo.run("do this")
    assert result.kind == "done"

    replies = {
        m["tool_call_id"]: m["content"]
        for m in ceo.messages
        if m.get("role") == "tool"
    }
    assert "call_complete_first" in replies, replies
    assert "call_trailing_fu" in replies, (
        "trailing tool_call wasn't sealed; this is the bug the existing "
        "test missed by putting complete LAST in the batch"
    )
    assert "skipped" in replies["call_trailing_fu"].lower(), (
        f"trailing reply isn't the sealed sentinel: {replies['call_trailing_fu']!r}"
    )
    print("[complete_first_seals_trailing_call] OK")


# ---------------------------------------------------------------------------
# R32: dump_large_tool_result filename collision avoidance via hash suffix.
# ---------------------------------------------------------------------------


def test_dump_filename_unique_per_call_id(tmp_path: Path) -> None:
    """Two long call_ids that differ only beyond char 24 must produce
    distinct dump filenames (previously they'd collide and the second
    write would overwrite the first)."""
    long_a = "call_" + "x" * 30 + "_a"
    long_b = "call_" + "x" * 30 + "_b"
    content = "y" * 1024  # any string > threshold

    _, paths_a = dump_large_tool_result(
        content, str(tmp_path), turn_idx=0, call_id=long_a,
        tool_name="exec_code", threshold_bytes=100, chunk_margin=0,
    )
    _, paths_b = dump_large_tool_result(
        content, str(tmp_path), turn_idx=0, call_id=long_b,
        tool_name="exec_code", threshold_bytes=100, chunk_margin=0,
    )
    assert paths_a is not None and paths_b is not None
    # The two call_ids must produce DISJOINT filename sets -- if the call-id
    # hash collided, the second call would overwrite the first's chunks.
    names_a = {Path(p).name for p in paths_a}
    names_b = {Path(p).name for p in paths_b}
    assert names_a.isdisjoint(names_b), (names_a, names_b)
    # Every chunk from both calls coexists on disk (nothing clobbered).
    files = {p.name for p in tmp_path.iterdir()}
    assert files == names_a | names_b
    print("[dump_filename_unique_per_call_id] OK")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def _async_tests(tmp_root: Path) -> None:
    await test_write_workspace_refuses_to_follow_planted_symlink(tmp_root / "r1")
    await test_write_text_safe_refuses_symlink_at_leaf(tmp_root / "r1c")
    await test_tool_pipeline_guard_seals_on_dump_failure()
    await test_exec_code_can_read_workspace_via_env_var(tmp_root / "r4b")
    await test_exec_workspace_runs_with_workspace_cwd(tmp_root / "r4c")
    await test_exec_workspace_errors_when_no_workspace(tmp_root / "r4d")
    await test_compaction_emits_events_on_target_agent(tmp_root / "r7")
    await test_lost_update_warning_survives_compaction(tmp_root / "r8a")
    await test_read_workspace_records_path_in_persistent_set(tmp_root / "r8b")
    await test_maybe_compact_repeat_guard(tmp_root / "r9")
    await test_tool_fire_emits_state_event(tmp_root / "r11")
    await test_append_mode_atomic_for_small_payloads(tmp_root / "r14")
    await test_complete_first_seals_trailing_call()


def _sync_tests(tmp_root: Path) -> None:
    (tmp_root / "r1b").mkdir()
    test_safe_path_rejects_nul_byte(tmp_root / "r1b")
    (tmp_root / "r4a").mkdir()
    test_scrubbed_env_exposes_workspace_and_scratch(tmp_root / "r4a")
    test_archivist_snaps_recent_off_tool_boundary()
    test_is_context_length_error_matches_real_wordings()
    test_is_context_length_error_ignores_noise()
    (tmp_root / "r15").mkdir()
    test_stats_preserves_counts_across_spawn_replay(tmp_root / "r15")
    test_event_logger_truncates_large_string_fields()
    test_empty_narrate_does_not_touch_view()
    (tmp_root / "r32").mkdir()
    test_dump_filename_unique_per_call_id(tmp_root / "r32")


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        # Make subdirs the async tests need.
        for sub in (
            "r1", "r1c", "r4b", "r4c", "r4d", "r7", "r8a", "r8b",
            "r9", "r11", "r14",
        ):
            (root / sub).mkdir()
        asyncio.run(_async_tests(root))
        _sync_tests(root)
    print("\nAll round-two regression tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
