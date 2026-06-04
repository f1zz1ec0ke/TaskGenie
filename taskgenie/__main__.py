from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table

from taskgenie.agent import Agent
from taskgenie.artifacts import write_final
from taskgenie.config import CONFIG, setup_logging
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
    except EOFError:
        return None
    # KeyboardInterrupt is intentionally NOT caught -- let it bubble to
    # _drive_session's Ctrl+C handler so the snapshot+rc=130 contract
    # holds at the input prompt too (R75). Otherwise the only escape
    # from the prompt was typing 'abandon', which violates the
    # "Ctrl+C anywhere saves state and exits" promise.
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
    except EOFError:
        return None
    # KeyboardInterrupt deliberately uncaught -- see _read_unblock_input.
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
        # FIRST_EXCEPTION: if either task raises, return immediately so we
        # can surface the failure (rather than hanging on a blocked CEO
        # while the TUI has already died). If both complete without
        # exceptions, this still waits for both -- equivalent to
        # ALL_COMPLETED in the happy path.
        done, _pending = await asyncio.wait(
            {ceo_task, tui_task}, return_when=asyncio.FIRST_EXCEPTION
        )
        for t in done:
            if t.cancelled():
                continue
            exc = t.exception()
            if exc is not None:
                # Propagate the failure. The finally below will cancel
                # whichever task is still in flight.
                raise exc
        # All clean -- ceo_task carries the RunResult.
        return ceo_task.result()
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


_HEADLESS_EVENT_KINDS = frozenset(
    {"spawn", "hire", "assign", "complete", "blocked", "error", "question"}
)


async def _headless_event_printer(stop_event: asyncio.Event) -> None:
    """Background task that prints key BUS events to stderr in a simple
    one-line-per-event format for headless/CI mode."""
    queue = BUS.subscribe()
    try:
        while not stop_event.is_set():
            try:
                event: Event = await asyncio.wait_for(queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if event.kind not in _HEADLESS_EVENT_KINDS:
                continue
            ts = datetime.fromtimestamp(event.ts, tz=timezone.utc).strftime(
                "%H:%M:%S"
            )
            detail_parts: list[str] = []
            if event.kind == "hire" and "child_name" in event.data:
                detail_parts.append(f"hired {event.data['child_name']}")
            elif event.kind == "assign" and "task" in event.data:
                task_text = event.data["task"]
                if len(task_text) > 80:
                    task_text = task_text[:77] + "..."
                detail_parts.append(task_text)
            elif event.kind == "complete" and "report" in event.data:
                report_text = event.data["report"]
                if len(report_text) > 80:
                    report_text = report_text[:77] + "..."
                detail_parts.append(report_text)
            elif event.kind == "blocked" and "reason" in event.data:
                detail_parts.append(event.data["reason"])
            elif event.kind == "error" and "error" in event.data:
                detail_parts.append(event.data["error"])
            elif event.kind == "question" and "question" in event.data:
                detail_parts.append(event.data["question"])
            elif event.kind == "spawn":
                detail_parts.append(
                    f"status: {event.data.get('status', 'working')}"
                )
            detail = " ".join(detail_parts) if detail_parts else event.kind
            print(
                f"[{ts}] {event.agent_name}: {event.kind} {detail}",
                file=sys.stderr,
            )
    except asyncio.CancelledError:
        pass
    finally:
        BUS.unsubscribe(queue)


async def _run_session_headless(ceo: Agent, message: str) -> RunResult:
    """Run the CEO without the Rich TUI. Prints key events to stderr."""
    stop_event = asyncio.Event()
    printer_task = asyncio.create_task(
        _headless_event_printer(stop_event), name="headless-printer"
    )
    ceo_task = asyncio.create_task(ceo.run(message), name="ceo")
    try:
        result = await ceo_task
        return result
    finally:
        stop_event.set()
        if not printer_task.done():
            printer_task.cancel()
        if not ceo_task.done():
            ceo_task.cancel()
        await asyncio.gather(ceo_task, printer_task, return_exceptions=True)


def _workspace_file_list(session: Session) -> list[str]:
    """Return relative paths of all files in the project dir."""
    base = session.project_dir
    if not base.exists():
        return []
    return sorted(
        str(p.relative_to(base)) for p in base.rglob("*") if p.is_file()
    )


def _emit_json_result(
    status: str,
    session: Session | None = None,
    report: str = "",
    stats_dict: dict | None = None,
    error: str = "",
) -> None:
    """Print a JSON result object to stdout for --json mode."""
    if status == "done":
        obj: dict[str, Any] = {
            "status": "done",
            "report": report,
            "session_id": session.session_id if session else "",
            "project_dir": str(session.project_dir) if session else "",
            "stats": stats_dict or {},
            "workspace_files": (
                _workspace_file_list(session) if session else []
            ),
        }
    else:
        obj = {"status": "error", "error": error}
    print(json.dumps(obj, indent=2), file=sys.stdout)


FOLLOWUP_COMMANDS = {"/done", "/status", "/retry", "/reset"}


def _read_followup_input(console: Console) -> str | None:
    """Prompt the user for a follow-up task or slash command after CEO
    completes a turn. Returns None on EOF/empty input.

    KeyboardInterrupt is intentionally uncaught so the outer Ctrl+C
    handler in `_drive_session` snapshots + returns 130 instead of
    silently exiting the follow-up loop.
    """
    console.print(
        "[dim]Type a follow-up task, or one of: "
        "[cyan]/done[/] [cyan]/status[/] [cyan]/retry[/] [cyan]/reset[/][/]"
    )
    try:
        response = console.input("[bold cyan]you > [/] ").strip()
    except EOFError:
        return None
    return response or None


def _build_followup_message(user_text: str) -> str:
    # Kept terse on purpose: the FOLLOW-UP TURNS section of the CEO
    # prompt already explains team persistence, list_workspace use,
    # re-assign-vs-rehire-vs-fire, and when to complete. Repeating that
    # guidance every follow-up turn is dead weight in the context window.
    return f"USER FOLLOW-UP: {user_text}\n\nCall `complete` when this follow-up is addressed."


async def _reset_team(session: Session, ceo: Agent, console: Console) -> int:
    """Fire every subordinate (recursively), but keep the CEO and the
    workspace files. The CEO is told the team is gone; the next user
    task is effectively a fresh start with the existing workspace as
    context.

    Async because we cancel+await any in-flight child tasks the CEO
    had queued -- without this, replacing `task_assignments = {}`
    leaves the underlying asyncio.Task objects alive in the event
    loop. The new `_drain_pending_assignments` at run()-exit sees the
    empty dict and reaps nothing; the tasks dangle to 'Task was
    destroyed but it is pending!' at shutdown. Cancelling here closes
    that gap.
    """
    fired_count = 0
    for agent in list(Agent.REGISTRY.values()):
        if agent.id == ceo.id:
            continue
        if agent.status != "fired":
            # Use _set_status (not direct mutation + manual publish) so
            # the transition goes through the same path as _tool_fire
            # (R11) -- consistent event payload, future-proof for any
            # side effects _set_status grows.
            agent._set_status("fired", reason="user /reset")
            fired_count += 1
        # GC from REGISTRY entirely so successive /resets don't grow
        # state.json unbounded. The TUI's own self.agents view still
        # has them and renders the "fired" filter, so no visual loss.
        Agent.REGISTRY.pop(agent.id, None)
    # Cancel + await CEO's pending children BEFORE wiping the dict.
    # Otherwise the task references survive only in the event loop,
    # untracked anywhere, and asyncio warns at process exit.
    pending = [t for t in ceo.task_assignments.values() if not t.done()]
    for t in pending:
        t.cancel()
    if pending:
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True),
                timeout=2.0,
            )
        except (asyncio.TimeoutError, Exception):
            pass  # best-effort; /reset shouldn't hang the CLI
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

    # Verify the CEO instance is STILL the registered one (not just that
    # the id is present). If anyone rebound REGISTRY[ceo.id] to a fresh
    # Agent (e.g., a future feature that swaps the root), we'd be reading
    # stale .children off a detached object.
    if Agent.REGISTRY.get(ceo.id) is not ceo:
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
    tui: TUI | None,
    first_message: str,
    logger: JSONLEventLogger | None = None,
    extra_output: Path | None = None,
    *,
    headless: bool = False,
    json_output: bool = False,
) -> int:
    message = first_message
    last_followup: str | None = None
    while True:
        try:
            if headless:
                result = await _run_session_headless(ceo, message)
            else:
                assert tui is not None
                result = await _run_session(ceo, tui, message)
        except (KeyboardInterrupt, asyncio.CancelledError):
            # Python 3.11+ asyncio.Runner converts SIGINT to a
            # CancelledError on the running task; catching KeyboardInterrupt
            # alone would miss it and the in-flight snapshot promise would
            # silently break. Snapshot and exit 130 either way.
            if headless:
                print("interrupted", file=sys.stderr)
            else:
                console.print("[yellow]interrupted[/]")
            _snapshot(session, ceo)
            if json_output:
                _emit_json_result("error", error="interrupted")
            return 130

        # Always snapshot at quiescent points.
        if result.kind == "done":
            session.mark_done(result.report)
            _snapshot(session, ceo)

            if headless:
                # Minimal stderr summary, then JSON or plain-text to stdout.
                print(
                    f"done: session {session.session_id}", file=sys.stderr
                )
            else:
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
            if not headless:
                _print_workspace_panel(console, session)
            stats_dict = await _compute_stats_for_artifact(
                session, ceo, logger
            )
            try:
                paths = write_final(
                    session,
                    ceo,
                    result.report,
                    stats_dict,
                    extra_output=extra_output,
                )
                if not headless:
                    lines = [
                        f"  [cyan]markdown[/]: {paths['md']}",
                        f"  [cyan]html[/]:     {paths['html']}",
                        f"  [cyan]json[/]:     {paths['json']}",
                    ]
                    if readme_path is not None:
                        lines.append(
                            f"  [bold green]README[/]:   {readme_path}"
                        )
                    if "extra" in paths:
                        lines.append(
                            f"  [bold green]--output[/]: {paths['extra']}"
                        )
                    console.print(
                        Panel(
                            "\n".join(lines),
                            title="Artifacts",
                            border_style="cyan",
                        )
                    )
            except Exception as e:
                if headless:
                    print(
                        f"warning: failed to write artifacts: {e}",
                        file=sys.stderr,
                    )
                else:
                    console.print(f"[red]Failed to write artifacts: {e}[/]")

            if json_output:
                _emit_json_result(
                    "done",
                    session=session,
                    report=result.report or "",
                    stats_dict=stats_dict,
                )
                return 0

            if headless:
                # No follow-up loop in headless mode -- just print and exit.
                print(result.report or "(no answer)", file=sys.stdout)
                return 0

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
                    await _print_status_panel(
                        console, session, ceo, logger
                    )
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
                    await _reset_team(session, ceo, console)
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
            if headless:
                # Cannot prompt interactively in headless mode.
                error_msg = (
                    f"CEO asked a question (not supported in headless "
                    f"mode): {result.question}"
                )
                print(error_msg, file=sys.stderr)
                session.mark_abandoned()
                _snapshot(session, ceo)
                if json_output:
                    _emit_json_result("error", error=error_msg)
                return 2
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
            if headless:
                error_msg = (
                    f"CEO is blocked: {result.reason or '(no reason)'} "
                    f"| needs: {result.needs or '(none)'}"
                )
                print(error_msg, file=sys.stderr)
                session.mark_abandoned()
                _snapshot(session, ceo)
                if json_output:
                    _emit_json_result("error", error=error_msg)
                return 2
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
                        result.partial_progress
                        or "(no partial progress reported)",
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
        if json_output:
            _emit_json_result(
                "error",
                error=result.error or result.kind or "(unknown)",
            )
        if headless:
            print(
                f"error: {result.kind}: {result.error or '(no detail)'}",
                file=sys.stderr,
            )
        else:
            console.print(
                Panel(
                    f"[red]{result.kind.upper()}[/]\n\n"
                    f"{result.error or '(no detail)'}",
                    title="Run ended",
                    border_style="red",
                )
            )
        return 1


async def _run_new(
    task: str,
    extra_output: Path | None = None,
    project: str | None = None,
    *,
    headless: bool = False,
    json_output: bool = False,
) -> int:
    console = Console(stderr=True) if headless else Console()
    mcp = await MCPClient.from_config(CONFIG.mcp_config_path)
    logger: JSONLEventLogger | None = None
    try:
        session = Session.create(task, project=project)
        Agent.SCRATCH_ROOT = str(session.scratch_root)
        Agent.WORKSPACE_ROOT = str(session.project_dir)
        session.project_dir.mkdir(parents=True, exist_ok=True)
        logger = JSONLEventLogger(session.events_path)
        logger.start()
        if not headless:
            _print_banner(console, session, mcp)
        else:
            print(
                f"session: {session.session_id} | "
                f"project: {session.project_dir}",
                file=sys.stderr,
            )
        ceo = Agent(name="CEO", role_type="ceo", system_prompt=CEO_ROLE_PROMPT)
        tui: TUI | None = None if headless else TUI(task=task)
        return await _drive_session(
            console,
            session,
            mcp,
            ceo,
            tui,
            task,
            logger,
            extra_output=extra_output,
            headless=headless,
            json_output=json_output,
        )
    finally:
        if logger is not None:
            await logger.stop()
        await mcp.close()


async def _run_resume(
    session_id: str | None,
    extra_output: Path | None = None,
    *,
    headless: bool = False,
    json_output: bool = False,
) -> int:
    console = Console(stderr=True) if headless else Console()
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

        if not headless:
            _print_banner(console, session, mcp)
        else:
            print(
                f"resuming session: {session.session_id}", file=sys.stderr
            )

        tool_diff = session.diff_tools_against(
            REGISTRY.signature("management"),
            REGISTRY.signature("work"),
            REGISTRY.signature("ceo"),
        )
        if tool_diff and not headless:
            console.print(Panel(tool_diff, title="Tool changes", border_style="cyan"))

        # Find the most-recent terminal/resolution entry. Pauses BEFORE
        # that one are historical (already answered / unblocked /
        # completed), not what we're paused on right now. Without this
        # qualification, resuming a session that went
        # question->answer->done would find the long-resolved question and
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
            if headless:
                # Cannot prompt interactively in headless mode.
                error_msg = (
                    "Session paused on a question; cannot resume "
                    "in headless mode without user input."
                )
                print(error_msg, file=sys.stderr)
                if json_output:
                    _emit_json_result("error", error=error_msg)
                return 2
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
            if headless:
                # Headless resume of a completed session: nothing to do.
                print(
                    "Session already completed; nothing to resume.",
                    file=sys.stderr,
                )
                if json_output:
                    _emit_json_result(
                        "error",
                        error="Session already completed.",
                    )
                return 0
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
        elif session.data.status in ("abandoned", "error"):
            if headless:
                error_msg = (
                    f"Session status is '{session.data.status}'; cannot "
                    "resume in headless mode without user input."
                )
                print(error_msg, file=sys.stderr)
                if json_output:
                    _emit_json_result("error", error=error_msg)
                return 2
            # Resuming a session that previously ended (user abandoned
            # or it errored out). Don't pretend it's a regular blocker
            # -- tell the user what happened, then offer a fresh
            # follow-up. R61.
            border = "red" if session.data.status == "error" else "yellow"
            title = (
                "Session previously errored"
                if session.data.status == "error"
                else "Session was abandoned"
            )
            # Try to surface the last error/abandon detail from history.
            last_terminal = next(
                (
                    h for h in reversed(session.data.history)
                    if h["kind"] in ("error", "abandoned")
                ),
                None,
            )
            detail = (
                last_terminal.get("text", "")
                if last_terminal
                else ""
            )
            body = (
                f"[bold]Status:[/] {session.data.status}\n"
                + (f"[bold]Detail:[/] {detail}\n" if detail else "")
                + "Give a follow-up task to pick the work back up, or "
                "type [cyan]/done[/] to leave it as-is."
            )
            console.print(Panel(body, title=title, border_style=border))
            followup = _read_followup_input(console)
            if followup is None or followup == "/done":
                console.print("[dim]Nothing changed.[/]")
                return 0
            session.mark_followup(followup)
            _snapshot(session, ceo)
            message = _build_followup_message(followup)
        else:
            if headless:
                # Blocked session -- can't prompt for unblock guidance.
                error_msg = (
                    "Session is blocked; cannot resume in headless mode "
                    "without user input."
                )
                print(error_msg, file=sys.stderr)
                if json_output:
                    _emit_json_result("error", error=error_msg)
                return 2
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

        tui: TUI | None = None if headless else TUI(task=session.data.task)
        return await _drive_session(
            console,
            session,
            mcp,
            ceo,
            tui,
            message,
            logger,
            extra_output=extra_output,
            headless=headless,
            json_output=json_output,
        )
    finally:
        if logger is not None:
            await logger.stop()
        await mcp.close()


def _compute_duration(started_at: str, last_saved_at: str) -> str:
    """Return a human-readable duration string, or '-' if unparseable."""
    try:
        start = datetime.fromisoformat(started_at)
        end = datetime.fromisoformat(last_saved_at)
        delta = end - start
        total_secs = int(delta.total_seconds())
        if total_secs < 0:
            return "-"
        if total_secs < 60:
            return f"{total_secs}s"
        minutes, secs = divmod(total_secs, 60)
        if minutes < 60:
            return f"{minutes}m{secs}s"
        hours, minutes = divmod(minutes, 60)
        return f"{hours}h{minutes}m"
    except Exception:
        return "-"


def _list_sessions(filter_text: str | None = None) -> int:
    console = Console()
    rows = Session.list_all()
    if filter_text:
        needle = filter_text.lower()
        rows = [r for r in rows if needle in r["task"].lower()]
    if not rows:
        if filter_text:
            console.print(f"[dim]No sessions matching '{filter_text}'.[/]")
        else:
            console.print("[dim]No sessions yet.[/]")
        return 0
    table = Table(title="TaskGenie sessions", show_lines=False, expand=False)
    table.add_column("session id", style="cyan", no_wrap=True)
    table.add_column("started", style="dim", no_wrap=True)
    table.add_column("duration", style="dim", no_wrap=True)
    table.add_column("status")
    table.add_column("agents", justify="right")
    table.add_column("tokens", justify="right", style="dim")
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
        duration = _compute_duration(r["started_at"], r["last_saved_at"])
        agent_count = r.get("agent_count", 0)
        prompt_tokens = r.get("prompt_tokens", 0)
        token_str = f"{prompt_tokens:,}" if prompt_tokens else "-"
        table.add_row(
            r["session_id"],
            r["started_at"],
            duration,
            f"[{style}]{status}[/]",
            str(agent_count) if agent_count else "-",
            token_str,
            r["task"],
        )
    console.print(table)
    return 0


def _delete_session(session_id: str, force: bool = False) -> int:
    """Delete a session directory and all its data."""
    console = Console()
    session_dir = CONFIG.sessions_dir / session_id
    if not session_dir.exists():
        console.print(f"[red]Session not found:[/] {session_id}")
        return 1
    if not force:
        try:
            answer = console.input(
                f"Delete session [cyan]{session_id}[/] and all its data? [y/N] "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[yellow]Cancelled.[/]")
            return 1
        if answer not in ("y", "yes"):
            console.print("[yellow]Cancelled.[/]")
            return 1
    shutil.rmtree(session_dir)
    console.print(f"[green]Deleted session:[/] {session_id}")
    return 0


def _show_stats(session_id: str) -> int:
    """Show the run stats panel for a past session without resuming it."""
    console = Console()
    try:
        session = Session.load(session_id)
    except (FileNotFoundError, ValueError) as e:
        console.print(f"[red]{e}[/]")
        return 1
    if not session.events_path.exists():
        console.print(f"[red]No events log found for session:[/] {session_id}")
        return 1
    try:
        stats = stats_module.compute(
            session.events_path, ceo_id=session.data.ceo_id
        )
    except Exception as e:
        console.print(f"[red]Failed to compute stats:[/] {e}")
        return 1
    console.print(stats_module.render_panel(stats))
    return 0


def cli() -> None:
    setup_logging(getattr(CONFIG, "log_level", "WARNING"))

    epilog = """examples:
  taskgenie "Build a physics game"          # new task
  taskgenie --resume                        # resume latest session
  taskgenie --list                          # list all sessions
  taskgenie --list --filter "game"          # search sessions
  taskgenie --stats <session-id>            # view past run stats
  taskgenie --delete <session-id>           # delete a session
  taskgenie --delete <session-id> --force   # delete without confirmation
"""
    parser = argparse.ArgumentParser(
        prog="taskgenie",
        description="A hierarchical multi-agent system where managers hire workers by writing role prompts.",
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
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
        "--filter",
        type=str,
        default=None,
        metavar="TEXT",
        help="Filter sessions by task text (case-insensitive substring match). Use with --list.",
    )
    parser.add_argument(
        "--delete",
        type=str,
        default=None,
        metavar="SESSION_ID",
        help="Delete a session and all its data.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Skip confirmation prompt when deleting a session.",
    )
    parser.add_argument(
        "--stats",
        type=str,
        default=None,
        metavar="SESSION_ID",
        help="Show the run stats panel for a past session without resuming it.",
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
    parser.add_argument(
        "--no-tui",
        "--headless",
        action="store_true",
        dest="headless",
        default=False,
        help=(
            "Run without the Rich TUI. Status updates are printed to stderr "
            "and the final result to stdout. Useful for CI/scripting."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        default=False,
        help=(
            "Output the final result as JSON to stdout (implies --no-tui). "
            "On success: {status, report, session_id, project_dir, stats, "
            "workspace_files}. On error: {status, error}."
        ),
    )
    args = parser.parse_args()

    # --json implies --no-tui / headless.
    headless: bool = args.headless or args.json_output or CONFIG.headless
    json_output: bool = args.json_output

    # --delete: early return
    if args.delete is not None:
        sys.exit(_delete_session(args.delete, force=args.force))

    # --stats: early return
    if args.stats is not None:
        sys.exit(_show_stats(args.stats))

    if args.list:
        sys.exit(_list_sessions(filter_text=args.filter))

    if args.resume is not None:
        session_id = args.resume or None
        sys.exit(
            asyncio.run(
                _run_resume(
                    session_id,
                    extra_output=args.output,
                    headless=headless,
                    json_output=json_output,
                )
            )
        )

    if not args.task:
        parser.error("provide a task, --resume, --list, --stats, or --delete")
    task = " ".join(args.task)
    sys.exit(
        asyncio.run(
            _run_new(
                task,
                extra_output=args.output,
                project=args.project,
                headless=headless,
                json_output=json_output,
            )
        )
    )


if __name__ == "__main__":
    cli()
