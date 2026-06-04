from __future__ import annotations

import asyncio
import sys
import time
from typing import Any

from rich.cells import cell_len, set_cell_size
from rich.console import Console, ConsoleOptions, Group, RenderResult
from rich.layout import Layout
from rich.live import Live
from rich.markup import escape as _md_escape
from rich.measure import Measurement
from rich.panel import Panel
from rich.segment import Segment
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from taskgenie.events import BUS, Event
from taskgenie.narrator import StoryNarrator
from taskgenie.utils import fmt_count, fmt_duration

STATUS_STYLE = {
    "idle": "dim",
    "working": "yellow",
    "done": "green",
    "blocked": "bold orange1",
    "asking": "bold cyan",
    "fired": "red strike",
    "exhausted": "magenta",
    "error": "red",
}

SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
SPAWN_FLASH_SECONDS = 1.2
ACTIVITY_LOG_CAP = 200
ACTIVITY_DISPLAY_CAP = 40
THINKING_THRESHOLD_SECONDS = 2.0

SCREEN_MAIN = 1
SCREEN_LOG = 2
SCREEN_STORY = 3
SCREEN_HELP = 4


def _trunc(text: str, n: int) -> str:
    text = text.strip().replace("\n", " ")
    return text if len(text) <= n else text[: n - 1] + "…"


def _spinner_char(now: float | None = None) -> str:
    t = now if now is not None else time.time()
    return SPINNER_FRAMES[int(t * 10) % len(SPINNER_FRAMES)]


def _short_args(args: dict[str, Any]) -> str:
    parts = []
    for k, v in args.items():
        sv = str(v)
        if len(sv) > 40:
            sv = sv[:37] + "…"
        parts.append(f"{k}={sv}")
    return ", ".join(parts)


_ACTIVITY_TRIM_CHARS = 200


def _trim_for_activity(value: Any) -> Any:
    """Recursively clip string leaves so a single fat tool_args dict can't
    hold MBs of content alive in `AgentView.activity_log`.

    Preserves structure (keys, list ordering) so the rendered display in
    _format_activity_entry still gets the same shape it always did.
    """
    if isinstance(value, str):
        if len(value) > _ACTIVITY_TRIM_CHARS:
            return value[:_ACTIVITY_TRIM_CHARS] + "…(truncated)"
        return value
    if isinstance(value, dict):
        return {k: _trim_for_activity(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_trim_for_activity(v) for v in value]
    return value


class AgentView:
    def __init__(self, agent_id: str, name: str, role_type: str, depth: int) -> None:
        self.id = agent_id
        self.name = name
        self.role_type = role_type
        self.depth = depth
        self.status = "idle"
        self.narration: str = ""
        self.last_tool: str = ""
        self.last_tool_args: dict[str, Any] = {}
        self.task: str = ""
        self.report: str = ""
        self.blocker_reason: str = ""
        self.blocker_needs: str = ""
        self.partial_progress: str = ""
        self.follow_ups: list[dict[str, str]] = []
        self.question: str = ""
        self.question_options: list[str] = []
        self.question_context: str = ""
        self.children_ids: list[str] = []
        self.spawn_time: float = time.time()
        self.last_activity: float = time.time()
        # Per-agent activity log: each entry {ts, kind, text, tool_args}.
        # Accumulates over the agent's whole life so a worker doing 5 LLM
        # turns appears as 5 entries rather than one ephemeral overwritten
        # narration line. Capped to ACTIVITY_LOG_CAP.
        self.activity_log: list[dict[str, Any]] = []
        # Running compaction telemetry mirroring agent.compaction_stats.
        # Updated by compaction_end events so the focus panel can render a
        # summary line ("compacted 3x, saved 12.4k tokens") rather than
        # making the user count log entries.
        self.compaction_count: int = 0
        self.compaction_skipped: int = 0
        self.compaction_failed: int = 0
        self.compaction_tokens_saved: int = 0
        self.compaction_messages_compacted: int = 0
        self.compaction_duration_ms: float = 0.0

    def push_activity(self, kind: str, text: str, **extra: Any) -> None:
        entry: dict[str, Any] = {"ts": time.time(), "kind": kind, "text": text}
        if extra:
            entry.update(extra)
        self.activity_log.append(entry)
        if len(self.activity_log) > ACTIVITY_LOG_CAP:
            self.activity_log = self.activity_log[-ACTIVITY_LOG_CAP:]


def _truncate_segments(seg_list: list[Segment], max_width: int) -> list[Segment]:
    """Cut a list of Segments down to `max_width` display columns.

    Uses Rich's cell_len so fullwidth/CJK characters are counted as 2.
    The final segment may be partially sliced; styles are preserved.
    """
    out: list[Segment] = []
    width = 0
    for seg in seg_list:
        if width >= max_width:
            break
        text = seg.text
        if text == "\n" or not text:
            continue
        seg_w = cell_len(text)
        if seg_w == 0:
            continue
        if width + seg_w <= max_width:
            out.append(seg)
            width += seg_w
        else:
            remaining = max_width - width
            out.append(Segment(set_cell_size(text, remaining), seg.style))
            width = max_width
            break
    return out


def _count_tree_nodes(tree: Tree, cap: int) -> int:
    """Count up to `cap` nodes in `tree` (root + all descendants).
    Short-circuits once cap is hit so the count for huge trees stays
    bounded. Used as a cheap O(min(N, cap)) pre-check to skip the
    expensive wide-render when the tree obviously fits in the panel.
    """
    count = 1  # the root itself contributes a line
    stack: list[Tree] = list(getattr(tree, "children", []) or [])
    while stack and count < cap:
        node = stack.pop()
        count += 1
        kids = getattr(node, "children", None)
        if kids:
            stack.extend(kids)
    return count


class _ColumnOverflowTree:
    """Tree wrapper that flows into more columns when it would overflow.

    The tree renders as one logical thing — when the rendered height
    exceeds the panel's assigned height, the overflow lines wrap into a
    second column to the right, and a third if needed, etc. Same
    indentation prefixes carry across the column break, so column 2's
    first line might be `│   ├── ● Foo` referencing parents that live in
    column 1 — the same visual you'd see scrolling through a deep tree.

    When the tree fits, we yield it directly so single-column rendering
    is identical to the pre-overflow behaviour.
    """

    MIN_COL_WIDTH = 38  # Below this, even short worker labels look bad.

    def __init__(self, tree: Tree) -> None:
        self._tree = tree

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        avail_h = options.height
        avail_w = options.max_width
        if avail_h is None or avail_h <= 0 or avail_w <= 0:
            yield self._tree
            return

        # Cheap pre-check: a Tree's logical line count is bounded by
        # 1 + (descendants). At ~80% of avail_h or fewer total nodes,
        # there's no plausible way the rendered output overflows --
        # skip the (somewhat expensive) wide-render and yield directly.
        # Stops us paying ~4ms/tick at 8 fps for the common small-tree
        # case (CEO + a handful of managers + workers).
        max_safe_nodes = max(1, int(avail_h * 0.8))
        if _count_tree_nodes(self._tree, max_safe_nodes + 1) <= max_safe_nodes:
            yield self._tree
            return

        # Render once at a wide width AND uncapped height so we measure
        # the tree's full extent. render_lines() crops to options.height
        # otherwise, which would hide the very overflow we want to detect.
        # The wide width must exceed any plausible single-label cell
        # count -- if a label wraps during the wide-render, our
        # line-count overflow detection mis-counts. 200 was too tight
        # for trees with very long agent names (300+ chars from a
        # role_type/status suffix on top of the name); 4096 covers any
        # realistic case without meaningfully changing render cost.
        wide_opts = options.update(
            width=max(avail_w, 4096), height=None
        )
        wide_lines = console.render_lines(self._tree, wide_opts, pad=False)
        # Drop trailing blank lines Rich sometimes adds.
        while wide_lines and not any(s.text.strip() for s in wide_lines[-1]):
            wide_lines.pop()

        if len(wide_lines) <= avail_h:
            yield self._tree
            return

        cols_needed = (len(wide_lines) + avail_h - 1) // avail_h
        max_cols = max(1, avail_w // self.MIN_COL_WIDTH)
        cols = min(cols_needed, max_cols)
        if cols <= 1:
            yield self._tree  # Even one column won't fit — let it crop.
            return

        col_w = avail_w // cols
        trunc_lines = [_truncate_segments(line, col_w) for line in wide_lines]

        grid = Table.grid(padding=0)
        cells: list[Text] = []
        for i in range(cols):
            chunk = trunc_lines[i * avail_h : (i + 1) * avail_h]
            if not chunk:
                break
            grid.add_column(width=col_w, overflow="crop", no_wrap=True)
            cell_text = Text(overflow="crop", no_wrap=True)
            for line_idx, seg_list in enumerate(chunk):
                if line_idx > 0:
                    cell_text.append("\n")
                for seg in seg_list:
                    cell_text.append(seg.text, style=seg.style or "")
            cells.append(cell_text)
        grid.add_row(*cells)
        yield grid

    def __rich_measure__(
        self, console: Console, options: ConsoleOptions
    ) -> Measurement:
        # Tell Panel we'll happily use the full inner width. Without this,
        # measure would fall through to Tree.__rich_measure__ which can
        # report a very wide preferred width and make Panel mis-size.
        # Clamp min to max so very narrow terminals (max_width < MIN_COL_WIDTH)
        # don't produce an invalid min>max Measurement.
        return Measurement(
            min(self.MIN_COL_WIDTH, options.max_width),
            options.max_width,
        )


class TUI:
    def __init__(self, task: str) -> None:
        self.task = task
        self.agents: dict[str, AgentView] = {}
        self.root_id: str | None = None
        self.recent_events: list[str] = []
        self.focus_id: str | None = None
        self.console = Console(force_terminal=True, color_system="auto")
        self.llm_calls: int = 0
        self.prompt_tokens: int = 0
        self.completion_tokens: int = 0
        self.tool_calls_total: int = 0
        self.start_time: float = time.time()
        self._screen: int = SCREEN_MAIN
        self.show_fired: bool = True
        self.narrator: StoryNarrator = StoryNarrator()
        # Keyboard input state. Set in _setup_input(), cleaned by _restore_input().
        self._termios_old: Any = None
        self._stdin_fd: int | None = None
        self._reader_attached: bool = False
        self._input_loop: asyncio.AbstractEventLoop | None = None

    # ---- event handling ----------------------------------------------------

    def _touch(self, view: AgentView) -> None:
        view.last_activity = time.time()
        self.focus_id = view.id

    def _snapshot_initial_agents(self) -> None:
        """Populate self.agents from Agent.REGISTRY at startup.

        The CEO is created BEFORE tui.run() is scheduled, so its spawn
        event fires before any TUI subscriber exists -- the BUS doesn't
        buffer for late subscribers, so the event is lost. The first
        manager that subsequently spawns has parent_id=CEO.id which the
        TUI doesn't recognise, so it gets set as root_id one after
        another, hiding the CEO entirely.

        Also fires on resume, where Agent.bulk_load re-emits spawn
        events for every persisted agent before tui.run() exists.

        Two passes (instantiate views, then wire parent/child links) so
        ordering of REGISTRY iteration doesn't matter.
        """
        from taskgenie.agent import Agent as _Agent

        for aid, agent in _Agent.REGISTRY.items():
            if aid in self.agents:
                continue
            view = AgentView(
                agent_id=aid,
                name=agent.name,
                role_type=agent.role_type,
                depth=agent.depth,
            )
            view.status = agent.status
            view.blocker_reason = getattr(agent, "blocker_reason", "")
            view.blocker_needs = getattr(agent, "blocker_needs", "")
            view.follow_ups = list(getattr(agent, "follow_ups", []) or [])
            self.agents[aid] = view

        for aid, agent in _Agent.REGISTRY.items():
            view = self.agents.get(aid)
            if view is None:
                continue
            parent = getattr(agent, "parent", None)
            if parent is None:
                self.root_id = aid
            elif parent.id in self.agents:
                if aid not in self.agents[parent.id].children_ids:
                    self.agents[parent.id].children_ids.append(aid)

    def _handle_event(self, event: Event) -> None:
        kind = event.kind
        aid = event.agent_id
        data = event.data

        if kind == "spawn":
            if aid in self.agents:
                return
            view = AgentView(
                agent_id=aid,
                name=event.agent_name,
                role_type=data.get("role_type", "?"),
                depth=data.get("depth", 0),
            )
            self.agents[aid] = view
            parent_id = data.get("parent_id")
            if parent_id and parent_id in self.agents:
                if aid not in self.agents[parent_id].children_ids:
                    self.agents[parent_id].children_ids.append(aid)
            else:
                self.root_id = aid
            self._log(
                f"[dim]spawn[/] {_md_escape(event.agent_name)} "
                f"({_md_escape(view.role_type)})"
            )
            self.focus_id = aid
            return

        view = self.agents.get(aid)
        if not view:
            return

        if kind == "state":
            view.status = data.get("status", view.status)
            if "task" in data:
                view.task = data["task"]
                view.push_activity("task", _trunc(data["task"], 200))
            self._touch(view)
        elif kind == "narrate":
            text = data.get("text", "")
            # Empty narrations would still reset last_activity and defeat
            # the "thinking N s" indicator. Skip entirely so a silent
            # working agent still looks alive in the focus panel.
            if not text:
                return
            view.narration = text
            view.push_activity("narrate", text)
            self._touch(view)
        elif kind == "tool_call":
            tool = data.get("tool", "")
            args = data.get("args", {}) or {}
            view.last_tool = tool
            view.last_tool_args = args
            self.tool_calls_total += 1
            # Truncate string leaves anywhere in tool_args at push time.
            # The display path uses _short_args (40-char cap) anyway, so
            # the full bytes were dead weight -- and with up to 200
            # entries per agent, an active worker doing many large
            # write_workspace calls held megabytes of redundant content
            # per AgentView. Recurses into nested dicts/lists so
            # write_workspace.content (or any future nested-dict tool
            # arg) doesn't slip past the trim.
            trimmed_args = _trim_for_activity(args)
            view.push_activity("tool", tool, tool_args=trimmed_args)
            self._log(
                f"[cyan]{_md_escape(view.name)}[/] -> "
                f"{_md_escape(tool)}({_md_escape(_short_args(args))})"
            )
            self._touch(view)
        elif kind == "llm_call":
            self.llm_calls += 1
            self.prompt_tokens += int(data.get("prompt_tokens", 0) or 0)
            self.completion_tokens += int(data.get("completion_tokens", 0) or 0)
        elif kind == "hire":
            view.push_activity(
                "hire",
                f"hired {data.get('child_name')} as {data.get('role_type')}",
            )
            self._log(
                f"[green]{_md_escape(view.name)}[/] hired "
                f"[bold]{_md_escape(data.get('child_name', ''))}[/] "
                f"as {_md_escape(data.get('role_type', ''))}"
            )
        elif kind == "assign":
            view.push_activity(
                "assign",
                f"-> {data.get('agent_name')}: {_trunc(data.get('task', ''), 120)}",
            )
            self._log(
                f"[blue]{_md_escape(view.name)}[/] assigned task to "
                f"[bold]{_md_escape(data.get('agent_name', ''))}[/]: "
                f"{_md_escape(_trunc(data.get('task', ''), 80))}"
            )
        elif kind == "review":
            verdict = "ACCEPTED" if data.get("accepted") else "REJECTED"
            view.push_activity(
                "review",
                f"{verdict}: {_trunc(data.get('feedback', ''), 140)}",
            )
            style = "green" if data.get("accepted") else "red"
            self._log(
                f"[{style}]{_md_escape(view.name)} review {verdict}[/]: "
                f"{_md_escape(_trunc(data.get('feedback', ''), 100))}"
            )
        elif kind == "fire":
            view.push_activity(
                "fire",
                f"fired {data.get('agent_name')}: {_trunc(data.get('reason', ''), 120)}",
            )
            self._log(
                f"[red]{_md_escape(view.name)} fired "
                f"{_md_escape(data.get('agent_name', ''))}[/] -- "
                f"{_md_escape(_trunc(data.get('reason', ''), 80))}"
            )
        elif kind == "rehire":
            view.push_activity("rehire", "rehired with new charter")
            self._log(f"[yellow]{_md_escape(view.name)} rehired[/] with new charter")
        elif kind == "complete":
            view.report = data.get("report", "")
            view.status = "done"
            view.push_activity("complete", _trunc(data.get("report", ""), 200))
            self._log(f"[green]{_md_escape(view.name)} complete[/]")
            self._touch(view)
        elif kind == "follow_up_suggested":
            entry = {
                "what": data.get("what", ""),
                "rationale": data.get("rationale", ""),
                "who": data.get("who", ""),
            }
            view.follow_ups.append(entry)
            view.push_activity(
                "follow_up",
                f"↑ {_trunc(entry['what'], 140)}",
            )
            self._log(
                f"[magenta]{_md_escape(view.name)} ↑ follow-up[/]: "
                f"{_md_escape(_trunc(data.get('what', ''), 80))}"
            )
            self._touch(view)
        elif kind == "blocked":
            view.blocker_reason = data.get("reason", "")
            view.blocker_needs = data.get("needs", "")
            view.partial_progress = data.get("partial_progress", "")
            view.status = "blocked"
            view.push_activity(
                "blocked",
                f"reason: {_trunc(view.blocker_reason, 100)} (needs: {_trunc(view.blocker_needs, 60)})",
            )
            self._log(
                f"[orange1]{_md_escape(view.name)} BLOCKED[/] -- "
                f"{_md_escape(_trunc(view.blocker_reason, 70))} "
                f"[dim](needs: {_md_escape(_trunc(view.blocker_needs, 50))})[/]"
            )
            self._touch(view)
        elif kind == "question":
            view.question = data.get("question", "")
            view.question_options = list(data.get("options", []) or [])
            view.question_context = data.get("context", "")
            view.status = "asking"
            view.push_activity("question", _trunc(view.question, 200))
            self._log(
                f"[cyan]{_md_escape(view.name)} ASKING USER[/]: "
                f"{_md_escape(_trunc(view.question, 90))}"
            )
            self._touch(view)
        elif kind == "error":
            view.push_activity("error", _trunc(data.get("message", ""), 200))
            self._log(
                f"[red]error in {_md_escape(view.name)}[/]: "
                f"{_md_escape(_trunc(data.get('message', ''), 100))}"
            )
        elif kind == "compaction_start":
            before = data.get("before_messages", 0)
            before_tok = data.get("before_tokens", 0)
            view.push_activity(
                "compaction",
                f"archivist: compacting {before} messages "
                f"({fmt_count(before_tok)} tokens)",
            )
            self._log(
                f"[blue]{_md_escape(view.name)}[/] archivist compacting "
                f"{before} messages"
            )
        elif kind == "compaction_end":
            duration_ms = float(data.get("duration_ms", 0) or 0)
            view.compaction_duration_ms += duration_ms
            dur_s = duration_ms / 1000.0
            if data.get("success"):
                before = data.get("before_messages", 0)
                after = data.get("after_messages", 0)
                before_tok = data.get("before_tokens", 0)
                after_tok = data.get("after_tokens", 0)
                view.compaction_count += 1
                view.compaction_messages_compacted += max(0, before - after)
                view.compaction_tokens_saved += max(0, before_tok - after_tok)
                view.push_activity(
                    "compaction",
                    f"archivist: compacted {before}→{after} messages "
                    f"({fmt_count(before_tok)}→{fmt_count(after_tok)} tokens, "
                    f"{dur_s:.1f}s)",
                )
                self._log(
                    f"[green]{_md_escape(view.name)}[/] archivist compacted "
                    f"{before}→{after} messages"
                )
            else:
                # Agent sends `reason` (R76); fall back to `error` for
                # older event-log entries.
                reason = data.get("reason") or data.get("error") or "no detail"
                # Distinguish structural skip vs transient failure from
                # the reason prefix (compact_messages tags them).
                if str(reason).startswith("transient:"):
                    view.compaction_failed += 1
                else:
                    view.compaction_skipped += 1
                view.push_activity(
                    "compaction",
                    f"archivist: compaction skipped ({dur_s:.1f}s): "
                    f"{_trunc(str(reason), 100)}",
                )
                self._log(
                    f"[yellow]{_md_escape(view.name)} archivist skipped[/]: "
                    f"{_md_escape(_trunc(str(reason), 80))}"
                )

    def _log(self, line: str) -> None:
        self.recent_events.append(line)
        # The log screen has a whole panel now -- keep more history than
        # the old quarter-panel ever could hold.
        if len(self.recent_events) > 500:
            self.recent_events = self.recent_events[-500:]

    # ---- input handling ----------------------------------------------------

    def _setup_input(self) -> None:
        """Wire stdin to feed _handle_key without blocking.

        Unix: termios cbreak + loop.add_reader. cbreak leaves ISIG on,
        so Ctrl+C still raises KeyboardInterrupt as expected.
        Windows: nothing here; the render tick polls msvcrt.kbhit().
        Not a tty (tests, piped, CI): no-op; keybindings become inert.
        """
        if not sys.stdin.isatty():
            return
        if sys.platform == "win32":
            return
        try:
            import termios
            import tty

            self._stdin_fd = sys.stdin.fileno()
            self._termios_old = termios.tcgetattr(self._stdin_fd)
            tty.setcbreak(self._stdin_fd)
            self._input_loop = asyncio.get_running_loop()
            self._input_loop.add_reader(self._stdin_fd, self._on_stdin_unix)
            self._reader_attached = True
        except Exception:
            # If anything goes wrong (no real tty, locked-down env),
            # don't crash the TUI -- just lose keybindings for this run.
            self._termios_old = None
            self._stdin_fd = None
            self._reader_attached = False

    def _restore_input(self) -> None:
        if (
            self._reader_attached
            and self._stdin_fd is not None
            and self._input_loop is not None
        ):
            try:
                self._input_loop.remove_reader(self._stdin_fd)
            except Exception:
                pass
            self._reader_attached = False
        if self._termios_old is not None and self._stdin_fd is not None:
            try:
                import termios

                termios.tcsetattr(self._stdin_fd, termios.TCSADRAIN, self._termios_old)
            except Exception:
                pass
            self._termios_old = None

    def _on_stdin_unix(self) -> None:
        try:
            ch = sys.stdin.read(1)
        except (OSError, ValueError):
            return
        if ch:
            self._handle_key(ch)

    def _poll_keys_windows(self) -> None:
        if sys.platform != "win32" or not sys.stdin.isatty():
            return
        try:
            import msvcrt
        except ImportError:
            return
        while msvcrt.kbhit():
            ch = msvcrt.getch()
            # Extended scancodes (function/arrow keys) come as two bytes;
            # drop the second so it doesn't get reinterpreted as ASCII.
            if ch in (b"\x00", b"\xe0"):
                msvcrt.getch()
                continue
            try:
                self._handle_key(ch.decode("utf-8", errors="ignore"))
            except UnicodeDecodeError:
                pass

    def _handle_key(self, ch: str) -> None:
        if ch == "\t":
            cycle = [SCREEN_MAIN, SCREEN_LOG, SCREEN_STORY]
            try:
                idx = cycle.index(self._screen)
                self._screen = cycle[(idx + 1) % len(cycle)]
            except ValueError:
                self._screen = SCREEN_MAIN
        elif ch == "1":
            self._screen = SCREEN_MAIN
        elif ch == "2":
            self._screen = SCREEN_LOG
        elif ch == "3":
            self._screen = SCREEN_STORY
        elif ch in ("?", "h"):
            self._screen = SCREEN_HELP if self._screen != SCREEN_HELP else SCREEN_MAIN
        elif ch == "j":
            self._cycle_focus(1)
        elif ch == "k":
            self._cycle_focus(-1)
        elif ch == "f":
            self._toggle_fired()

    def _cycle_focus(self, direction: int) -> None:
        """Cycle focus to the next/previous agent in the hierarchy."""
        if not self.agents:
            return
        ids = list(self.agents.keys())
        if not self.focus_id or self.focus_id not in self.agents:
            self.focus_id = ids[0]
            return
        idx = ids.index(self.focus_id)
        idx = (idx + direction) % len(ids)
        self.focus_id = ids[idx]

    def _toggle_fired(self) -> None:
        """Toggle visibility of fired agents in the tree."""
        self.show_fired = not self.show_fired

    # ---- rendering ---------------------------------------------------------

    def _agent_label(self, view: AgentView, now: float) -> Text:
        style = STATUS_STYLE.get(view.status, "white")
        label = Text()
        if view.status == "working":
            label.append(_spinner_char(now) + " ", style=style)
        else:
            label.append("● ", style=style)

        elapsed_since_spawn = now - view.spawn_time
        if elapsed_since_spawn < SPAWN_FLASH_SECONDS:
            label.append(view.name, style="bold bright_cyan")
            label.append(" [NEW]", style="bright_cyan dim")
        else:
            label.append(view.name, style="bold")

        label.append(f"  {view.role_type}/", style="dim")
        label.append(view.status, style=style)
        if view.follow_ups:
            label.append(f"  +{len(view.follow_ups)}", style="magenta")
        return label

    def _build_tree(self) -> Tree:
        now = time.time()
        title = Text()
        title.append("TaskGenie", style="bold cyan")
        title.append("  ", style="dim")
        title.append(_trunc(self.task, 100), style="italic")
        tree = Tree(title)
        if self.root_id:
            self._add_node(tree, self.root_id, now)
        return tree

    def _add_node(self, parent_node: Tree, agent_id: str, now: float) -> None:
        """Render the management skeleton + a spotlight worker per manager."""
        v = self.agents[agent_id]
        label = self._agent_label(v, now)

        known_children = [
            self.agents[cid] for cid in v.children_ids if cid in self.agents
        ]
        if not known_children:
            parent_node.add(label)
            return

        managers = [
            c for c in known_children
            if c.role_type in ("manager", "ceo")
            and (self.show_fired or c.status != "fired")
        ]
        workers = [
            c for c in known_children
            if c.role_type == "worker"
            and (self.show_fired or c.status != "fired")
        ]

        active_managers = [c for c in managers if c.status not in ("done", "fired")]
        done_managers = [c for c in managers if c.status in ("done", "fired")]
        active_workers = [c for c in workers if c.status not in ("done", "fired")]
        done_workers = [c for c in workers if c.status in ("done", "fired")]
        blocked_workers = [c for c in workers if c.status in ("blocked", "asking")]
        working_workers = [c for c in workers if c.status == "working"]

        if v.status == "done" and not active_managers and not active_workers:
            collapsed = Text()
            collapsed.append_text(label)
            total_done = len(done_managers) + len(done_workers)
            if total_done:
                collapsed.append(f"  ({total_done} done)", style="green dim")
            parent_node.add(collapsed)
            return

        if workers:
            label.append("  ")
            label.append("[", style="dim")
            if active_workers:
                label.append(f"{len(active_workers)}↻", style="yellow")
            if blocked_workers:
                if active_workers:
                    label.append(" ", style="dim")
                label.append(f"{len(blocked_workers)}!", style="orange1")
            if done_workers:
                if active_workers or blocked_workers:
                    label.append(" ", style="dim")
                label.append(f"{len(done_workers)}✓", style="green")
            label.append(" workers]", style="dim")

        node = parent_node.add(label)

        spotlight_id: str | None = None
        if working_workers:
            spotlight_id = max(working_workers, key=lambda w: w.last_activity).id

        ordered_workers: list[AgentView] = []
        if spotlight_id is not None:
            ordered_workers.append(self.agents[spotlight_id])
            ordered_workers.extend(w for w in active_workers if w.id != spotlight_id)
        else:
            ordered_workers.extend(active_workers)
        ordered_workers.extend(done_workers)

        worker_cap = 10
        for w in ordered_workers[:worker_cap]:
            wline = Text()
            wstyle = STATUS_STYLE.get(w.status, "white")
            if w.status == "working":
                wline.append(_spinner_char(now) + " ", style=wstyle)
            else:
                wline.append("● ", style=wstyle)
            wline.append(w.name, style="bold")
            if w.id == spotlight_id:
                activity = w.narration or w.last_tool or ""
                if activity:
                    wline.append("  ")
                    wline.append(_trunc(activity, 60), style="italic dim")
            elif w.status == "working" and w.last_tool:
                wline.append(f"  {w.last_tool}", style="cyan dim")
            else:
                wline.append(f"  {w.status}", style=f"{wstyle} dim")
            node.add(wline)
        worker_overflow = len(ordered_workers) - worker_cap
        if worker_overflow > 0:
            node.add(
                Text(
                    f"…+{worker_overflow} more workers",
                    style="dim italic",
                )
            )

        cap = 10
        ordered_managers = active_managers + done_managers
        for child in ordered_managers[:cap]:
            self._add_node(node, child.id, now)
        hidden = len(ordered_managers) - min(cap, len(ordered_managers))
        if hidden > 0:
            node.add(
                Text(f"…+{hidden} more managers", style="dim italic")
            )

    def _format_activity_entry(self, entry: dict[str, Any]) -> Text:
        """One row of an agent's activity log. Kind drives color + prefix."""
        kind = entry.get("kind", "")
        text = entry.get("text", "")
        line = Text()
        prefix_style = {
            "narrate": ("dim", "say"),
            "tool": ("cyan", "tool"),
            "task": ("blue", "task"),
            "hire": ("green", "hire"),
            "assign": ("blue", "assign"),
            "review": ("magenta", "review"),
            "fire": ("red", "fire"),
            "rehire": ("yellow", "rehire"),
            "complete": ("green", "done"),
            "follow_up": ("magenta", "↑"),
            "blocked": ("orange1", "blocked"),
            "question": ("cyan", "ask"),
            "error": ("red", "error"),
            "compaction": ("blue", "ctx"),
        }
        style, label = prefix_style.get(kind, ("white", kind or "·"))
        # Right-justify using cell width (CJK / fullwidth chars count as 2)
        # rather than character count, so the column stays aligned when
        # labels include arrows / fullwidth glyphs (e.g. "↑" for follow_up
        # has cell width 1 in most terminals; future labels with cell
        # width 2 would otherwise drift the column).
        label_pad = max(0, 7 - cell_len(label))
        line.append((" " * label_pad) + label + " ", style=f"{style} dim")
        if kind == "tool":
            line.append(text, style=f"{style} bold")
            tool_args = entry.get("tool_args") or {}
            if tool_args:
                line.append(f"({_short_args(tool_args)})", style="dim")
        elif kind == "narrate":
            line.append(f"“{text}”", style="italic")
        else:
            line.append(text, style=style)
        return line

    def _build_help(self) -> Panel:
        help_text = Text()
        shortcuts = [
            ("Tab", "Cycle between focus+tree, event log, story mode"),
            ("1", "Focus + tree view"),
            ("2", "Event log view"),
            ("3", "Story mode (sarcastic narrator)"),
            ("? / h", "Toggle this help screen"),
            ("j / k", "Cycle focus to next/previous agent"),
            ("f", "Toggle fired agents visibility"),
            ("Ctrl+C", "Save state and exit"),
        ]
        for key, desc in shortcuts:
            help_text.append(f"  {key:>10s}", style="bold cyan")
            help_text.append(f"  {desc}\n")
        return Panel(help_text, title="keyboard shortcuts", border_style="cyan")

    def _build_story(self) -> Panel:
        entries = self.narrator.entries
        if not entries:
            body = Text(
                "(the narrator is warming up — give it a moment...)",
                style="dim italic",
            )
            return Panel(body, title="story mode", border_style="magenta")
        parts: list[Text] = []
        for entry in reversed(entries[-15:]):
            age = time.time() - entry.ts
            if age < 60:
                ago = f"{int(age)}s ago"
            elif age < 3600:
                ago = f"{int(age / 60)}m ago"
            else:
                ago = f"{int(age / 3600)}h ago"
            line = Text()
            line.append(f"  {ago:>8s}  ", style="dim")
            line.append(entry.text, style="italic")
            parts.append(line)
            parts.append(Text(""))
        return Panel(
            Group(*parts),
            title="story mode",
            subtitle="[dim]narration updates every ~20s[/]",
            border_style="magenta",
        )

    def _build_focus(self) -> Panel:
        if not self.focus_id or self.focus_id not in self.agents:
            return Panel(
                Text("(no agent has acted yet)", style="dim italic"),
                title="focus",
                border_style="dim",
            )
        v = self.agents[self.focus_id]
        style = STATUS_STYLE.get(v.status, "white")
        now = time.time()

        header = Text()
        header.append(v.name, style="bold")
        header.append(f"  {v.role_type} / ", style="dim")
        header.append(v.status, style=style)
        # Liveness: if the agent is "working" but hasn't emitted anything
        # for a while, the LLM is mid-call. Show a thinking timer so a
        # silent worker still looks alive.
        if v.status == "working":
            silent_for = now - v.last_activity
            if silent_for >= THINKING_THRESHOLD_SECONDS:
                header.append(
                    f"  · thinking {int(silent_for)}s {_spinner_char(now)}",
                    style="yellow italic",
                )

        body_parts: list[Any] = [header]

        if v.task:
            body_parts.append(Text(""))
            body_parts.append(Text("task:", style="dim"))
            body_parts.append(Text(_trunc(v.task, 600), style="italic"))

        if v.status == "blocked":
            body_parts.append(Text(""))
            body_parts.append(Text("BLOCKER", style="bold orange1"))
            body_parts.append(Text(f"reason: {v.blocker_reason}", style="orange1"))
            body_parts.append(Text(f"needs:  {v.blocker_needs}", style="orange1"))
            if v.partial_progress:
                body_parts.append(Text(f"partial: {v.partial_progress}", style="orange1 dim"))

        if v.status == "asking" or v.question:
            body_parts.append(Text(""))
            body_parts.append(Text("QUESTION FOR USER", style="bold cyan"))
            body_parts.append(Text(v.question, style="cyan"))
            if v.question_context:
                body_parts.append(
                    Text(f"context: {_trunc(v.question_context, 400)}", style="cyan dim")
                )
            if v.question_options:
                body_parts.append(Text("options:", style="cyan dim"))
                for i, opt in enumerate(v.question_options, 1):
                    body_parts.append(Text(f"  {i}. {opt}", style="cyan"))

        # Activity log: the actual "watch it grow" surface. Latest at the
        # top so the most recent thing this agent did stays visible even
        # when the panel runs out of vertical space.
        if v.activity_log:
            body_parts.append(Text(""))
            body_parts.append(Text(f"activity ({len(v.activity_log)}):", style="dim"))
            for entry in reversed(v.activity_log[-ACTIVITY_DISPLAY_CAP:]):
                body_parts.append(self._format_activity_entry(entry))

        # Compaction summary -- shown when this agent had to compact at
        # least once. Lets the operator see "this worker burned 3
        # archivist calls" without scrolling activity log for compaction
        # entries.
        if (
            v.compaction_count
            or v.compaction_skipped
            or v.compaction_failed
        ):
            body_parts.append(Text(""))
            line = Text()
            line.append("context compaction: ", style="dim")
            line.append(f"{v.compaction_count}", style="blue bold")
            line.append(" ok", style="blue dim")
            if v.compaction_skipped:
                line.append(f"  {v.compaction_skipped}", style="yellow bold")
                line.append(" skipped", style="yellow dim")
            if v.compaction_failed:
                line.append(f"  {v.compaction_failed}", style="red bold")
                line.append(" failed", style="red dim")
            if v.compaction_tokens_saved:
                line.append(
                    f"  · saved {fmt_count(v.compaction_tokens_saved)} tokens",
                    style="dim",
                )
            if v.compaction_duration_ms:
                line.append(
                    f"  · {v.compaction_duration_ms / 1000.0:.1f}s archivist time",
                    style="dim",
                )
            body_parts.append(line)

        if v.follow_ups:
            body_parts.append(Text(""))
            body_parts.append(
                Text(f"follow-ups suggested ({len(v.follow_ups)}):", style="magenta dim")
            )
            for i, fu in enumerate(v.follow_ups[:5], 1):
                line = Text()
                line.append(f"  {i}. ", style="magenta dim")
                line.append(_trunc(fu.get("what", ""), 90), style="magenta")
                if fu.get("rationale"):
                    line.append(f" -- {_trunc(fu['rationale'], 60)}", style="magenta dim")
                body_parts.append(line)
            if len(v.follow_ups) > 5:
                body_parts.append(
                    Text(f"  ... and {len(v.follow_ups) - 5} more", style="magenta dim")
                )

        return Panel(
            Group(*body_parts),
            title=f"focus -- {v.name}",
            border_style=style,
        )

    def _build_header(self) -> Panel:
        counts: dict[str, int] = {}
        for v in self.agents.values():
            counts[v.status] = counts.get(v.status, 0) + 1
        total = len(self.agents)
        elapsed = time.time() - self.start_time

        line = Text()
        line.append(_trunc(self.task, 60), style="italic")
        line.append("   ")
        line.append("agents ", style="dim")
        line.append(f"{total}", style="bold")
        parts: list[tuple[str, str]] = []
        if counts.get("working"):
            parts.append((str(counts["working"]) + "w", "yellow"))
        if counts.get("done"):
            parts.append((str(counts["done"]) + "d", "green"))
        if counts.get("blocked"):
            parts.append((str(counts["blocked"]) + "b", "orange1"))
        if counts.get("asking"):
            parts.append((str(counts["asking"]) + "a", "cyan"))
        if counts.get("exhausted"):
            parts.append((str(counts["exhausted"]) + "x", "magenta"))
        if counts.get("fired"):
            parts.append((str(counts["fired"]) + "f", "red"))
        if parts:
            line.append(" (", style="dim")
            for i, (text, style) in enumerate(parts):
                if i:
                    line.append(" ", style="dim")
                line.append(text, style=style)
            line.append(")", style="dim")
        line.append("   ·   ", style="dim")
        line.append("LLM ", style="dim")
        line.append(f"{self.llm_calls}", style="bold")
        line.append(" calls   ·   ", style="dim")
        line.append("tok ", style="dim")
        line.append(f"{fmt_count(self.prompt_tokens)}↑", style="bold")
        line.append("/", style="dim")
        line.append(f"{fmt_count(self.completion_tokens)}↓", style="bold")
        line.append("   ·   ", style="dim")
        line.append("tools ", style="dim")
        line.append(f"{self.tool_calls_total}", style="bold")
        line.append("   ·   ", style="dim")
        line.append("elapsed ", style="dim")
        line.append(fmt_duration(elapsed), style="bold")
        return Panel(line, border_style="cyan", title="TaskGenie")

    def _build_log_panel(self, *, title_suffix: str = "") -> Panel:
        if not self.recent_events:
            body: Text = Text("(waiting for events)", style="dim italic")
        else:
            body = Text.from_markup("\n".join(self.recent_events))
        title = "event log"
        if title_suffix:
            title = f"{title} {title_suffix}"
        return Panel(body, title=title, border_style="dim")

    def _build_footer(self) -> Text:
        line = Text()
        main_style = "reverse bold" if self._screen == SCREEN_MAIN else "dim"
        log_style = "reverse bold" if self._screen == SCREEN_LOG else "dim"
        story_style = "reverse bold" if self._screen == SCREEN_STORY else "dim"
        help_style = "reverse bold" if self._screen == SCREEN_HELP else "dim"
        line.append(" [1] focus + tree ", style=main_style)
        line.append("  ", style="dim")
        line.append(" [2] event log ", style=log_style)
        line.append("  ", style="dim")
        line.append(" [3] story ", style=story_style)
        line.append("  ", style="dim")
        line.append(" [?] help ", style=help_style)
        line.append("   j/k  f  ^C", style="dim")
        return line

    def _render(self) -> Layout:
        """Header strip + body + footer. Body switches with the active screen."""
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="body", ratio=1),
            Layout(name="footer", size=1),
        )
        layout["header"].update(self._build_header())
        layout["footer"].update(self._build_footer())

        if self._screen == SCREEN_HELP:
            layout["body"].update(self._build_help())
        elif self._screen == SCREEN_STORY:
            layout["body"].update(self._build_story())
        elif self._screen == SCREEN_LOG:
            layout["body"].update(
                self._build_log_panel(title_suffix=f"({len(self.recent_events)})")
            )
        else:
            # Narrow terminals: a 50/50 split_row gives each side ~25 cols,
            # which collapses the tree column-overflow logic AND wraps the
            # focus labels into noise. Stack vertically when the terminal
            # can't comfortably support side-by-side panels.
            term_width = self.console.size.width
            if term_width < 70:
                layout["body"].split_column(
                    Layout(name="focus", ratio=2),
                    Layout(name="tree", ratio=1),
                )
            else:
                layout["body"].split_row(
                    Layout(name="focus", ratio=1),
                    Layout(name="tree", ratio=1),
                )
            layout["body"]["focus"].update(self._build_focus())
            layout["body"]["tree"].update(
                Panel(
                    _ColumnOverflowTree(self._build_tree()),
                    border_style="cyan",
                    title="hierarchy",
                )
            )
        return layout

    # ---- main loop ---------------------------------------------------------

    async def run(self, stop_when: asyncio.Task[Any]) -> None:
        self._snapshot_initial_agents()
        q = BUS.subscribe()
        await self.narrator.start()
        use_screen = sys.stdout.isatty() and bool(
            getattr(self.console, "is_terminal", False)
        )
        self._setup_input()
        try:
            with Live(
                self._render(),
                console=self.console,
                refresh_per_second=8,
                screen=use_screen,
                redirect_stdout=False,
                redirect_stderr=False,
                vertical_overflow="crop",
            ) as live:
                while True:
                    try:
                        event = await asyncio.wait_for(q.get(), timeout=0.1)
                        self._handle_event(event)
                    except asyncio.TimeoutError:
                        pass
                    # Windows: stdin can't be add_reader'd, so we poll
                    # once per render tick. Unix already fed any
                    # keypresses via the reader callback.
                    self._poll_keys_windows()
                    live.update(self._render())
                    if stop_when.done():
                        while not q.empty():
                            self._handle_event(q.get_nowait())
                        live.update(self._render())
                        break
        finally:
            self._restore_input()
            await self.narrator.stop()
            BUS.unsubscribe(q)
