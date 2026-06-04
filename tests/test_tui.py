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
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rich.console import Console

from rich.table import Table
from rich.tree import Tree

from taskgenie.events import Event
from taskgenie.tui import (
    SCREEN_HELP,
    SCREEN_LOG,
    SCREEN_MAIN,
    SCREEN_STORY,
    THINKING_THRESHOLD_SECONDS,
    AgentView,
    TUI,
    _ColumnOverflowTree,
)


def _render_to_string(tui: TUI) -> str:
    """Render the TUI to a plain string so we can grep for content."""
    # io.StringIO instead of open("/dev/null") so the test works on
    # Windows runners where /dev/null doesn't exist.
    buf = Console(file=io.StringIO(), force_terminal=False, width=200)
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
    """Tab cycles main->log->story, number keys pin, ‘?’/’h’ toggles help.
    Unknown keys are no-ops."""
    tui = TUI(task="t")
    assert tui._screen == SCREEN_MAIN

    # Tab cycles: main -> log -> story -> main
    tui._handle_key("\t")
    assert tui._screen == SCREEN_LOG
    tui._handle_key("\t")
    assert tui._screen == SCREEN_STORY
    tui._handle_key("\t")
    assert tui._screen == SCREEN_MAIN

    # Number keys pin to specific screens.
    tui._handle_key("2")
    assert tui._screen == SCREEN_LOG
    tui._handle_key("1")
    assert tui._screen == SCREEN_MAIN
    tui._handle_key("3")
    assert tui._screen == SCREEN_STORY
    tui._handle_key("1")
    assert tui._screen == SCREEN_MAIN

    # Help screen toggle with ‘?’.
    tui._handle_key("?")
    assert tui._screen == SCREEN_HELP
    tui._handle_key("?")
    assert tui._screen == SCREEN_MAIN

    # Help screen toggle with ‘h’.
    tui._handle_key("h")
    assert tui._screen == SCREEN_HELP
    tui._handle_key("h")
    assert tui._screen == SCREEN_MAIN

    # Tab from help goes back to main (help not in tab cycle).
    tui._handle_key("h")
    assert tui._screen == SCREEN_HELP
    tui._handle_key("\t")
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
    assert "[3] story" in main_out or "story" in main_out
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
    assert "story" in log_out
    print("[screens_render_distinct_content] OK")


def test_overflow_tree_passes_through_when_fits() -> None:
    """Short trees should render exactly the same as before — the wrapper
    is a no-op when the tree fits in the assigned height."""
    tree = Tree("Root")
    for i in range(3):
        tree.add(f"line {i}")
    overflow = _ColumnOverflowTree(tree)

    console = Console(width=120, height=30, force_terminal=True)
    opts = console.options.update(width=80, height=20)
    result = list(overflow.__rich_console__(console, opts))
    # Exactly one yielded renderable, and it's the original tree object —
    # no extra column-flow machinery in the single-column case.
    assert len(result) == 1
    assert result[0] is tree
    print("[overflow_tree_passes_through_when_fits] OK")


def test_overflow_tree_spills_into_columns() -> None:
    """Tall trees overflow into Table.grid columns — the rendered output
    must be a Table (not the bare tree) and must fit the assigned height."""
    tree = Tree("Root")
    for i in range(60):  # way more than the 10-row budget
        tree.add(f"line {i}")
    overflow = _ColumnOverflowTree(tree)

    console = Console(width=200, height=12, force_terminal=True)
    opts = console.options.update(width=150, height=10)
    result = list(overflow.__rich_console__(console, opts))
    assert len(result) == 1
    assert isinstance(result[0], Table), (
        f"expected Table.grid for overflow path, got {type(result[0])}"
    )

    # Render the table and verify entries from BOTH ends of the source
    # tree are visible — proving the tree actually wrapped into columns
    # rather than just truncating.
    buf = Console(width=150, height=10, force_terminal=False)
    with buf.capture() as captured:
        buf.print(result[0])
    out = captured.get()
    assert "line 0" in out, f"first entry missing from columned tree:\n{out}"
    # Something further down the tree should also be present in a later
    # column (we have 60 lines into ~10 rows of height across multiple cols).
    assert "line 20" in out or "line 30" in out, (
        f"deeper entries missing — overflow didn't spill into columns:\n{out}"
    )
    print("[overflow_tree_spills_into_columns] OK")


def test_overflow_tree_no_height_yields_tree() -> None:
    """If options.height is None/zero (initial measure pass), the wrapper
    must not crash and should fall back to the bare tree."""
    tree = Tree("Root")
    for i in range(50):
        tree.add(f"line {i}")
    overflow = _ColumnOverflowTree(tree)

    console = Console(width=100, force_terminal=True)
    opts = console.options.update(width=80, height=None)
    result = list(overflow.__rich_console__(console, opts))
    assert len(result) == 1
    assert result[0] is tree
    print("[overflow_tree_no_height_yields_tree] OK")


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


def test_cycle_focus() -> None:
    """j/k keys cycle focus_id through the agent list."""
    tui = TUI(task="t")
    tui._handle_event(Event("spawn", "a1", "CEO", data={"role_type": "ceo", "depth": 0}))
    tui._handle_event(Event("spawn", "a2", "Mgr", data={"role_type": "manager", "depth": 1, "parent_id": "a1"}))
    tui._handle_event(Event("spawn", "a3", "Worker", data={"role_type": "worker", "depth": 2, "parent_id": "a2"}))

    # focus_id should be on the last spawned agent
    assert tui.focus_id == "a3"

    # 'j' moves forward
    tui._handle_key("j")
    ids = list(tui.agents.keys())
    expected_idx = (ids.index("a3") + 1) % len(ids)
    assert tui.focus_id == ids[expected_idx]

    # 'k' moves backward
    prev_id = tui.focus_id
    tui._handle_key("k")
    assert tui.focus_id == "a3"

    # Cycling wraps around
    for _ in range(len(tui.agents)):
        tui._handle_key("j")
    # After a full cycle we're back where we started
    assert tui.focus_id == "a3"
    print("[cycle_focus] OK")


def test_cycle_focus_empty() -> None:
    """Cycling with no agents is a safe no-op."""
    tui = TUI(task="t")
    tui._handle_key("j")
    assert tui.focus_id is None
    tui._handle_key("k")
    assert tui.focus_id is None
    print("[cycle_focus_empty] OK")


def test_toggle_fired() -> None:
    """'f' key toggles show_fired flag."""
    tui = TUI(task="t")
    assert tui.show_fired is True
    tui._handle_key("f")
    assert tui.show_fired is False
    tui._handle_key("f")
    assert tui.show_fired is True
    print("[toggle_fired] OK")


def test_fired_agents_hidden_in_tree() -> None:
    """When show_fired is False, fired agents should not appear in the tree."""
    tui = TUI(task="t")
    tui._handle_event(Event("spawn", "a1", "CEO", data={"role_type": "ceo", "depth": 0}))
    tui._handle_event(Event("spawn", "a2", "Worker1", data={"role_type": "worker", "depth": 1, "parent_id": "a1"}))
    tui._handle_event(Event("spawn", "a3", "Worker2", data={"role_type": "worker", "depth": 1, "parent_id": "a1"}))
    tui._handle_event(Event("state", "a2", "Worker1", data={"status": "fired"}))

    # Focus on a non-fired agent so the focus panel doesn't show the fired one.
    tui.focus_id = "a3"

    # With show_fired=True, fired worker is visible in the tree
    tui.show_fired = True
    import io
    from rich.console import Console as _C
    buf = _C(file=io.StringIO(), force_terminal=False, width=200)
    with buf.capture() as cap:
        buf.print(tui._build_tree())
    tree_shown = cap.get()
    assert "Worker1" in tree_shown

    # With show_fired=False, fired worker is hidden from the tree
    tui.show_fired = False
    buf2 = _C(file=io.StringIO(), force_terminal=False, width=200)
    with buf2.capture() as cap2:
        buf2.print(tui._build_tree())
    tree_hidden = cap2.get()
    assert "Worker1" not in tree_hidden
    assert "Worker2" in tree_hidden
    print("[fired_agents_hidden_in_tree] OK")


def test_help_screen_renders() -> None:
    """The help screen should contain shortcut descriptions."""
    tui = TUI(task="t")
    tui._screen = SCREEN_HELP
    out = _render_to_string(tui)
    assert "keyboard shortcuts" in out
    assert "Toggle this help screen" in out
    assert "Story mode" in out
    assert "fired agents" in out.lower()
    assert "Ctrl+C" in out
    print("[help_screen_renders] OK")


def test_story_screen_renders() -> None:
    """Story mode screen should render even with no narration entries."""
    tui = TUI(task="t")
    tui._screen = SCREEN_STORY
    out = _render_to_string(tui)
    assert "story mode" in out
    assert "warming up" in out

    # With a narration entry, it should render the text.
    from taskgenie.narrator import NarrationEntry
    tui.narrator.entries.append(
        NarrationEntry(text="The CEO has hired a manager. How original.")
    )
    out2 = _render_to_string(tui)
    assert "How original" in out2
    print("[story_screen_renders] OK")


def main() -> int:
    test_activity_log_accumulates()
    test_activity_log_captures_tools_and_assigns()
    test_activity_log_cap()
    test_focus_renders_activity_log()
    test_thinking_indicator_appears_after_threshold()
    test_thinking_indicator_absent_when_recent()
    test_keys_toggle_screen()
    test_screens_render_distinct_content()
    test_overflow_tree_passes_through_when_fits()
    test_overflow_tree_spills_into_columns()
    test_overflow_tree_no_height_yields_tree()
    test_setup_input_noop_off_tty()
    test_cycle_focus()
    test_cycle_focus_empty()
    test_toggle_fired()
    test_fired_agents_hidden_in_tree()
    test_help_screen_renders()
    test_story_screen_renders()
    print("\nAll TUI tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
