"""Tests for compaction telemetry surfaced through RunResult.

When a child agent runs through one or more compactions during its
assigned task, the parent (via `await_results` -> `to_tool_payload`)
should see a `compaction` field summarising what happened. A high
count is a useful "task was too big, split it next time" signal.

Run with: python -m tests.test_compaction_telemetry
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from taskgenie import agent as agent_module
from taskgenie.agent import Agent
from taskgenie.archivist import CompactionSkipped, CompactionTransientFailure
from taskgenie.result import RunResult


def _new_agent() -> Agent:
    Agent.REGISTRY.clear()
    d = tempfile.mkdtemp(prefix="tg-test-tele-")
    Agent.SCRATCH_ROOT = d
    Agent.WORKSPACE_ROOT = d
    return Agent(name="W", role_type="worker", system_prompt="...")


def test_compaction_stats_starts_zeroed() -> None:
    a = _new_agent()
    assert a.compaction_stats["count"] == 0
    assert a.compaction_stats["skipped"] == 0
    assert a.compaction_stats["failed"] == 0
    assert a.compaction_stats["messages_compacted"] == 0
    assert a.compaction_stats["tokens_saved"] == 0
    assert a.compaction_stats["duration_ms"] == 0.0
    print("[compaction_stats_starts_zeroed] OK")


async def test_successful_compaction_bumps_count_and_savings() -> None:
    a = _new_agent()
    a.messages.extend([{"role": "user", "content": "x" * 500}] * 20)

    async def _stub_shrink(name, messages, keep_recent=None):
        # Definitely shorter and definitely shorter in tokens.
        return [messages[0], {"role": "user", "content": "summary"}]

    original = agent_module.compact_messages
    agent_module.compact_messages = _stub_shrink
    try:
        ok = await a._maybe_compact(force=True)
    finally:
        agent_module.compact_messages = original

    assert ok is True
    assert a.compaction_stats["count"] == 1
    assert a.compaction_stats["skipped"] == 0
    assert a.compaction_stats["failed"] == 0
    assert a.compaction_stats["messages_compacted"] > 0
    assert a.compaction_stats["tokens_saved"] > 0
    assert a.compaction_stats["duration_ms"] > 0.0
    print("[successful_compaction_bumps_count_and_savings] OK")


async def test_skipped_compaction_bumps_skipped_not_count() -> None:
    a = _new_agent()
    a.messages.extend([{"role": "user", "content": "x"}] * 5)

    async def _stub_skip(name, messages, keep_recent=None):
        raise CompactionSkipped("test skip")

    original = agent_module.compact_messages
    agent_module.compact_messages = _stub_skip
    try:
        ok = await a._maybe_compact(force=True)
    finally:
        agent_module.compact_messages = original

    assert ok is False
    assert a.compaction_stats["count"] == 0
    assert a.compaction_stats["skipped"] == 1
    assert a.compaction_stats["failed"] == 0
    assert a.compaction_stats["duration_ms"] > 0.0
    print("[skipped_compaction_bumps_skipped_not_count] OK")


async def test_transient_failure_bumps_failed_not_count() -> None:
    a = _new_agent()
    a.messages.extend([{"role": "user", "content": "x"}] * 5)

    async def _stub_transient(name, messages, keep_recent=None):
        raise CompactionTransientFailure("network blip")

    original = agent_module.compact_messages
    agent_module.compact_messages = _stub_transient
    try:
        ok = await a._maybe_compact(force=True)
    finally:
        agent_module.compact_messages = original

    assert ok is False
    assert a.compaction_stats["count"] == 0
    assert a.compaction_stats["skipped"] == 0
    assert a.compaction_stats["failed"] == 1
    print("[transient_failure_bumps_failed_not_count] OK")


async def test_unexpected_exception_still_counts_as_failed() -> None:
    """An unhandled exception from compact_messages propagates out, but
    the finally block must still tally it. Without this, a parent
    inspecting stats would see "compaction never happened" when in
    reality it crashed."""
    a = _new_agent()
    a.messages.extend([{"role": "user", "content": "x"}] * 5)

    async def _stub_explode(name, messages, keep_recent=None):
        raise RuntimeError("unexpected boom")

    original = agent_module.compact_messages
    agent_module.compact_messages = _stub_explode
    try:
        try:
            await a._maybe_compact(force=True)
        except RuntimeError:
            pass  # expected to propagate
    finally:
        agent_module.compact_messages = original

    assert a.compaction_stats["failed"] == 1, (
        "unhandled compact_messages exception should still bump 'failed'"
    )
    print("[unexpected_exception_still_counts_as_failed] OK")


async def test_multiple_compactions_accumulate() -> None:
    a = _new_agent()
    a.messages.extend([{"role": "user", "content": "x" * 100}] * 30)

    async def _stub_shrink(name, messages, keep_recent=None):
        return messages[:5]  # always shrinks

    original = agent_module.compact_messages
    agent_module.compact_messages = _stub_shrink
    try:
        # First compact succeeds; floor advances. Re-add msgs to allow
        # second compact past the 4-message guard.
        await a._maybe_compact(force=True)
        a.messages.extend([{"role": "user", "content": "y" * 100}] * 15)
        await a._maybe_compact(force=True)
    finally:
        agent_module.compact_messages = original

    assert a.compaction_stats["count"] == 2
    assert a.compaction_stats["messages_compacted"] > 0
    print("[multiple_compactions_accumulate] OK")


# ---------------------------------------------------------------------------
# RunResult / to_tool_payload integration
# ---------------------------------------------------------------------------


def test_run_result_has_compaction_stats_field() -> None:
    r = RunResult(kind="done", report="hi")
    assert hasattr(r, "compaction_stats")
    assert r.compaction_stats == {}
    print("[run_result_has_compaction_stats_field] OK")


def test_payload_omits_compaction_when_zeroed() -> None:
    """An agent that did NO compaction must NOT have a compaction
    field in its tool payload -- zero-padding every reply is noise."""
    r = RunResult(
        kind="done",
        report="quick task",
        compaction_stats={
            "count": 0,
            "skipped": 0,
            "failed": 0,
            "messages_compacted": 0,
            "tokens_saved": 0,
            "duration_ms": 0.0,
        },
    )
    payload = r.to_tool_payload()
    assert "compaction" not in payload, payload
    assert payload["status"] == "done"
    assert payload["report"] == "quick task"
    print("[payload_omits_compaction_when_zeroed] OK")


def test_payload_includes_compaction_when_count_nonzero() -> None:
    r = RunResult(
        kind="done",
        report="big task",
        compaction_stats={
            "count": 3,
            "skipped": 1,
            "failed": 0,
            "messages_compacted": 47,
            "tokens_saved": 12400,
            "duration_ms": 4210.5,
        },
    )
    payload = r.to_tool_payload()
    assert "compaction" in payload
    c = payload["compaction"]
    assert c["count"] == 3
    assert c["skipped"] == 1
    assert "failed" not in c, "zero values should be dropped to keep payload tight"
    assert c["messages_compacted"] == 47
    assert c["tokens_saved"] == 12400
    assert c["duration_ms"] == 4210  # rounded to whole ms
    print("[payload_includes_compaction_when_count_nonzero] OK")


def test_payload_includes_compaction_when_only_failed() -> None:
    """Even if no compaction succeeded, a parent should see that we
    TRIED -- a child that hammered the archivist 5 times without
    success is a meaningful signal."""
    r = RunResult(
        kind="error",
        error="something",
        compaction_stats={
            "count": 0,
            "skipped": 0,
            "failed": 5,
            "messages_compacted": 0,
            "tokens_saved": 0,
            "duration_ms": 1234.0,
        },
    )
    payload = r.to_tool_payload()
    assert "compaction" in payload
    assert payload["compaction"]["failed"] == 5
    assert payload["compaction"]["duration_ms"] == 1234
    print("[payload_includes_compaction_when_only_failed] OK")


def test_payload_blocked_includes_compaction() -> None:
    """All RunResult kinds (done/blocked/question/exhausted/error)
    should surface the same compaction telemetry."""
    r = RunResult(
        kind="blocked",
        reason="stuck",
        needs="help",
        compaction_stats={
            "count": 2,
            "skipped": 0,
            "failed": 0,
            "messages_compacted": 30,
            "tokens_saved": 8000,
            "duration_ms": 2000.0,
        },
    )
    payload = r.to_tool_payload()
    assert payload["status"] == "blocked"
    assert "compaction" in payload
    assert payload["compaction"]["count"] == 2
    print("[payload_blocked_includes_compaction] OK")


# ---------------------------------------------------------------------------
# End-to-end: agent.run() through to parent.await_results sees the stats
# ---------------------------------------------------------------------------


class _OneCompactionLLM:
    """Returns one narrate then a complete. The agent loop calls
    _maybe_compact before LLM.chat each turn; combined with a stub
    archivist below this exercises the full path."""

    def __init__(self) -> None:
        self.turn = 0

    async def model_name(self) -> str:
        return "scripted"

    async def chat(self, messages, tools=None, **_kwargs):
        self.turn += 1
        import json

        return {
            "content": "wrapping up",
            "tool_calls": [
                {
                    "id": "call_done_1",
                    "name": "complete",
                    "arguments": json.dumps({"report": "all done"}),
                }
            ],
        }


async def test_run_returns_result_with_live_stats() -> None:
    """The full agent.run() path: stats accumulated during the run
    should end up on the returned RunResult."""
    a = _new_agent()
    # Pre-bloat messages enough that the repeat-compact guard (which
    # requires >= 4 messages since the last floor) won't suppress the
    # in-loop _maybe_compact call.
    a.messages.extend([{"role": "user", "content": "x" * 200}] * 10)

    async def _stub_shrink(name, messages, keep_recent=None):
        return [messages[0], {"role": "user", "content": "summary"}]

    original_llm = agent_module.LLM
    original_compact = agent_module.compact_messages
    original_should = agent_module.should_compact

    agent_module.LLM = _OneCompactionLLM()
    agent_module.compact_messages = _stub_shrink
    # Force should_compact to return True so the pre-call compaction
    # fires regardless of history length.
    agent_module.should_compact = lambda *a, **kw: True

    try:
        result = await a.run("do the thing")
    finally:
        agent_module.LLM = original_llm
        agent_module.compact_messages = original_compact
        agent_module.should_compact = original_should

    assert result.kind == "done"
    assert result.compaction_stats["count"] >= 1, (
        f"expected at least one successful compact, got {result.compaction_stats}"
    )
    # And the payload exposes it for the parent.
    payload = result.to_tool_payload()
    assert "compaction" in payload, payload
    assert payload["compaction"]["count"] >= 1
    print("[run_returns_result_with_live_stats] OK")


# ---------------------------------------------------------------------------
# Persistence: stats survive a save/load round-trip
# ---------------------------------------------------------------------------


def test_compaction_stats_persists_through_rehydrate() -> None:
    a = _new_agent()
    a.compaction_stats["count"] = 7
    a.compaction_stats["skipped"] = 2
    a.compaction_stats["failed"] = 1
    a.compaction_stats["messages_compacted"] = 134
    a.compaction_stats["tokens_saved"] = 45000
    a.compaction_stats["duration_ms"] = 11234.5

    d = a.to_dict()
    assert d["compaction_stats"]["count"] == 7

    Agent.REGISTRY.clear()
    Agent.bulk_load({d["id"]: d})
    revived = Agent.REGISTRY[d["id"]]
    assert revived.compaction_stats["count"] == 7
    assert revived.compaction_stats["skipped"] == 2
    assert revived.compaction_stats["failed"] == 1
    assert revived.compaction_stats["messages_compacted"] == 134
    assert revived.compaction_stats["tokens_saved"] == 45000
    assert revived.compaction_stats["duration_ms"] == 11234.5
    print("[compaction_stats_persists_through_rehydrate] OK")


def test_compaction_stats_default_when_snapshot_lacks_field() -> None:
    """A pre-telemetry snapshot (no compaction_stats field) should
    rehydrate to the zeroed shape, not KeyError."""
    Agent.REGISTRY.clear()
    Agent.SCRATCH_ROOT = tempfile.mkdtemp(prefix="tg-test-tele-")
    Agent.WORKSPACE_ROOT = Agent.SCRATCH_ROOT
    d = {
        "id": "ceo-old",
        "name": "CEO",
        "role_type": "ceo",
        "system_prompt": "...",
        "parent_id": None,
        "child_ids": [],
        "depth": 0,
        "status": "idle",
        "messages": [{"role": "system", "content": "..."}],
        "scratch_dir": Agent.SCRATCH_ROOT,
        "blocker_reason": "",
        "blocker_needs": "",
        "follow_ups": [],
        # Note: no compaction_stats key (pre-telemetry snapshot).
    }
    Agent.bulk_load({"ceo-old": d})
    revived = Agent.REGISTRY["ceo-old"]
    assert revived.compaction_stats["count"] == 0
    assert revived.compaction_stats["duration_ms"] == 0.0
    print("[compaction_stats_default_when_snapshot_lacks_field] OK")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def _async_tests() -> None:
    await test_successful_compaction_bumps_count_and_savings()
    await test_skipped_compaction_bumps_skipped_not_count()
    await test_transient_failure_bumps_failed_not_count()
    await test_unexpected_exception_still_counts_as_failed()
    await test_multiple_compactions_accumulate()
    await test_run_returns_result_with_live_stats()


def _sync_tests() -> None:
    test_compaction_stats_starts_zeroed()
    test_run_result_has_compaction_stats_field()
    test_payload_omits_compaction_when_zeroed()
    test_payload_includes_compaction_when_count_nonzero()
    test_payload_includes_compaction_when_only_failed()
    test_payload_blocked_includes_compaction()
    test_compaction_stats_persists_through_rehydrate()
    test_compaction_stats_default_when_snapshot_lacks_field()


def main() -> int:
    asyncio.run(_async_tests())
    _sync_tests()
    print("\nAll compaction-telemetry tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
