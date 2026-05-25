from __future__ import annotations

import asyncio
from typing import Any

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.text import Text
from rich.tree import Tree

from taskgenie.events import BUS, Event

STATUS_STYLE = {
    "idle": "dim",
    "working": "yellow",
    "done": "green",
    "fired": "red strike",
    "exhausted": "magenta",
    "error": "red",
}


def _trunc(text: str, n: int) -> str:
    text = text.strip().replace("\n", " ")
    return text if len(text) <= n else text[: n - 1] + "…"


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
        self.children_ids: list[str] = []


class TUI:
    def __init__(self, task: str) -> None:
        self.task = task
        self.agents: dict[str, AgentView] = {}
        self.root_id: str | None = None
        self.recent_events: list[str] = []
        self.console = Console()

    def _handle_event(self, event: Event) -> None:
        kind = event.kind
        aid = event.agent_id
        data = event.data

        if kind == "spawn":
            view = AgentView(
                agent_id=aid,
                name=event.agent_name,
                role_type=data.get("role_type", "?"),
                depth=data.get("depth", 0),
            )
            self.agents[aid] = view
            parent_id = data.get("parent_id")
            if parent_id and parent_id in self.agents:
                self.agents[parent_id].children_ids.append(aid)
            else:
                self.root_id = aid
            self._log(f"[dim]spawn[/] {event.agent_name} ({view.role_type})")
            return

        view = self.agents.get(aid)
        if not view:
            return

        if kind == "state":
            view.status = data.get("status", view.status)
            if "task" in data:
                view.task = data["task"]
        elif kind == "narrate":
            view.narration = data.get("text", "")
        elif kind == "tool_call":
            tool = data.get("tool", "")
            args = data.get("args", {}) or {}
            view.last_tool = tool
            view.last_tool_args = args
            self._log(f"[cyan]{view.name}[/] → {tool}({_short_args(args)})")
        elif kind == "tool_result":
            pass
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
                f"[red]{view.name} fired {data.get('agent_name')}[/] — "
                f"{_trunc(data.get('reason', ''), 80)}"
            )
        elif kind == "rehire":
            self._log(f"[yellow]{view.name} rehired[/] with new charter")
        elif kind == "complete":
            view.report = data.get("report", "")
            view.status = "done"
            self._log(f"[green]{view.name} complete[/]")
        elif kind == "error":
            self._log(f"[red]error in {view.name}[/]: {_trunc(data.get('message', ''), 100)}")

    def _log(self, line: str) -> None:
        self.recent_events.append(line)
        if len(self.recent_events) > 18:
            self.recent_events = self.recent_events[-18:]

    def _build_tree(self) -> Tree:
        title = Text("TaskGenie", style="bold cyan")
        title.append("  task: ", style="dim")
        title.append(_trunc(self.task, 100), style="italic")
        tree = Tree(title)
        if self.root_id:
            self._add_node(tree, self.root_id)
        return tree

    def _add_node(self, parent_node: Tree, agent_id: str) -> None:
        v = self.agents[agent_id]
        style = STATUS_STYLE.get(v.status, "white")

        label = Text()
        label.append("● ", style=style)
        label.append(v.name, style="bold")
        label.append(f"  [{v.role_type}/{v.status}]", style="dim")

        sub = Text()
        if v.narration:
            sub.append("\n  ", style="dim")
            sub.append('“', style="italic dim")
            sub.append(_trunc(v.narration, 220), style="italic")
            sub.append('”', style="italic dim")
        if v.last_tool:
            sub.append("\n  → ", style="dim")
            sub.append(v.last_tool, style="cyan")
            sub.append("(", style="dim")
            sub.append(_short_args(v.last_tool_args), style="dim")
            sub.append(")", style="dim")
        if v.report and v.status == "done":
            sub.append("\n  ⮑ ", style="green dim")
            sub.append(_trunc(v.report, 220), style="green")

        node = parent_node.add(label + sub)
        for cid in v.children_ids:
            self._add_node(node, cid)

    def _render(self) -> Group:
        tree = self._build_tree()
        log_text = Text("\n".join(self.recent_events) if self.recent_events else "(no events yet)")
        log_panel = Panel(log_text, title="event log", border_style="dim")
        return Group(tree, log_panel)

    async def run(self, stop_when: asyncio.Task[Any]) -> None:
        q = BUS.subscribe()
        with Live(self._render(), console=self.console, refresh_per_second=8, screen=False) as live:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=0.25)
                    self._handle_event(event)
                except asyncio.TimeoutError:
                    pass
                live.update(self._render())
                if stop_when.done():
                    while not q.empty():
                        self._handle_event(q.get_nowait())
                    live.update(self._render())
                    break


def _short_args(args: dict[str, Any]) -> str:
    parts = []
    for k, v in args.items():
        sv = str(v)
        if len(sv) > 40:
            sv = sv[:37] + "…"
        parts.append(f"{k}={sv}")
    return ", ".join(parts)
