"""Tests for PR A — the three critical/high bugs surfaced by the review.

A1. Agent.run() returning early on complete/report_blocker/ask_user used to
    leave the just-called tool_call_id (and any trailing tool_call_ids in
    the same batch) without a matching `{"role":"tool"}` reply. The next
    time those messages hit the LLM (e.g. a follow-up turn), the API
    would 400. _seal_tool_batch fixes it.

A2. tools/work.py:_safe_path used `str.startswith` for containment, which
    accepts siblings whose name shares a prefix with the base dir
    (/tmp/scratch-evil vs /tmp/scratch). Now uses parents-containment.

A3. tui.py:_build_log_panel ran the event log through Text.from_markup,
    which would re-interpret any [red]...[/] (etc.) the LLM put into a
    name, narration, or blocker reason. Agent-supplied fields are now
    escaped at the _log() call sites.

Run with: python -m tests.test_review_pra
"""
from __future__ import annotations

import asyncio
import io
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rich.console import Console

from taskgenie import agent as agent_module
from taskgenie.agent import Agent
from taskgenie.events import Event
from taskgenie.prompts import CEO_ROLE_PROMPT
from taskgenie.tools.work import _safe_path
from taskgenie.tui import TUI


# ---------------------------------------------------------------------------
# A1: tool_call_ids are sealed before early return
# ---------------------------------------------------------------------------


def _tc(call_id: str, tool_name: str, **args):
    return {"id": call_id, "name": tool_name, "arguments": json.dumps(args)}


def _assert_no_dangling_tool_calls(messages: list[dict]) -> None:
    """Walk the message log and verify every assistant.tool_call[].id has a
    following tool message with matching tool_call_id. This is exactly the
    invariant the OpenAI/Anthropic chat-completions API enforces."""
    for i, msg in enumerate(messages):
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls", []) or []:
            tc_id = tc["id"]
            # Find a matching tool message AFTER this assistant message.
            ok = False
            for later in messages[i + 1 :]:
                if (
                    later.get("role") == "tool"
                    and later.get("tool_call_id") == tc_id
                ):
                    ok = True
                    break
            assert ok, (
                f"dangling tool_call_id {tc_id!r} at message #{i}: "
                f"no following tool reply"
            )


class _CompleteOnTurnZero:
    """Scripted LLM that calls complete on its first turn, no other tools."""

    async def model_name(self) -> str:
        return "scripted"

    async def chat(self, messages, tools=None, **_kwargs):
        return {
            "content": "Done.",
            "tool_calls": [_tc("call_complete_1", "complete", report="hello")],
        }


class _BlockerOnTurnZero:
    async def model_name(self) -> str:
        return "scripted"

    async def chat(self, messages, tools=None, **_kwargs):
        return {
            "content": "stuck",
            "tool_calls": [
                _tc(
                    "call_block_1",
                    "report_blocker",
                    reason="no widget",
                    needs="give me a widget",
                )
            ],
        }


class _AskUserOnTurnZero:
    async def model_name(self) -> str:
        return "scripted"

    async def chat(self, messages, tools=None, **_kwargs):
        return {
            "content": "need input",
            "tool_calls": [
                _tc("call_ask_1", "ask_user", question="A or B?", options=["A", "B"])
            ],
        }


class _CompleteAfterSiblingTool:
    """Returns TWO tool_calls in one turn: a benign suggest_follow_up, then
    complete. The bug: trailing complete's tool_call_id never sealed; *also*
    pre-fix, complete itself never sealed even when alone."""

    async def model_name(self) -> str:
        return "scripted"

    async def chat(self, messages, tools=None, **_kwargs):
        return {
            "content": "both at once",
            "tool_calls": [
                _tc(
                    "call_fu_1",
                    "suggest_follow_up",
                    what_is_needed="a thing",
                    rationale="reason",
                    who_should_do_it="someone",
                ),
                _tc("call_complete_2", "complete", report="finished after followup"),
            ],
        }


def _new_ceo() -> Agent:
    """Build a fresh CEO. Uses mkdtemp (not TemporaryDirectory as a context
    manager) because the scratch dir must outlive this call -- the
    original `with TemporaryDirectory() as d: ... return Agent(...)`
    pattern deleted the tempdir before the test could use it, so every
    CEO created here was silently writing under a path that wouldn't be
    cleaned up by the context manager."""
    Agent.REGISTRY.clear()
    d = tempfile.mkdtemp(prefix="tg-test-pra-")
    Agent.SCRATCH_ROOT = d
    Agent.WORKSPACE_ROOT = d
    return Agent(name="CEO", role_type="ceo", system_prompt=CEO_ROLE_PROMPT)


async def test_complete_seals_its_own_tool_call() -> None:
    """Pre-fix: complete returned without appending a tool message for its
    own tool_call_id, leaving a dangling ID. Now it must seal."""
    ceo = _new_ceo()
    agent_module.LLM = _CompleteOnTurnZero()
    result = await ceo.run("do the thing")
    assert result.kind == "done", result
    _assert_no_dangling_tool_calls(ceo.messages)
    # Specifically: the LAST message must be a tool reply for the complete.
    last = ceo.messages[-1]
    assert last["role"] == "tool"
    assert last["tool_call_id"] == "call_complete_1"
    print("[complete_seals_its_own_tool_call] OK")


async def test_blocker_seals_its_own_tool_call() -> None:
    ceo = _new_ceo()
    agent_module.LLM = _BlockerOnTurnZero()
    result = await ceo.run("try the thing")
    assert result.kind == "blocked", result
    _assert_no_dangling_tool_calls(ceo.messages)
    last = ceo.messages[-1]
    assert last["role"] == "tool"
    assert last["tool_call_id"] == "call_block_1"
    print("[blocker_seals_its_own_tool_call] OK")


async def test_ask_user_seals_its_own_tool_call() -> None:
    ceo = _new_ceo()
    agent_module.LLM = _AskUserOnTurnZero()
    result = await ceo.run("decide for me")
    assert result.kind == "question", result
    _assert_no_dangling_tool_calls(ceo.messages)
    last = ceo.messages[-1]
    assert last["role"] == "tool"
    assert last["tool_call_id"] == "call_ask_1"
    print("[ask_user_seals_its_own_tool_call] OK")


async def test_complete_seals_trailing_tool_calls_in_same_turn() -> None:
    """Two tool calls in one turn: suggest_follow_up THEN complete. Both
    tool_call_ids must have a matching tool reply; trailing tool calls
    don't exist in this scenario, but verify the seal helper handles the
    multi-call shape correctly."""
    ceo = _new_ceo()
    agent_module.LLM = _CompleteAfterSiblingTool()
    result = await ceo.run("do both")
    assert result.kind == "done", result
    _assert_no_dangling_tool_calls(ceo.messages)
    # Both tool_call_ids should have replies.
    tool_replies = {
        m.get("tool_call_id")
        for m in ceo.messages
        if m.get("role") == "tool"
    }
    assert "call_fu_1" in tool_replies
    assert "call_complete_2" in tool_replies
    print("[complete_seals_trailing_tool_calls_in_same_turn] OK")


# ---------------------------------------------------------------------------
# A2: _safe_path rejects sibling-with-shared-prefix
# ---------------------------------------------------------------------------


def test_safe_path_rejects_sibling_with_shared_prefix(tmp_path: Path) -> None:
    """The classic startswith bug: /tmp/scratch-evil shares a prefix with
    /tmp/scratch. The fix must reject it."""
    base = tmp_path / "scratch"
    base.mkdir()
    sibling = tmp_path / "scratch-evil"
    sibling.mkdir()
    (sibling / "secret.txt").write_text("naughty", encoding="utf-8")

    # Attempt to escape via .. into the sibling.
    try:
        _safe_path(str(base), "../scratch-evil/secret.txt")
    except ValueError as e:
        assert "escapes" in str(e)
        print("[safe_path_rejects_sibling_with_shared_prefix] OK")
        return
    raise AssertionError("expected ValueError but _safe_path accepted the sibling")


def test_safe_path_accepts_valid_child(tmp_path: Path) -> None:
    base = tmp_path / "scratch"
    base.mkdir()
    result = _safe_path(str(base), "subdir/file.txt")
    assert result == (base / "subdir" / "file.txt").resolve()
    print("[safe_path_accepts_valid_child] OK")


def test_safe_path_accepts_base_itself(tmp_path: Path) -> None:
    """An empty rel_path resolves to the base directory itself; this is a
    common idiom and must not be flagged as an escape."""
    base = tmp_path / "scratch"
    base.mkdir()
    result = _safe_path(str(base), ".")
    assert result == base.resolve()
    print("[safe_path_accepts_base_itself] OK")


def test_safe_path_rejects_absolute_escape(tmp_path: Path) -> None:
    """Absolute paths outside base should also be rejected. Path('/etc/passwd')
    fully replaces the relative join."""
    base = tmp_path / "scratch"
    base.mkdir()
    try:
        _safe_path(str(base), "/etc/passwd")
    except ValueError:
        print("[safe_path_rejects_absolute_escape] OK")
        return
    raise AssertionError("expected ValueError for absolute path outside base")


# ---------------------------------------------------------------------------
# A3: agent-supplied markup is escaped in the event log
# ---------------------------------------------------------------------------


def _render_log_panel(tui: TUI) -> str:
    """Render the event log panel to a plain string with ANSI stripped."""
    buf = Console(
        file=io.StringIO(),
        force_terminal=False,
        width=200,
        color_system=None,
        record=True,
    )
    buf.print(tui._build_log_panel())
    return buf.export_text()


def test_agent_name_with_markup_is_escaped_in_log() -> None:
    """An LLM that picks the agent name `[red]EVIL[/]` must NOT have those
    brackets re-interpreted as Rich markup in the event log."""
    tui = TUI(task="t")
    tui._handle_event(
        Event(
            "spawn",
            "a1",
            "[red]EVIL[/]",
            data={"role_type": "worker", "depth": 1},
        )
    )
    out = _render_log_panel(tui)
    # The literal brackets must survive; if Rich had re-parsed them, the
    # output would just be "EVIL" with no brackets visible.
    assert "[red]EVIL[/]" in out, out
    print("[agent_name_with_markup_is_escaped_in_log] OK")


def test_blocker_reason_with_markup_is_escaped_in_log() -> None:
    """A blocker reason containing Rich markup must not break out into
    styling on the log screen."""
    tui = TUI(task="t")
    tui._handle_event(Event("spawn", "a1", "Worker", data={"role_type": "worker", "depth": 1}))
    tui._handle_event(
        Event(
            "blocked",
            "a1",
            "Worker",
            data={
                "reason": "[link=file:///etc/passwd]click me[/link]",
                "needs": "[bold]hack everything[/]",
            },
        )
    )
    out = _render_log_panel(tui)
    # Brackets must be preserved verbatim.
    assert "[link=file:///etc/passwd]click me[/link]" in out, out
    assert "[bold]hack everything[/]" in out, out
    print("[blocker_reason_with_markup_is_escaped_in_log] OK")


def test_tool_args_with_markup_is_escaped_in_log() -> None:
    """Tool arguments are LLM-generated; markup in them must not escape."""
    tui = TUI(task="t")
    tui._handle_event(Event("spawn", "a1", "Worker", data={"role_type": "worker", "depth": 1}))
    tui._handle_event(
        Event(
            "tool_call",
            "a1",
            "Worker",
            data={"tool": "write", "args": {"content": "[red]boom[/]"}},
        )
    )
    out = _render_log_panel(tui)
    assert "[red]boom[/]" in out, out
    print("[tool_args_with_markup_is_escaped_in_log] OK")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def _async_main() -> int:
    await test_complete_seals_its_own_tool_call()
    await test_blocker_seals_its_own_tool_call()
    await test_ask_user_seals_its_own_tool_call()
    await test_complete_seals_trailing_tool_calls_in_same_turn()
    return 0


def main() -> int:
    asyncio.run(_async_main())
    with tempfile.TemporaryDirectory() as d:
        test_safe_path_rejects_sibling_with_shared_prefix(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_safe_path_accepts_valid_child(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_safe_path_accepts_base_itself(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_safe_path_rejects_absolute_escape(Path(d))
    test_agent_name_with_markup_is_escaped_in_log()
    test_blocker_reason_with_markup_is_escaped_in_log()
    test_tool_args_with_markup_is_escaped_in_log()
    print("\nAll PR A regression tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
