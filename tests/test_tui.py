"""Focused tests for the TUI rework:
  - per-agent activity log accumulates instead of overwriting
  - focus panel renders the activity log
  - "thinking Ns…" liveness indicator appears after the threshold
  - Tab / 1 / 2 keys toggle the active screen
  - render() picks the right layout per screen

Run with: python -m tests.test_tui
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rich.console import Console

from taskgenie.events import Event
from taskgenie.tui import (
    SCREEN_LOG,
    SCREEN_MAIN,
    THINKING_THRESHOLD_SECONDS,
    AgentView,
    TUI,
)


def _render_to_string(tui: TUI) -> str:
    """Render the TUI to a plain string so we can grep for content."""
    buf = Console(file=open("/dev/null", "w"), force_terminal=False, width=200)
    with buf.capture() as captured:
        buf.print(tui._render())
    return captured.get()


def test_activity_log_accumulates() -> None:
    """Five narrate events => five activity_log entries (not overwrite)."""
    tui = TUI(task="t")
    tui._handle_event(Event("spawn", "a1", "Worker", data={"role_type": "worker", "depth": 1}))
    for i in range(5):
        tui._handle_event(Event("narrate", "a1", "Worker", data={"text": f"step {i}"}))

    log = tui.agents["a1"].activity_log
    narrates = [e for e in log if e["kind"] == "narrate"]
    assert len(narrates) == 5, f"expected 5 narrates, got {len(narrates)}: {narrates}"
    assert narrates[0]["text"] == "step 0"
    assert narrates[4]["text"] == "step 4"
    # Single-line narration field is still overwritten (used by header etc.)
    assert tui.agents["a1"].narration == "step 4"
    print("[activity_log_accumulates] OK")


def test_activity_log_captures_tools_and_assigns() -> None:
    """Tool calls and assignments both land in the activity log with proper
    kinds, so the focus panel can show ‘what this agent has been doing’."""
    tui = TUI(task="t")
    tui._handle_event(Event("spawn", "a1", "Mgr", data={"role_type": "manager", "depth": 1}))
    tui._handle_event(
        Event("tool_call", "a1", "Mgr", data={"tool": "hire", "args": {"name": "Bob"}})
    )
    tui._handle_event(
        Event("assign", "a1", "Mgr", data={"agent_name": "Bob", "task": "do thing"})
    )
    tui._handle_event(
        Event("review", "a1", "Mgr", data={"accepted": True, "feedback": "nice"})
    )

    kinds = [e["kind"] for e in tui.agents["a1"].activity_log]
    assert kinds == ["tool", "assign", "review"], kinds
    tool_entry = tui.agents["a1"].activity_log[0]
    assert tool_entry["text"] == "hire"
    assert tool_entry["tool_args"] == {"name": "Bob"}
    print("[activity_log_captures_tools_and_assigns] OK")


def test_activity_log_cap() -> None:
    """ACTIVITY_LOG_CAP prevents unbounded memory growth."""
    from taskgenie.tui import ACTIVITY_LOG_CAP

    view = AgentView("x", "X", "worker", 1)
    for i in range(ACTIVITY_LOG_CAP + 50):
        view.push_activity("narrate", f"line {i}")
    assert len(view.activity_log) == ACTIVITY_LOG_CAP
    # Oldest entries got dropped; the latest is preserved.
    assert view.activity_log[-1]["text"] == f"line {ACTIVITY_LOG_CAP + 50 - 1}"
    print("[activity_log_cap] OK")


def test_focus_renders_activity_log() -> None:
    """The focus panel should include each activity entry in its output."""
    tui = TUI(task="t")
    tui._handle_event(Event("spawn", "a1", "Worker", data={"role_type": "worker", "depth": 1}))
    tui._handle_event(Event("state", "a1", "Worker", data={"status": "working", "task": "main task"}))
    tui._handle_event(Event("narrate", "a1", "Worker", data={"text": "first thought"}))
    tui._handle_event(
        Event("tool_call", "a1", "Worker", data={"tool": "write_workspace", "args": {"path": "foo.py"}})
    )
    tui._handle_event(Event("narrate", "a1", "Worker", data={"text": "second thought"}))

    out = _render_to_string(tui)
    # Both narrations and the tool call should appear in the rendered focus.
    assert "first thought" in out, out
    assert "second thought" in out, out
    assert "write_workspace" in out, out
    print("[focus_renders_activity_log] OK")


def test_thinking_indicator_appears_after_threshold() -> None:
    """When an agent is 'working' and hasn't emitted for THINKING_THRESHOLD
    seconds, the focus panel header shows ‘thinking Ns’."""
    tui = TUI(task="t")
    tui._handle_event(Event("spawn", "a1", "Worker", data={"role_type": "worker", "depth": 1}))
    tui._handle_event(Event("state", "a1", "Worker", data={"status": "working"}))
    # Fast-forward last_activity into the past so the silence threshold trips.
    view = tui.agents["a1"]
    import time

    view.last_activity = time.time() - (THINKING_THRESHOLD_SECONDS + 5.0)

    out = _render_to_string(tui)
    assert "thinking" in out, out
    print("[thinking_indicator_appears_after_threshold] OK")


def test_thinking_indicator_absent_when_recent() -> None:
    """Fresh activity => no thinking spinner."""
    tui = TUI(task="t")
    tui._handle_event(Event("spawn", "a1", "Worker", data={"role_type": "worker", "depth": 1}))
    tui._handle_event(Event("state", "a1", "Worker", data={"status": "working"}))
    tui._handle_event(Event("narrate", "a1", "Worker", data={"text": "just spoke"}))

    out = _render_to_string(tui)
    assert "thinking" not in out, out
    print("[thinking_indicator_absent_when_recent] OK")


def test_keys_toggle_screen() -> None:
    """Tab cycles, ‘1’ pins main, ‘2’ pins log. Unknown keys are no-ops."""
    tui = TUI(task="t")
    assert tui._screen == SCREEN_MAIN

    tui._handle_key("\t")
    assert tui._screen == SCREEN_LOG
    tui._handle_key("\t")
    assert tui._screen == SCREEN_MAIN

    tui._handle_key("2")
    assert tui._screen == SCREEN_LOG
    tui._handle_key("1")
    assert tui._screen == SCREEN_MAIN

    # Unknown keys must not change state.
    tui._handle_key("x")
    assert tui._screen == SCREEN_MAIN
    print("[keys_toggle_screen] OK")


def test_screens_render_distinct_content() -> None:
    """Screen 1 contains the hierarchy panel; screen 2 contains the log panel.
    The footer stays present on both."""
    tui = TUI(task="t")
    tui._handle_event(Event("spawn", "a1", "Worker", data={"role_type": "worker", "depth": 1}))
    tui._handle_event(
        Event("tool_call", "a1", "Worker", data={"tool": "edit_workspace", "args": {}})
    )

    tui._screen = SCREEN_MAIN
    main_out = _render_to_string(tui)
    # Hierarchy panel + focus panel headers are both panel titles.
    assert "hierarchy" in main_out
    assert "focus --" in main_out  # panel title is "focus -- <Name>"
    # Footer shown on every screen.
    assert "Tab" in main_out
    # Active panel concept is gone for good.
    assert "(no active agents)" not in main_out

    tui._screen = SCREEN_LOG
    log_out = _render_to_string(tui)
    assert "event log" in log_out
    # On the log screen, the hierarchy + focus PANELS must be gone --
    # check for their panel-title forms (the word "focus" still appears
    # inside the footer hint, which is intentional).
    assert "hierarchy" not in log_out
    assert "focus --" not in log_out
    assert "Tab" in log_out
    print("[screens_render_distinct_content] OK")


def test_setup_input_noop_off_tty() -> None:
    """Tests are not a tty; _setup_input must be a safe no-op so we don't
    accidentally clobber the test runner's terminal state."""
    async def go() -> None:
        tui = TUI(task="t")
        tui._setup_input()
        assert tui._reader_attached is False
        assert tui._termios_old is None
        # Cleanup is a no-op too.
        tui._restore_input()
    asyncio.run(go())
    print("[setup_input_noop_off_tty] OK")


def main() -> int:
    test_activity_log_accumulates()
    test_activity_log_captures_tools_and_assigns()
    test_activity_log_cap()
    test_focus_renders_activity_log()
    test_thinking_indicator_appears_after_threshold()
    test_thinking_indicator_absent_when_recent()
    test_keys_toggle_screen()
    test_screens_render_distinct_content()
    test_setup_input_noop_off_tty()
    print("\nAll TUI tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
