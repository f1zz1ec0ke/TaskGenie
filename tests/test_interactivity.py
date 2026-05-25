"""Mid-task interactivity:

1. CEO calls `ask_user` -> main loop pauses, prompts, resumes with the answer.
2. After completion, final.{md,html,json} artifacts are written into the
   session dir and an extra --output copy lands at a user-chosen path.

Run with: python -m tests.test_interactivity
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rich.console import Console

from taskgenie import __main__ as cli_module
from taskgenie import agent as agent_module
from taskgenie.agent import Agent
from taskgenie.artifacts import write_final
from taskgenie.config import CONFIG
from taskgenie.prompts import CEO_ROLE_PROMPT
from taskgenie.result import RunResult
from taskgenie.session import Session, PROJECTS_ROOT_ENV

from tests.test_loop import ScriptedLLM, tc


class AskUserLLM(ScriptedLLM):
    """CEO asks the user a clarifying question about output format,
    then completes after receiving the answer."""

    def _respond(self, who, turn, messages):
        if turn == 0:
            return {
                "content": "Before I delegate, I need to know the desired format.",
                "tool_calls": [
                    tc(
                        "c-q",
                        "ask_user",
                        question="Should the final answer be a polished markdown report or a structured JSON?",
                        options=["markdown report", "structured JSON"],
                        context="The output format determines how I structure the team's work.",
                    )
                ],
            }
        if turn == 1:
            # After receiving the user's answer, just complete.
            user_msg = messages[-1]["content"]
            assert "USER ANSWER" in user_msg
            return {
                "content": "Got it. Producing the markdown report.",
                "tool_calls": [
                    tc(
                        "c-c",
                        "complete",
                        report=(
                            "# Final report\n\nUser asked for markdown. Here it is.\n\n"
                            "## Process retrospective\n\nClarification saved time."
                        ),
                    )
                ],
            }
        return {"content": "?", "tool_calls": [tc("x", "complete", report="?")]}


def _reset(mock_llm) -> None:
    Agent.REGISTRY.clear()
    agent_module.LLM = mock_llm  # type: ignore[assignment]


async def test_ask_user_round_trip(tmp_path: Path) -> None:
    """Drive _drive_session with a scripted user answer; verify pause->ask->resume->done."""
    _reset(AskUserLLM())

    # Point session/artifact files at a temp dir.
    object.__setattr__(CONFIG, "data_dir", str(tmp_path))
    CONFIG.sessions_dir.mkdir(parents=True, exist_ok=True)
    os.environ[PROJECTS_ROOT_ENV] = str(tmp_path / "projects")

    session = Session.create("test interactivity task")
    Agent.SCRATCH_ROOT = str(session.scratch_root)
    ceo = Agent(name="CEO", role_type="ceo", system_prompt=CEO_ROLE_PROMPT)

    # Capture console output silently; feed the answer via stdin.
    console = Console(file=io.StringIO(), force_terminal=False)
    answers_io = io.StringIO("markdown report\n")
    cli_module.Console = lambda *a, **kw: console  # type: ignore[assignment]

    # Patch _read_answer_input to consume from answers_io rather than blocking.
    real_read_answer = cli_module._read_answer_input

    def fake_read_answer(_console, _options):
        return answers_io.readline().strip() or None

    cli_module._read_answer_input = fake_read_answer  # type: ignore[assignment]

    # Stub MCP client + logger to no-ops.
    class _NoopMCP:
        registered_tools: list[str] = []
        connect_errors: dict[str, str] = {}

        async def close(self) -> None:
            return None

    extra_output = tmp_path / "report-copy.html"

    from taskgenie.tui import TUI

    try:
        rc = await cli_module._drive_session(
            console=console,
            session=session,
            mcp=_NoopMCP(),
            ceo=ceo,
            tui=TUI(task=session.data.task),
            first_message=session.data.task,
            logger=None,
            extra_output=extra_output,
        )
    finally:
        cli_module._read_answer_input = real_read_answer  # type: ignore[assignment]

    assert rc == 0
    # Session ends marked done.
    assert session.data.status == "done"
    # History contains a question then an answer.
    kinds = [h["kind"] for h in session.data.history]
    assert "question" in kinds and "answer" in kinds
    # Answer text propagated.
    answer_entry = next(h for h in session.data.history if h["kind"] == "answer")
    assert "markdown report" in answer_entry["text"]

    # Artifacts written into the session dir.
    md_path = session.root_dir / "final.md"
    html_path = session.root_dir / "final.html"
    json_path = session.root_dir / "final.json"
    assert md_path.exists() and html_path.exists() and json_path.exists()
    assert "Final report" in md_path.read_text()
    assert "<!doctype html>" in html_path.read_text()[:80]
    bundle = json.loads(json_path.read_text())
    assert bundle["task"] == "test interactivity task"
    assert "Final report" in bundle["report"]
    assert any(a["name"] == "CEO" for a in bundle["agents"])

    # Extra --output copy: HTML extension means HTML content.
    assert extra_output.exists()
    assert "<!doctype html>" in extra_output.read_text()[:80]

    print("[ask_user_round_trip] OK")


def test_artifact_extensions(tmp_path: Path) -> None:
    """write_final infers format from --output extension: .md / .html / .json / fallback."""
    object.__setattr__(CONFIG, "data_dir", str(tmp_path))
    CONFIG.sessions_dir.mkdir(parents=True, exist_ok=True)
    os.environ[PROJECTS_ROOT_ENV] = str(tmp_path / "projects")
    Agent.REGISTRY.clear()
    session = Session.create("artifact extension task")
    Agent.SCRATCH_ROOT = str(session.scratch_root)
    ceo = Agent(name="CEO", role_type="ceo", system_prompt=CEO_ROLE_PROMPT)
    report = "# Hello\n\nThis is the report.\n"

    # .md target
    md_target = tmp_path / "out.md"
    paths = write_final(session, ceo, report, stats=None, extra_output=md_target)
    assert paths["extra"] == md_target and md_target.exists()
    assert "Hello" in md_target.read_text()

    # .html target
    html_target = tmp_path / "out.html"
    paths = write_final(session, ceo, report, stats=None, extra_output=html_target)
    assert paths["extra"] == html_target
    assert "<!doctype html>" in html_target.read_text()[:80]
    assert "<h1" in html_target.read_text()

    # .json target
    json_target = tmp_path / "out.json"
    paths = write_final(session, ceo, report, stats=None, extra_output=json_target)
    bundle = json.loads(json_target.read_text())
    assert bundle["report"] == report

    # No extension -> .md appended
    bare_target = tmp_path / "out_no_ext"
    paths = write_final(session, ceo, report, stats=None, extra_output=bare_target)
    expected = bare_target.with_suffix(".md")
    assert expected.exists()
    assert paths["extra"] == expected

    print("[artifact_extensions] OK")


async def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        await test_ask_user_round_trip(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_artifact_extensions(Path(d))
    print("\nAll interactivity tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
