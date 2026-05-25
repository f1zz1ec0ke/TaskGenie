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
from taskgenie.tui import TUI


async def _run(task: str) -> int:
    console = Console()
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

    ceo = Agent(name="CEO", role_type="ceo", system_prompt=CEO_ROLE_PROMPT)
    ceo_task = asyncio.create_task(ceo.run(task), name="ceo")
    tui = TUI(task=task)
    tui_task = asyncio.create_task(tui.run(stop_when=ceo_task), name="tui")

    try:
        final = await ceo_task
    except KeyboardInterrupt:
        console.print("[yellow]interrupted[/]")
        ceo_task.cancel()
        await asyncio.gather(tui_task, return_exceptions=True)
        return 130

    await tui_task

    console.print(Rule(style="green"))
    console.print(Panel(Markdown(final or "(no answer)"), title="Final answer", border_style="green"))
    return 0


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
