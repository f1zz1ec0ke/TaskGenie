from __future__ import annotations

import asyncio
import time
from typing import Any

from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.text import Text
from rich.tree import Tree

from taskgenie.events import BUS, Event

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


class TUI:
    def __init__(self, task: str) -> None:
        self.task = task
        self.agents: dict[str, AgentView] = {}
        self.root_id: str | None = None
        self.recent_events: list[str] = []
        self.focus_id: str | None = None
        self.console = Console(force_terminal=True, color_system="auto")

    # ---- event handling ----------------------------------------------------

    def _touch(self, view: AgentView) -> None:
        view.last_activity = time.time()
        self.focus_id = view.id

    def _handle_event(self, event: Event) -> None:
        kind = event.kind
        aid = event.agent_id
        data = event.data

        if kind == "spawn":
            if aid in self.agents:
                # Already loaded via rehydrate — skip duplicate spawn from replay.
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
            self._log(f"[dim]spawn[/] {event.agent_name} ({view.role_type})")
            self.focus_id = aid
            return

        view = self.agents.get(aid)
        if not view:
            return

        if kind == "state":
            view.status = data.get("status", view.status)
            if "task" in data:
                view.task = data["task"]
            self._touch(view)
        elif kind == "narrate":
            view.narration = data.get("text", "")
            self._touch(view)
        elif kind == "tool_call":
            tool = data.get("tool", "")
            args = data.get("args", {}) or {}
            view.last_tool = tool
            view.last_tool_args = args
            self._log(f"[cyan]{view.name}[/] -> {tool}({_short_args(args)})")
            self._touch(view)
        elif kind == "hire":
            self._log(
                f"[green]{view.name}[/] hired [bold]{data.get('child_name')}[/] "
                f"as {data.get('role_type')}"
            )
        elif kind == "assign":
            self._log(
                f"[blue]{view.name}[/] assigned task to "
                f"[bold]{data.get('agent_name')}[/]: {_trunc(data.get('task', ''), 80)}"
            )
        elif kind == "review":
            verdict = "ACCEPTED" if data.get("accepted") else "REJECTED"
            style = "green" if data.get("accepted") else "red"
            self._log(
                f"[{style}]{view.name} review {verdict}[/]: "
                f"{_trunc(data.get('feedback', ''), 100)}"
            )
        elif kind == "fire":
            self._log(
                f"[red]{view.name} fired {data.get('agent_name')}[/] -- "
                f"{_trunc(data.get('reason', ''), 80)}"
            )
        elif kind == "rehire":
            self._log(f"[yellow]{view.name} rehired[/] with new charter")
        elif kind == "complete":
            view.report = data.get("report", "")
            view.status = "done"
            self._log(f"[green]{view.name} complete[/]")
            self._touch(view)
        elif kind == "follow_up_suggested":
            view.follow_ups.append(
                {
                    "what": data.get("what", ""),
                    "rationale": data.get("rationale", ""),
                    "who": data.get("who", ""),
                }
            )
            self._log(
                f"[magenta]{view.name} ↑ follow-up[/]: "
                f"{_trunc(data.get('what', ''), 80)}"
            )
            self._touch(view)
        elif kind == "blocked":
            view.blocker_reason = data.get("reason", "")
            view.blocker_needs = data.get("needs", "")
            view.partial_progress = data.get("partial_progress", "")
            view.status = "blocked"
            self._log(
                f"[orange1]{view.name} BLOCKED[/] -- "
                f"{_trunc(view.blocker_reason, 70)} "
                f"[dim](needs: {_trunc(view.blocker_needs, 50)})[/]"
            )
            self._touch(view)
        elif kind == "question":
            view.question = data.get("question", "")
            view.question_options = list(data.get("options", []) or [])
            view.question_context = data.get("context", "")
            view.status = "asking"
            self._log(
                f"[cyan]{view.name} ASKING USER[/]: "
                f"{_trunc(view.question, 90)}"
            )
            self._touch(view)
        elif kind == "error":
            self._log(f"[red]error in {view.name}[/]: {_trunc(data.get('message', ''), 100)}")

    def _log(self, line: str) -> None:
        self.recent_events.append(line)
        if len(self.recent_events) > 30:
            self.recent_events = self.recent_events[-30:]

    # ---- rendering ---------------------------------------------------------

    def _agent_label(self, view: AgentView, now: float) -> Text:
        style = STATUS_STYLE.get(view.status, "white")
        label = Text()
        if view.status == "working":
            label.append(_spinner_char(now) + " ", style=style)
        else:
            label.append("● ", style=style)

        # Spawn flash: pulse the name in bold cyan for a moment after spawn.
        elapsed_since_spawn = now - view.spawn_time
        if elapsed_since_spawn < SPAWN_FLASH_SECONDS:
            # decay from bold bright cyan to normal
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
        v = self.agents[agent_id]
        node = parent_node.add(self._agent_label(v, now))
        for cid in v.children_ids:
            self._add_node(node, cid, now)

    def _agent_action_summary(self, v: "AgentView") -> Text:
        """One-line current-action summary for the active panel."""
        text = Text()
        if v.status == "blocked":
            text.append("⚠ ", style="orange1 bold")
            text.append(_trunc(v.blocker_reason or "blocked", 30), style="orange1")
        elif v.status == "asking":
            text.append("? ", style="cyan bold")
            text.append(_trunc(v.question or "asking user", 30), style="cyan")
        elif v.last_tool:
            text.append(v.last_tool, style="cyan bold")
            if v.last_tool_args:
                short = _short_args(v.last_tool_args)
                text.append(f"({_trunc(short, 28)})", style="dim")
        elif v.narration:
            text.append(f"“{_trunc(v.narration, 34)}”", style="italic dim")
        else:
            text.append("(starting up)", style="dim")
        return text

    def _build_active_panel(self) -> Panel:
        """All currently-active (not idle/done/fired) agents in one place.

        Sorts deepest-first so workers (who actually do visible work) lead
        instead of being drowned out by orchestrating managers.
        """
        now = time.time()
        active = [
            v for v in self.agents.values()
            if v.status in ("working", "blocked", "asking", "exhausted")
        ]
        # Count summary for the title.
        counts: dict[str, int] = {}
        for v in active:
            counts[v.status] = counts.get(v.status, 0) + 1
        title_bits = [f"{counts[k]}{k[0]}" for k in ("working", "blocked", "asking", "exhausted") if counts.get(k)]
        title = "active" + (f" ({' '.join(title_bits)})" if title_bits else "")

        if not active:
            return Panel(
                Text("(no active agents)", style="dim italic"),
                title=title,
                border_style="dim",
            )

        active.sort(key=lambda v: (-v.depth, -v.last_activity))
        cap = 14
        lines: list[Any] = []
        for v in active[:cap]:
            style = STATUS_STYLE.get(v.status, "white")
            line = Text()
            if v.status == "working":
                line.append(_spinner_char(now) + " ", style=style)
            else:
                line.append("● ", style=style)
            role_tag = {"ceo": "C", "manager": "M", "worker": "W"}.get(v.role_type, "?")
            line.append(f"[{role_tag}] ", style="dim")
            indent = "  " * min(v.depth, 4)
            line.append(f"{indent}{v.name}", style="bold")
            line.append("  ", style="dim")
            line.append_text(self._agent_action_summary(v))
            lines.append(line)
        if len(active) > cap:
            lines.append(
                Text(f"  …and {len(active) - cap} more active", style="dim italic")
            )

        return Panel(Group(*lines), title=title, border_style="yellow")

    def _build_focus(self) -> Panel:
        if not self.focus_id or self.focus_id not in self.agents:
            return Panel(
                Text("(no agent has acted yet)", style="dim italic"),
                title="focus",
                border_style="dim",
            )
        v = self.agents[self.focus_id]
        style = STATUS_STYLE.get(v.status, "white")

        header = Text()
        header.append(v.name, style="bold")
        header.append(f"  {v.role_type} / ", style="dim")
        header.append(v.status, style=style)

        body_parts: list[Any] = [header, Text("")]

        if v.task:
            body_parts.append(Text("task:", style="dim"))
            body_parts.append(Text(_trunc(v.task, 600), style="italic"))
            body_parts.append(Text(""))

        if v.narration:
            body_parts.append(Text("saying:", style="dim"))
            body_parts.append(Text(v.narration, style="italic"))
            body_parts.append(Text(""))

        if v.last_tool:
            tool_line = Text()
            tool_line.append("tool: ", style="dim")
            tool_line.append(v.last_tool, style="cyan bold")
            tool_line.append(f"({_short_args(v.last_tool_args)})", style="dim")
            body_parts.append(tool_line)
            body_parts.append(Text(""))

        if v.status == "blocked":
            body_parts.append(Text("BLOCKER", style="bold orange1"))
            body_parts.append(Text(f"reason: {v.blocker_reason}", style="orange1"))
            body_parts.append(Text(f"needs:  {v.blocker_needs}", style="orange1"))
            if v.partial_progress:
                body_parts.append(Text(f"partial: {v.partial_progress}", style="orange1 dim"))

        if v.status == "asking" or v.question:
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

        if v.status == "done" and v.report:
            body_parts.append(Text("report:", style="green dim"))
            body_parts.append(Text(_trunc(v.report, 800), style="green"))

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

    def _build_log(self) -> Panel:
        if not self.recent_events:
            body = Text("(waiting for events)", style="dim italic")
        else:
            body = Text("\n".join(self.recent_events[-10:]))
        return Panel(body, title="event log", border_style="dim")

    def _render(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="main", ratio=3),
            Layout(name="log", size=10),
        )
        layout["main"].split_row(
            Layout(name="tree", ratio=3),
            Layout(name="right", ratio=3),
        )
        layout["right"].split_column(
            Layout(name="active", ratio=2),
            Layout(name="focus", ratio=2),
        )
        layout["tree"].update(
            Panel(self._build_tree(), border_style="cyan", title="hierarchy")
        )
        layout["active"].update(self._build_active_panel())
        layout["focus"].update(self._build_focus())
        layout["log"].update(self._build_log())
        return layout

    # ---- main loop ---------------------------------------------------------

    async def run(self, stop_when: asyncio.Task[Any]) -> None:
        q = BUS.subscribe()
        # Alt-screen mode prevents flicker when the layout outgrows the
        # terminal height; fall back to inline mode when stdout isn't a tty
        # (tests, CI capture, piped output).
        use_screen = bool(getattr(self.console, "is_terminal", False))
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
                    live.update(self._render())
                    if stop_when.done():
                        while not q.empty():
                            self._handle_event(q.get_nowait())
                        live.update(self._render())
                        break
        finally:
            BUS.unsubscribe(q)
