"""Deterministic stats over a session's event log.

Used to render the post-run summary panel — a meta-debugging view that
complements the CEO's qualitative `Process retrospective` section in the
final report.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from taskgenie.event_logger import read_events


@dataclass
class AgentSummary:
    name: str
    role_type: str
    turns: int = 0  # number of assistant messages = narrate events
    llm_calls: int = 0  # actual LLM round-trips this agent made
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tool_calls: Counter[str] = field(default_factory=Counter)
    status: str = "?"
    blocker: dict[str, str] | None = None
    follow_ups_raised: int = 0


@dataclass
class RunStats:
    agents: dict[str, AgentSummary]
    hires: int
    fires: int
    rehires: int
    blockers_total: int
    blockers_escalated_to_user: int  # CEO blockers
    blockers_resolved_internally: int  # blockers that didn't propagate further
    reviews_accepted: int
    reviews_rejected: int
    tool_call_total: int
    llm_calls_total: int
    prompt_tokens_total: int
    completion_tokens_total: int
    questions_asked: int  # CEO -> user mid-task questions
    follow_ups_total: int
    duration_seconds: float | None
    started_at: str | None
    ended_at: str | None


def _parse_ts(ts: Any) -> datetime | None:
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(ts)
        except (OverflowError, ValueError):
            return None
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts)
        except ValueError:
            return None
    return None


def compute(events_path: Path, ceo_id: str | None = None) -> RunStats:
    events = read_events(events_path)
    agents: dict[str, AgentSummary] = {}
    hires = fires = rehires = 0
    blockers_total = blockers_escalated_to_user = 0
    reviews_accepted = reviews_rejected = 0
    tool_call_total = 0
    llm_calls_total = 0
    prompt_tokens_total = 0
    completion_tokens_total = 0
    questions_asked = 0
    follow_ups_total = 0
    first_ts: datetime | None = None
    last_ts: datetime | None = None

    for ev in events:
        kind = ev.get("kind")
        aid = ev.get("agent_id")
        data = ev.get("data") or {}
        ts = _parse_ts(ev.get("ts"))
        if ts:
            if first_ts is None or ts < first_ts:
                first_ts = ts
            if last_ts is None or ts > last_ts:
                last_ts = ts

        # bulk_load re-emits spawn/state/blocked events tagged with
        # `replay=True` on resume so the live TUI can rebuild its tree.
        # Persistent counters must NOT see them as fresh events --
        # otherwise blockers_total/escalated_to_user double-count on
        # every resume, and the per-agent AgentSummary would have been
        # re-initialised (the first-spawn-wins guard below handles spawn
        # specifically but `blocked` had its own counter to mis-bump).
        if data.get("replay"):
            continue

        if kind == "spawn" and aid:
            # Even with the `replay` filter above catching round-3 sessions,
            # keep this first-spawn-wins guard for older event logs that
            # don't tag replays.
            if aid not in agents:
                agents[aid] = AgentSummary(
                    name=ev.get("agent_name") or aid,
                    role_type=data.get("role_type", "?"),
                )
            continue

        agent = agents.get(aid)
        if not agent:
            continue

        if kind == "state":
            status = data.get("status")
            if status:
                agent.status = status
        elif kind == "narrate":
            agent.turns += 1
        elif kind == "tool_call":
            tool = data.get("tool", "?")
            agent.tool_calls[tool] += 1
            tool_call_total += 1
        elif kind == "llm_call":
            agent.llm_calls += 1
            llm_calls_total += 1
            pt = int(data.get("prompt_tokens", 0) or 0)
            ct = int(data.get("completion_tokens", 0) or 0)
            agent.prompt_tokens += pt
            agent.completion_tokens += ct
            prompt_tokens_total += pt
            completion_tokens_total += ct
        elif kind == "follow_up_suggested":
            agent.follow_ups_raised += 1
            follow_ups_total += 1
        elif kind == "question":
            # The CEO is the only role with ask_user, but counting per-agent
            # falls out for free in case that ever changes.
            questions_asked += 1
        elif kind == "hire":
            hires += 1
        elif kind == "fire":
            fires += 1
        elif kind == "rehire":
            rehires += 1
        elif kind == "review":
            if data.get("accepted"):
                reviews_accepted += 1
            else:
                reviews_rejected += 1
        elif kind == "blocked":
            blockers_total += 1
            agent.blocker = {
                "reason": data.get("reason", ""),
                "needs": data.get("needs", ""),
            }
            if ceo_id and aid == ceo_id:
                blockers_escalated_to_user += 1

    blockers_resolved_internally = max(0, blockers_total - blockers_escalated_to_user)

    duration = (
        (last_ts - first_ts).total_seconds()
        if first_ts and last_ts
        else None
    )

    return RunStats(
        agents=agents,
        hires=hires,
        fires=fires,
        rehires=rehires,
        blockers_total=blockers_total,
        blockers_escalated_to_user=blockers_escalated_to_user,
        blockers_resolved_internally=blockers_resolved_internally,
        reviews_accepted=reviews_accepted,
        reviews_rejected=reviews_rejected,
        tool_call_total=tool_call_total,
        llm_calls_total=llm_calls_total,
        prompt_tokens_total=prompt_tokens_total,
        completion_tokens_total=completion_tokens_total,
        questions_asked=questions_asked,
        follow_ups_total=follow_ups_total,
        duration_seconds=duration,
        started_at=first_ts.isoformat(timespec="seconds") if first_ts else None,
        ended_at=last_ts.isoformat(timespec="seconds") if last_ts else None,
    )


def _fmt_count(n: int) -> str:
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k"
    return f"{n / 1_000_000:.1f}M"


def render_panel(stats: RunStats):
    """Build a Rich renderable summarising the run."""
    from rich.console import Group
    from rich.panel import Panel
    from rich.table import Table

    role_counts: Counter[str] = Counter(a.role_type for a in stats.agents.values())
    role_summary = ", ".join(f"{n} {r}" for r, n in sorted(role_counts.items()))

    overview = Table.grid(padding=(0, 2))
    overview.add_column(style="dim")
    overview.add_column()
    overview.add_row("Agents", f"{len(stats.agents)} total — {role_summary or '(none)'}")
    overview.add_row("Hires / fires / rehires", f"{stats.hires} / {stats.fires} / {stats.rehires}")
    overview.add_row(
        "Blockers",
        f"{stats.blockers_total} total "
        f"({stats.blockers_resolved_internally} resolved by managers, "
        f"{stats.blockers_escalated_to_user} escalated to user)",
    )
    overview.add_row(
        "Reviews",
        f"{stats.reviews_accepted} accepted, {stats.reviews_rejected} rejected",
    )
    overview.add_row(
        "User questions",
        f"{stats.questions_asked} asked",
    )
    overview.add_row(
        "Follow-ups raised",
        f"{stats.follow_ups_total} total",
    )
    overview.add_row("Tool calls", str(stats.tool_call_total))
    overview.add_row(
        "LLM calls",
        f"{stats.llm_calls_total} calls — "
        f"{_fmt_count(stats.prompt_tokens_total)} prompt / "
        f"{_fmt_count(stats.completion_tokens_total)} completion tokens",
    )
    if stats.duration_seconds is not None:
        overview.add_row("Wall time", f"{stats.duration_seconds:.1f}s")

    per_agent = Table(show_header=True, header_style="bold", expand=False)
    per_agent.add_column("agent", style="cyan")
    per_agent.add_column("role")
    per_agent.add_column("status")
    per_agent.add_column("turns", justify="right")
    per_agent.add_column("LLM", justify="right")
    per_agent.add_column("tok ↑/↓", justify="right")
    per_agent.add_column("top tools", overflow="fold")
    per_agent.add_column("blocker", overflow="fold")
    status_style = {
        "done": "green",
        "blocked": "yellow",
        "working": "yellow",
        "fired": "red strike",
        "error": "red",
        "exhausted": "magenta",
    }
    for agent in sorted(stats.agents.values(), key=lambda a: (a.role_type, a.name)):
        style = status_style.get(agent.status, "white")
        top_tools = ", ".join(
            f"{tool}×{count}" for tool, count in agent.tool_calls.most_common(4)
        ) or "—"
        blocker_text = ""
        if agent.blocker:
            blocker_text = f"{agent.blocker.get('needs', '')[:60]}".strip()
        per_agent.add_row(
            agent.name,
            agent.role_type,
            f"[{style}]{agent.status}[/]",
            str(agent.turns),
            str(agent.llm_calls),
            f"{_fmt_count(agent.prompt_tokens)}/{_fmt_count(agent.completion_tokens)}",
            top_tools,
            blocker_text or "—",
        )

    return Panel(
        Group(overview, per_agent),
        title="Run summary",
        border_style="cyan",
    )
