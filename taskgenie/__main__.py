from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import asdict

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table

from taskgenie.agent import Agent
from taskgenie.config import CONFIG
from taskgenie.mcp_client import MCPClient
from taskgenie.prompts import CEO_ROLE_PROMPT
from taskgenie.result import RunResult
from taskgenie.session import Session
from taskgenie.tools.registry import REGISTRY
from taskgenie.tui import TUI

ABANDON_TOKENS = {"", "quit", "exit", "abandon", "stop"}


def _print_banner(console: Console, session: Session, mcp: MCPClient) -> None:
    lines = [
        f"[bold]Session:[/] {session.session_id}",
        f"[bold]Endpoint:[/] {CONFIG.base_url}",
        f"[bold]Model:[/] {CONFIG.model or '(auto-detect)'}",
        f"[bold]Max depth:[/] {CONFIG.max_depth}",
        f"[bold]Max turns/agent:[/] {CONFIG.max_turns_per_agent}",
    ]
    if mcp.registered_tools:
        lines.append(f"[bold]MCP tools:[/] {', '.join(mcp.registered_tools)}")
    else:
        lines.append("[bold]MCP tools:[/] [dim]none (no servers connected)[/]")
    if mcp.connect_errors:
        for server, err in mcp.connect_errors.items():
            lines.append(f"[bold red]MCP error[/] {server}: {err}")
    console.print(
        Panel.fit("\n".join(lines), title="TaskGenie", border_style="cyan")
    )


def _print_blocker(console: Console, result: RunResult) -> None:
    body = (
        f"[bold]Reason:[/] {result.reason or '(none)'}\n\n"
        f"[bold]Needs from you:[/] {result.needs or '(none)'}\n\n"
        f"[bold]Partial progress:[/] {result.partial_progress or '(none reported)'}"
    )
    console.print(Rule(style="yellow"))
    console.print(
        Panel(
            body,
            title="[bold yellow]CEO is blocked — needs your input[/]",
            border_style="yellow",
        )
    )


def _read_unblock_input(console: Console, prompt: str = "you > ") -> str | None:
    console.print(
        "[dim]Reply with guidance to resume, or empty/'abandon' to stop.[/]"
    )
    try:
        response = console.input(f"[bold yellow]{prompt}[/] ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if response.lower() in ABANDON_TOKENS:
        return None
    return response


def _snapshot(session: Session, ceo: Agent) -> None:
    session.set_ceo(ceo.id)
    session.snapshot_agents({aid: a.to_dict() for aid, a in Agent.REGISTRY.items()})
    session.save()


async def _run_session(ceo: Agent, tui: TUI, message: str) -> RunResult:
    ceo_task = asyncio.create_task(ceo.run(message), name="ceo")
    tui_task = asyncio.create_task(tui.run(stop_when=ceo_task), name="tui")
    try:
        result = await ceo_task
    finally:
        await tui_task
    return result


def _build_unblock_message(user_response: str, tool_diff: str | None) -> str:
    parts = ["USER UNBLOCK: " + user_response]
    if tool_diff:
        parts.append("")
        parts.append(tool_diff)
    parts.append("")
    parts.append(
        "Resume your work using your existing subordinates and memory. "
        "You may hire new agents, rehire existing ones, or re-assign with "
        "the new context. Call `complete` when you have the final answer."
    )
    return "\n".join(parts)


async def _drive_session(
    console: Console,
    session: Session,
    mcp: MCPClient,
    ceo: Agent,
    tui: TUI,
    first_message: str,
) -> int:
    message = first_message
    while True:
        try:
            result = await _run_session(ceo, tui, message)
        except KeyboardInterrupt:
            console.print("[yellow]interrupted[/]")
            _snapshot(session, ceo)
            return 130

        # Always snapshot at quiescent points.
        if result.kind == "done":
            session.mark_done(result.report)
            _snapshot(session, ceo)
            console.print(Rule(style="green"))
            console.print(
                Panel(
                    Markdown(result.report or "(no answer)"),
                    title="Final answer",
                    border_style="green",
                )
            )
            console.print(
                f"[dim]Session saved as[/] [bold]{session.session_id}[/]"
            )
            return 0

        if result.kind == "blocked":
            session.mark_blocked(
                {
                    "reason": result.reason,
                    "needs": result.needs,
                    "partial_progress": result.partial_progress,
                }
            )
            _snapshot(session, ceo)
            _print_blocker(console, result)
            console.print(
                f"[dim]Session saved as[/] [bold]{session.session_id}[/] "
                f"[dim]— you can quit (ctrl+C), make changes, then resume with[/] "
                f"[cyan]taskgenie --resume {session.session_id}[/]"
            )
            user_response = _read_unblock_input(console)
            if user_response is None:
                session.mark_abandoned()
                _snapshot(session, ceo)
                console.print("[yellow]Task abandoned (state saved).[/]")
                console.print(
                    Panel(
                        result.partial_progress or "(no partial progress reported)",
                        title="Partial progress",
                        border_style="yellow",
                    )
                )
                return 2
            session.mark_unblock(user_response)
            tool_diff = session.diff_tools_against(
                REGISTRY.signature("management"), REGISTRY.signature("work")
            )
            message = _build_unblock_message(user_response, tool_diff)
            continue

        # exhausted / error
        session.mark_error(result.error or result.kind)
        _snapshot(session, ceo)
        console.print(
            Panel(
                f"[red]{result.kind.upper()}[/]\n\n{result.error or '(no detail)'}",
                title="Run ended",
                border_style="red",
            )
        )
        return 1


async def _run_new(task: str) -> int:
    console = Console()
    mcp = await MCPClient.from_config(CONFIG.mcp_config_path)
    try:
        session = Session.create(task)
        Agent.SCRATCH_ROOT = str(session.scratch_root)
        _print_banner(console, session, mcp)
        ceo = Agent(name="CEO", role_type="ceo", system_prompt=CEO_ROLE_PROMPT)
        tui = TUI(task=task)
        return await _drive_session(console, session, mcp, ceo, tui, task)
    finally:
        await mcp.close()


async def _run_resume(session_id: str | None) -> int:
    console = Console()
    mcp = await MCPClient.from_config(CONFIG.mcp_config_path)
    try:
        if session_id:
            session = Session.load(session_id)
        else:
            session = Session.load_latest()
            if session is None:
                console.print("[red]No sessions to resume.[/]")
                return 1
            console.print(
                f"[dim]Resuming latest session:[/] [bold]{session.session_id}[/]"
            )

        Agent.SCRATCH_ROOT = str(session.scratch_root)
        Agent.bulk_load(session.data.agents)
        if session.data.ceo_id is None or session.data.ceo_id not in Agent.REGISTRY:
            console.print("[red]Session has no CEO recorded — cannot resume.[/]")
            return 1
        ceo = Agent.REGISTRY[session.data.ceo_id]

        _print_banner(console, session, mcp)

        tool_diff = session.diff_tools_against(
            REGISTRY.signature("management"), REGISTRY.signature("work")
        )
        if tool_diff:
            console.print(Panel(tool_diff, title="Tool changes", border_style="cyan"))

        # Show the last blocker context so the user can decide.
        last_blocker = next(
            (h for h in reversed(session.data.history) if h["kind"] == "blocker"),
            None,
        )
        if last_blocker:
            payload = last_blocker.get("payload", {})
            console.print(
                Panel(
                    f"[bold]Reason:[/] {payload.get('reason', '(none)')}\n\n"
                    f"[bold]Needs:[/] {payload.get('needs', '(none)')}\n\n"
                    f"[bold]Partial progress:[/] {payload.get('partial_progress', '(none)')}",
                    title="Previous blocker (paused here)",
                    border_style="yellow",
                )
            )

        user_response = _read_unblock_input(console, prompt="resume with > ")
        if user_response is None:
            console.print("[yellow]Resume cancelled.[/]")
            return 0
        session.mark_unblock(user_response)
        message = _build_unblock_message(user_response, tool_diff)

        tui = TUI(task=session.data.task)
        return await _drive_session(console, session, mcp, ceo, tui, message)
    finally:
        await mcp.close()


def _list_sessions() -> int:
    console = Console()
    rows = Session.list_all()
    if not rows:
        console.print("[dim]No sessions yet.[/]")
        return 0
    table = Table(title="TaskGenie sessions", show_lines=False, expand=False)
    table.add_column("session id", style="cyan", no_wrap=True)
    table.add_column("started", style="dim", no_wrap=True)
    table.add_column("status")
    table.add_column("task", overflow="fold")
    status_styles = {
        "done": "green",
        "blocked": "yellow",
        "running": "blue",
        "abandoned": "red",
        "error": "red",
    }
    for r in rows:
        status = r["status"]
        style = status_styles.get(status, "white")
        table.add_row(
            r["session_id"],
            r["started_at"],
            f"[{style}]{status}[/]",
            r["task"],
        )
    console.print(table)
    return 0


def cli() -> None:
    parser = argparse.ArgumentParser(
        prog="taskgenie",
        description="A hierarchical multi-agent system where managers hire workers by writing role prompts.",
    )
    parser.add_argument("task", nargs="*", help="The task to give the CEO (omit when using --resume or --list).")
    parser.add_argument(
        "--resume",
        nargs="?",
        const="",
        metavar="SESSION_ID",
        help="Resume a session. Pass an id to target one, or no value to resume the latest.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List all sessions and exit.",
    )
    args = parser.parse_args()

    if args.list:
        sys.exit(_list_sessions())

    if args.resume is not None:
        session_id = args.resume or None
        sys.exit(asyncio.run(_run_resume(session_id)))

    if not args.task:
        parser.error("provide a task, --resume, or --list")
    task = " ".join(args.task)
    sys.exit(asyncio.run(_run_new(task)))


if __name__ == "__main__":
    cli()
