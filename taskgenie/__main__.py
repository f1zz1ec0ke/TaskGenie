from __future__ import annotations

import argparse
import asyncio
import sys

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule

from taskgenie.agent import Agent
from taskgenie.config import CONFIG
from taskgenie.prompts import CEO_ROLE_PROMPT
from taskgenie.result import RunResult
from taskgenie.tui import TUI

ABANDON_TOKENS = {"", "quit", "exit", "abandon", "stop"}


def _print_banner(console: Console) -> None:
    console.print(
        Panel.fit(
            f"[bold]Endpoint:[/] {CONFIG.base_url}\n"
            f"[bold]Model:[/] {CONFIG.model or '(auto-detect)'}\n"
            f"[bold]Max depth:[/] {CONFIG.max_depth}\n"
            f"[bold]Max turns/agent:[/] {CONFIG.max_turns_per_agent}",
            title="TaskGenie",
            border_style="cyan",
        )
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


def _read_unblock_input(console: Console) -> str | None:
    console.print(
        "[dim]Reply with guidance to resume, or empty/'abandon' to stop.[/]"
    )
    try:
        response = console.input("[bold yellow]you > [/] ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if response.lower() in ABANDON_TOKENS:
        return None
    return response


async def _run_session(ceo: Agent, tui: TUI, message: str) -> RunResult:
    ceo_task = asyncio.create_task(ceo.run(message), name="ceo")
    tui_task = asyncio.create_task(tui.run(stop_when=ceo_task), name="tui")
    try:
        result = await ceo_task
    finally:
        await tui_task
    return result


async def _run(task: str) -> int:
    console = Console()
    _print_banner(console)

    ceo = Agent(name="CEO", role_type="ceo", system_prompt=CEO_ROLE_PROMPT)
    tui = TUI(task=task)
    message = task

    while True:
        try:
            result = await _run_session(ceo, tui, message)
        except KeyboardInterrupt:
            console.print("[yellow]interrupted[/]")
            return 130

        if result.kind == "done":
            console.print(Rule(style="green"))
            console.print(
                Panel(
                    Markdown(result.report or "(no answer)"),
                    title="Final answer",
                    border_style="green",
                )
            )
            return 0

        if result.kind == "blocked":
            _print_blocker(console, result)
            user_response = _read_unblock_input(console)
            if user_response is None:
                console.print("[yellow]Task abandoned.[/]")
                console.print(
                    Panel(
                        result.partial_progress or "(no partial progress reported)",
                        title="Partial progress",
                        border_style="yellow",
                    )
                )
                return 2
            message = (
                "USER UNBLOCK: " + user_response + "\n\n"
                "Resume your work using your existing subordinates and memory. "
                "You may hire new agents, rehire existing ones, or re-assign with "
                "the new context. Call `complete` when you have the final answer."
            )
            continue

        # exhausted / error
        console.print(
            Panel(
                f"[red]{result.kind.upper()}[/]\n\n{result.error or '(no detail)'}",
                title="Run ended",
                border_style="red",
            )
        )
        return 1


def cli() -> None:
    parser = argparse.ArgumentParser(
        prog="taskgenie",
        description="A hierarchical multi-agent system where managers hire workers by writing role prompts.",
    )
    parser.add_argument("task", nargs="+", help="The task to give the CEO.")
    args = parser.parse_args()
    task = " ".join(args.task)
    sys.exit(asyncio.run(_run(task)))


if __name__ == "__main__":
    cli()
