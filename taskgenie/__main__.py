from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table

from taskgenie.agent import Agent
from taskgenie.artifacts import write_final
from taskgenie.config import CONFIG
from taskgenie.event_logger import JSONLEventLogger
from taskgenie.events import BUS, Event
from taskgenie import stats as stats_module
from taskgenie.mcp_client import MCPClient
from taskgenie.prompts import CEO_ROLE_PROMPT
from taskgenie.result import RunResult
from taskgenie.session import Session
from taskgenie.tools.registry import REGISTRY
from taskgenie.tui import TUI

ABANDON_TOKENS = {"", "quit", "exit", "abandon", "stop"}


def _print_banner(console: Console, session: Session, mcp: MCPClient) -> None:
    lines = [
        f"[bold]Project:[/] [green]{session.project_dir}[/]",
        f"[bold]Session:[/] [dim]{session.session_id}[/]",
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


def _print_question(console: Console, result: RunResult) -> None:
    parts = [f"[bold]Question:[/] {result.question}"]
    if result.context:
        parts.append("")
        parts.append(f"[bold]Context:[/] {result.context}")
    if result.options:
        parts.append("")
        parts.append("[bold]Suggested answers:[/]")
        for i, opt in enumerate(result.options, 1):
            parts.append(f"  {i}. {opt}")
    console.print(Rule(style="cyan"))
    console.print(
        Panel(
            "\n".join(parts),
            title="[bold cyan]CEO has a question for you[/]",
            border_style="cyan",
        )
    )


def _read_answer_input(console: Console, options: list[str]) -> str | None:
    hint = (
        "[dim]Type your answer."
        + (" Or type a number to pick a suggested option." if options else "")
        + " Empty/'abandon' aborts the task.[/]"
    )
    console.print(hint)
    try:
        response = console.input("[bold cyan]answer > [/] ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if response.lower() in ABANDON_TOKENS:
        return None
    if options and response.isdigit():
        idx = int(response) - 1
        if 0 <= idx < len(options):
            return options[idx]
    return response


def _snapshot(session: Session, ceo: Agent) -> None:
    session.set_ceo(ceo.id)
    session.snapshot_agents({aid: a.to_dict() for aid, a in Agent.REGISTRY.items()})
    session.save()


async def _render_stats_panel(
    console: Console,
    session: Session,
    ceo: Agent,
    logger: JSONLEventLogger | None,
) -> None:
    if logger is not None:
        await logger.drain()
    try:
        stats = stats_module.compute(session.events_path, ceo_id=ceo.id)
    except Exception:
        return
    console.print(stats_module.render_panel(stats))


async def _run_session(ceo: Agent, tui: TUI, message: str) -> RunResult:
    ceo_task = asyncio.create_task(ceo.run(message), name="ceo")
    tui_task = asyncio.create_task(tui.run(stop_when=ceo_task), name="tui")
    try:
        return await ceo_task
    finally:
        # If anything raised (CancelledError on Ctrl+C, an unrelated TUI
        # exception, the CEO bubbling something up) we still need both
        # tasks fully unwound -- otherwise asyncio prints "Task was
        # destroyed but it is pending!" at shutdown and the TUI may leave
        # the terminal in cbreak mode. gather() with return_exceptions
        # so the cleanup never masks the original exception.
        if not ceo_task.done():
            ceo_task.cancel()
        if not tui_task.done():
            tui_task.cancel()
        await asyncio.gather(ceo_task, tui_task, return_exceptions=True)


FOLLOWUP_COMMANDS = {"/done", "/status", "/retry", "/reset"}


def _read_followup_input(console: Console) -> str | None:
    """Prompt the user for a follow-up task or slash command after CEO
    completes a turn. Returns None on EOF/Ctrl+C/empty input."""
    console.print(
        "[dim]Type a follow-up task, or one of: "
        "[cyan]/done[/] [cyan]/status[/] [cyan]/retry[/] [cyan]/reset[/][/]"
    )
    try:
        response = console.input("[bold cyan]you > [/] ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    return response or None


def _build_followup_message(user_text: str) -> str:
    return (
        f"USER FOLLOW-UP: {user_text}\n\n"
        "The team you previously hired and the workspace files all persist. "
        "Decide whether to re-assign existing managers (they keep their "
        "memory of what they built), hire new managers/workers for new "
        "facets of this follow-up, or `fire` and replace if a previous "
        "role is now wrong. Call `list_workspace` first if you need to "
        "see what's currently in the project before deciding. "
        "Call `complete` when this follow-up is addressed."
    )


def _reset_team(session: Session, ceo: Agent, console: Console) -> int:
    """Fire every subordinate (recursively), but keep the CEO and the
    workspace files. The CEO is told the team is gone; the next user
    task is effectively a fresh start with the existing workspace as
    context."""
    fired_count = 0
    for agent in list(Agent.REGISTRY.values()):
        if agent.id == ceo.id:
            continue
        if agent.status != "fired":
            agent.status = "fired"
            BUS.publish(
                Event("state", agent.id, agent.name, data={"status": "fired"})
            )
            fired_count += 1
    ceo.children = []
    ceo.task_assignments = {}
    ceo.task_targets = {}
    ceo.messages.append(
        {
            "role": "user",
            "content": (
                "USER COMMAND: /reset. Your previous team has been disbanded. "
                "The workspace files persist (call `list_workspace` to see). "
                "Treat the next task as a fresh start, but the workspace is "
                "available as context."
            ),
        }
    )
    session.mark_reset(fired_count)
    console.print(
        f"[yellow]/reset:[/] fired {fired_count} subordinate agents. "
        "Workspace preserved."
    )
    return fired_count


async def _print_status_panel(
    console: Console, session: Session, ceo: Agent, logger: JSONLEventLogger | None
) -> None:
    """Show the current agent tree + stats without giving a new task."""
    from rich.tree import Tree as RTree

    if ceo.id not in Agent.REGISTRY:
        console.print("[red]CEO is no longer in the registry.[/]")
        return
    root_label = f"[bold]{ceo.name}[/] [dim]{ceo.role_type}/{ceo.status}[/]"
    rtree = RTree(root_label)

    def _walk(parent_node: Any, agent: Agent) -> None:
        for child in agent.children:
            label = (
                f"[bold]{child.name}[/]  "
                f"[dim]{child.role_type}/{child.status}[/]"
            )
            node = parent_node.add(label)
            _walk(node, child)

    _walk(rtree, ceo)
    console.print(Panel(rtree, title="Current hierarchy", border_style="cyan"))
    await _render_stats_panel(console, session, ceo, logger)


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


def _build_answer_message(user_answer: str) -> str:
    return (
        f"USER ANSWER: {user_answer}\n\n"
        "Apply this guidance and continue your work. Do not re-ask the same "
        "question. Call `complete` when you have the final answer."
    )


def _print_workspace_panel(console: Console, session: Session) -> None:
    """If the team produced real files, show a tree of what's in the project dir."""
    base = session.project_dir
    if not base.exists():
        return
    files = sorted(p for p in base.rglob("*") if p.is_file())
    if not files:
        return
    lines = [f"[bold]Project:[/] [green]{base}[/]", ""]
    for p in files:
        rel = p.relative_to(base)
        size = p.stat().st_size
        lines.append(f"  [cyan]{rel}[/]  [dim]({size} bytes)[/]")
    console.print(
        Panel(
            "\n".join(lines),
            title=f"Deliverables ({len(files)} files)",
            border_style="green",
        )
    )


def _write_project_readme(session: Session, report: str) -> Path | None:
    """Copy the CEO's report into the project dir as README.md so the user
    sees the narrative right next to the deliverables when they cd in."""
    if not report:
        return None
    base = session.project_dir
    if not base.exists():
        return None
    readme_path = base / "README.md"
    body = (
        f"# {session.data.task}\n\n"
        f"_Generated by TaskGenie session `{session.session_id}` on "
        f"{session.data.last_saved_at}_\n\n"
        "---\n\n"
        f"{report}\n"
    )
    readme_path.write_text(body, encoding="utf-8")
    return readme_path


async def _compute_stats_for_artifact(
    session: Session, ceo: Agent, logger: JSONLEventLogger | None
) -> dict | None:
    if logger is not None:
        await logger.drain()
    try:
        s = stats_module.compute(session.events_path, ceo_id=ceo.id)
        return asdict(s)
    except Exception:
        return None


async def _drive_session(
    console: Console,
    session: Session,
    mcp: MCPClient,
    ceo: Agent,
    tui: TUI,
    first_message: str,
    logger: JSONLEventLogger | None = None,
    extra_output: Path | None = None,
) -> int:
    message = first_message
    last_followup: str | None = None
    while True:
        try:
            result = await _run_session(ceo, tui, message)
        except (KeyboardInterrupt, asyncio.CancelledError):
            # Python 3.11+ asyncio.Runner converts SIGINT to a
            # CancelledError on the running task; catching KeyboardInterrupt
            # alone would miss it and the in-flight snapshot promise would
            # silently break. Snapshot and exit 130 either way.
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
            await _render_stats_panel(console, session, ceo, logger)
            readme_path = _write_project_readme(session, result.report)
            _print_workspace_panel(console, session)
            stats_dict = await _compute_stats_for_artifact(session, ceo, logger)
            try:
                paths = write_final(
                    session, ceo, result.report, stats_dict, extra_output=extra_output
                )
                lines = [
                    f"  [cyan]markdown[/]: {paths['md']}",
                    f"  [cyan]html[/]:     {paths['html']}",
                    f"  [cyan]json[/]:     {paths['json']}",
                ]
                if readme_path is not None:
                    lines.append(f"  [bold green]README[/]:   {readme_path}")
                if "extra" in paths:
                    lines.append(f"  [bold green]--output[/]: {paths['extra']}")
                console.print(
                    Panel("\n".join(lines), title="Artifacts", border_style="cyan")
                )
            except Exception as e:
                console.print(f"[red]Failed to write artifacts: {e}[/]")
            console.print(
                f"[dim]Session saved as[/] [bold]{session.session_id}[/]"
            )

            # Follow-up loop. Stay until user types /done or sends EOF.
            next_followup: str | None = None
            while next_followup is None:
                console.print(Rule(style="cyan"))
                raw = _read_followup_input(console)
                if raw is None or raw == "/done":
                    console.print(
                        f"[dim]Resume any time with[/] "
                        f"[cyan]taskgenie --resume {session.session_id}[/]"
                    )
                    return 0
                if raw == "/status":
                    await _print_status_panel(console, session, ceo, logger)
                    continue
                if raw == "/retry":
                    if last_followup is None:
                        console.print(
                            "[yellow]No previous follow-up to retry.[/]"
                        )
                        continue
                    console.print(
                        f"[dim]/retry — replaying:[/] {last_followup}"
                    )
                    next_followup = last_followup
                    break
                if raw == "/reset":
                    _reset_team(session, ceo, console)
                    _snapshot(session, ceo)
                    continue  # back to prompt; next non-command input is the fresh task
                if raw.startswith("/"):
                    console.print(
                        f"[yellow]Unknown command:[/] {raw}. "
                        f"Try {', '.join(sorted(FOLLOWUP_COMMANDS))}."
                    )
                    continue
                next_followup = raw

            last_followup = next_followup
            session.mark_followup(next_followup)
            _snapshot(session, ceo)
            message = _build_followup_message(next_followup)
            continue

        if result.kind == "question":
            session.mark_question(
                {
                    "question": result.question,
                    "options": result.options,
                    "context": result.context,
                }
            )
            _snapshot(session, ceo)
            _print_question(console, result)
            user_answer = _read_answer_input(console, result.options)
            if user_answer is None:
                session.mark_abandoned()
                _snapshot(session, ceo)
                console.print("[yellow]Task abandoned (state saved).[/]")
                return 2
            session.mark_answer(user_answer)
            _snapshot(session, ceo)
            message = _build_answer_message(user_answer)
            continue

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
                REGISTRY.signature("management"),
                REGISTRY.signature("work"),
                REGISTRY.signature("ceo"),
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


async def _run_new(
    task: str, extra_output: Path | None = None, project: str | None = None
) -> int:
    console = Console()
    mcp = await MCPClient.from_config(CONFIG.mcp_config_path)
    logger: JSONLEventLogger | None = None
    try:
        session = Session.create(task, project=project)
        Agent.SCRATCH_ROOT = str(session.scratch_root)
        Agent.WORKSPACE_ROOT = str(session.project_dir)
        session.project_dir.mkdir(parents=True, exist_ok=True)
        logger = JSONLEventLogger(session.events_path)
        logger.start()
        _print_banner(console, session, mcp)
        ceo = Agent(name="CEO", role_type="ceo", system_prompt=CEO_ROLE_PROMPT)
        tui = TUI(task=task)
        return await _drive_session(
            console, session, mcp, ceo, tui, task, logger, extra_output=extra_output
        )
    finally:
        if logger is not None:
            await logger.stop()
        await mcp.close()


async def _run_resume(session_id: str | None, extra_output: Path | None = None) -> int:
    console = Console()
    mcp = await MCPClient.from_config(CONFIG.mcp_config_path)
    logger: JSONLEventLogger | None = None
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
        Agent.WORKSPACE_ROOT = str(session.project_dir)
        session.project_dir.mkdir(parents=True, exist_ok=True)
        logger = JSONLEventLogger(session.events_path)
        logger.start()
        Agent.bulk_load(session.data.agents)
        if session.data.ceo_id is None or session.data.ceo_id not in Agent.REGISTRY:
            console.print("[red]Session has no CEO recorded — cannot resume.[/]")
            return 1
        ceo = Agent.REGISTRY[session.data.ceo_id]

        _print_banner(console, session, mcp)

        tool_diff = session.diff_tools_against(
            REGISTRY.signature("management"),
            REGISTRY.signature("work"),
            REGISTRY.signature("ceo"),
        )
        if tool_diff:
            console.print(Panel(tool_diff, title="Tool changes", border_style="cyan"))

        # Find the most-recent terminal/resolution entry. Pauses BEFORE
        # that one are historical (already answered / unblocked /
        # completed), not what we're paused on right now. Without this
        # qualification, resuming a session that went
        # question→answer→done would find the long-resolved question and
        # re-ask it -- the CEO would end up with back-to-back answer
        # messages for the same question.
        resolved_idx = -1
        for _i, _h in enumerate(session.data.history):
            if _h["kind"] in (
                "answer",
                "unblock",
                "done",
                "followup",
                "abandoned",
                "error",
            ):
                resolved_idx = _i
        last_pause = next(
            (
                h for h in reversed(session.data.history[resolved_idx + 1 :])
                if h["kind"] in ("blocker", "question")
            ),
            None,
        )
        message: str
        if last_pause and last_pause["kind"] == "question":
            payload = last_pause.get("payload", {})
            console.print(
                Panel(
                    f"[bold]Question:[/] {payload.get('question', '(none)')}\n\n"
                    f"[bold]Context:[/] {payload.get('context', '(none)')}",
                    title="Previous question (paused here)",
                    border_style="cyan",
                )
            )
            user_answer = _read_answer_input(console, payload.get("options", []) or [])
            if user_answer is None:
                console.print("[yellow]Resume cancelled.[/]")
                return 0
            session.mark_answer(user_answer)
            _snapshot(session, ceo)
            message = _build_answer_message(user_answer)
        elif session.data.status == "done":
            # Session previously completed -- resume into a follow-up turn
            # rather than the blocker-style "what unblocks you?" prompt.
            console.print(
                Panel(
                    f"[bold]Last answer:[/] this session was last marked done. "
                    "Give a follow-up task to extend or refine the work, or "
                    "type [cyan]/done[/] to leave it as-is.",
                    title="Session was completed",
                    border_style="green",
                )
            )
            followup = _read_followup_input(console)
            if followup is None or followup == "/done":
                console.print("[dim]Nothing changed.[/]")
                return 0
            session.mark_followup(followup)
            _snapshot(session, ceo)
            message = _build_followup_message(followup)
        else:
            if last_pause and last_pause["kind"] == "blocker":
                payload = last_pause.get("payload", {})
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
            _snapshot(session, ceo)
            message = _build_unblock_message(user_response, tool_diff)

        tui = TUI(task=session.data.task)
        return await _drive_session(
            console, session, mcp, ceo, tui, message, logger, extra_output=extra_output
        )
    finally:
        if logger is not None:
            await logger.stop()
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
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "Also write a copy of the CEO's report to PATH. Format is inferred "
            "from the extension: .md, .html, or .json. The deliverable FILES "
            "the team produces always land in ./TaskGenie/<project>/."
        ),
    )
    parser.add_argument(
        "-p",
        "--project",
        type=str,
        default=None,
        metavar="NAME",
        help=(
            "Project directory name to use under TaskGenie/. Defaults to a "
            "slug derived from the task; e.g. 'build a physics game' becomes "
            "'build-a-physics-game'. A numeric suffix is appended if the name "
            "is already taken."
        ),
    )
    args = parser.parse_args()

    if args.list:
        sys.exit(_list_sessions())

    if args.resume is not None:
        session_id = args.resume or None
        sys.exit(asyncio.run(_run_resume(session_id, extra_output=args.output)))

    if not args.task:
        parser.error("provide a task, --resume, or --list")
    task = " ".join(args.task)
    sys.exit(asyncio.run(_run_new(task, extra_output=args.output, project=args.project)))


if __name__ == "__main__":
    cli()
