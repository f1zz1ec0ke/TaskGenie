"""Multi-turn interactive session: CEO completes, user gives a follow-up,
CEO completes again. Exercise the /done, /status, /retry, /reset commands.

Run with: python -m tests.test_followup_loop
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
from taskgenie.config import CONFIG
from taskgenie.prompts import CEO_ROLE_PROMPT
from taskgenie.session import Session, PROJECTS_ROOT_ENV
from taskgenie.tui import TUI

from tests.test_loop import ScriptedLLM, tc


class TwoTurnLLM(ScriptedLLM):
    """CEO completes twice -- once on the initial task, once on a follow-up.
    Tracks each completion so the test can assert both turns happened."""

    def __init__(self) -> None:
        super().__init__()
        self.complete_reports: list[str] = []

    def _respond(self, who, turn, messages):
        if turn == 0:
            return {
                "content": "Initial task. Doing it now.",
                "tool_calls": [
                    tc(
                        "c1",
                        "complete",
                        report="# Initial answer\n\nDone.\n\n## Process retrospective\n\nClean first run.",
                    )
                ],
            }
        if turn == 1:
            # By turn 1 we should be processing a follow-up. Verify the
            # follow-up text reached the CEO's history.
            last_user = messages[-1]["content"]
            assert "USER FOLLOW-UP" in last_user
            assert "make it harder" in last_user.lower()
            return {
                "content": "Applying follow-up.",
                "tool_calls": [
                    tc(
                        "c2",
                        "complete",
                        report="# Updated answer\n\nMade it harder.\n\n## Process retrospective\n\nClean second turn.",
                    )
                ],
            }
        return {"content": "?", "tool_calls": [tc("x", "complete", report="?")]}


class _NoopMCP:
    registered_tools: list[str] = []
    connect_errors: dict[str, str] = {}

    async def close(self) -> None:
        return None


def _setup_session(tmp_path: Path) -> Session:
    object.__setattr__(CONFIG, "data_dir", str(tmp_path / "data"))
    CONFIG.sessions_dir.mkdir(parents=True, exist_ok=True)
    os.environ[PROJECTS_ROOT_ENV] = str(tmp_path / "projects")
    Agent.REGISTRY.clear()
    return Session.create("multi-turn task")


async def test_followup_round_trip(tmp_path: Path) -> None:
    """CEO completes -> user gives follow-up -> CEO completes again -> /done."""
    session = _setup_session(tmp_path)
    Agent.SCRATCH_ROOT = str(session.scratch_root)
    Agent.WORKSPACE_ROOT = str(session.project_dir)
    mock = TwoTurnLLM()
    agent_module.LLM = mock  # type: ignore[assignment]
    ceo = Agent(name="CEO", role_type="ceo", system_prompt=CEO_ROLE_PROMPT)

    console = Console(file=io.StringIO(), force_terminal=False)
    # Two reads: the follow-up text, then /done.
    inputs = iter(["make it harder", "/done"])

    def fake_read_followup(_console):
        try:
            return next(inputs)
        except StopIteration:
            return None

    real = cli_module._read_followup_input
    cli_module._read_followup_input = fake_read_followup  # type: ignore[assignment]
    try:
        rc = await cli_module._drive_session(
            console=console,
            session=session,
            mcp=_NoopMCP(),
            ceo=ceo,
            tui=TUI(task=session.data.task),
            first_message=session.data.task,
            logger=None,
            extra_output=None,
        )
    finally:
        cli_module._read_followup_input = real  # type: ignore[assignment]

    assert rc == 0
    # Session history should record both done events and the followup
    # (the LLM mock doesn't append to its own complete_reports list,
    # so the canonical record is the persisted history -- check that).
    kinds = [h["kind"] for h in session.data.history]
    assert kinds.count("done") >= 2
    assert "followup" in kinds
    followup_entry = next(h for h in session.data.history if h["kind"] == "followup")
    assert "make it harder" in followup_entry["text"]
    # Final state is done (the second completion)
    assert session.data.status == "done"
    # Artifacts on disk reflect the LATEST report
    md_path = session.root_dir / "final.md"
    assert md_path.exists()
    assert "Updated answer" in md_path.read_text(encoding="utf-8")
    print("[followup_round_trip] OK")


async def test_retry_replays_last_followup(tmp_path: Path) -> None:
    """/retry without typing reuses the previous follow-up."""
    session = _setup_session(tmp_path)
    Agent.SCRATCH_ROOT = str(session.scratch_root)
    Agent.WORKSPACE_ROOT = str(session.project_dir)

    # Use a 3-completion scripted LLM. Each turn just completes.
    class ThreeTurnLLM(ScriptedLLM):
        def _respond(self, who, turn, messages):
            return {
                "content": f"turn {turn}",
                "tool_calls": [
                    tc(f"c{turn}", "complete", report=f"turn-{turn} report")
                ],
            }

    agent_module.LLM = ThreeTurnLLM()  # type: ignore[assignment]
    ceo = Agent(name="CEO", role_type="ceo", system_prompt=CEO_ROLE_PROMPT)

    console = Console(file=io.StringIO(), force_terminal=False)
    inputs = iter(["tweak A", "/retry", "/done"])
    cli_module._read_followup_input = lambda _c: next(inputs, None)  # type: ignore[assignment]

    rc = await cli_module._drive_session(
        console=console,
        session=session,
        mcp=_NoopMCP(),
        ceo=ceo,
        tui=TUI(task=session.data.task),
        first_message=session.data.task,
        logger=None,
        extra_output=None,
    )
    assert rc == 0
    # History should have 2 follow-up entries (the original "tweak A" + the /retry replay)
    followups = [h for h in session.data.history if h["kind"] == "followup"]
    assert len(followups) == 2
    assert followups[0]["text"] == "tweak A"
    assert followups[1]["text"] == "tweak A"  # /retry replays the same text
    print("[retry_replays_last_followup] OK")


async def test_reset_fires_team_but_keeps_ceo(tmp_path: Path) -> None:
    """/reset fires every subordinate, records history, keeps CEO + workspace."""
    session = _setup_session(tmp_path)
    Agent.SCRATCH_ROOT = str(session.scratch_root)
    Agent.WORKSPACE_ROOT = str(session.project_dir)
    Agent.REGISTRY.clear()
    ceo = Agent(name="CEO", role_type="ceo", system_prompt="...")
    mgr = Agent(
        name="Mgr",
        role_type="manager",
        system_prompt="...",
        parent=ceo,
        depth=1,
    )
    ceo.children.append(mgr)
    worker = Agent(
        name="Worker",
        role_type="worker",
        system_prompt="...",
        parent=mgr,
        depth=2,
    )
    mgr.children.append(worker)

    # Put a file in the workspace -- /reset must NOT delete it
    (session.project_dir / "keepme.txt").write_text("preserved", encoding="utf-8")

    console = Console(file=io.StringIO(), force_terminal=False)
    fired = cli_module._reset_team(session, ceo, console)

    assert fired == 2  # mgr + worker
    assert ceo.status != "fired"
    assert mgr.status == "fired"
    assert worker.status == "fired"
    # CEO's child list cleared (no live team)
    assert ceo.children == []
    # CEO got a reset notice in their message history
    last_msg = ceo.messages[-1]
    assert last_msg["role"] == "user"
    assert "/reset" in last_msg["content"]
    # Workspace files preserved
    assert (session.project_dir / "keepme.txt").read_text(encoding="utf-8") == "preserved"
    # Session history recorded the reset
    reset_entries = [h for h in session.data.history if h["kind"] == "reset"]
    assert len(reset_entries) == 1
    assert reset_entries[0]["payload"]["fired_count"] == 2
    print("[reset_fires_team_but_keeps_ceo] OK")


async def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        await test_followup_round_trip(Path(d))
    with tempfile.TemporaryDirectory() as d:
        await test_retry_replays_last_followup(Path(d))
    with tempfile.TemporaryDirectory() as d:
        await test_reset_fires_team_but_keeps_ceo(Path(d))
    print("\nAll follow-up loop tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
