"""Persistence test: blocker -> snapshot -> reload -> resume -> done.

Exercises the full save/load cycle so a real user can quit during a blocker,
restart the process, and resume from the saved state. Also exercises the
tool-diff message when the registry changes between save and load.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from taskgenie import agent as agent_module
from taskgenie.agent import Agent
from taskgenie.config import CONFIG
from taskgenie.prompts import CEO_ROLE_PROMPT
from taskgenie.session import Session
from taskgenie.tools.registry import REGISTRY

# Reuse the EscalationLLM from test_loop so we get the same blocker chain.
from tests.test_loop import EscalationLLM


def _reset(mock_llm, data_dir):
    Agent.REGISTRY.clear()
    Agent.SCRATCH_ROOT = str(Path(data_dir) / "scratch")
    agent_module.LLM = mock_llm  # type: ignore[assignment]


async def test_save_load_resume() -> None:
    tmp = tempfile.mkdtemp(prefix="tg-test-")
    os.environ["TASKGENIE_PROJECTS_DIR"] = tmp + "/projects"
    # Point the shared CONFIG singleton at the temp dir by MUTATING the
    # existing object (every test module holds this same reference).
    # Do NOT reload() the config/session modules: a reload swaps in a
    # brand-new CONFIG that session.py then uses, while other test
    # modules keep the old one -- their object.__setattr__(CONFIG, ...)
    # mutations stop reaching Session.load and they fail with spurious
    # FileNotFoundError. _projects_root() reads its env var dynamically,
    # so TASKGENIE_PROJECTS_DIR alone is enough for the projects path.
    original_data_dir = CONFIG.data_dir
    object.__setattr__(CONFIG, "data_dir", tmp)
    S = Session

    try:
        # --- Phase 1: original run ends in a CEO blocker.
        _reset(EscalationLLM(), tmp)
        session = S.create("Analyze something needing code execution.")
        ceo = Agent(
            name="CEO",
            role_type="ceo",
            system_prompt=CEO_ROLE_PROMPT,
        )
        result = await ceo.run("Analyze something needing code execution.")
        assert result.kind == "blocked", result

        # Persist snapshot like __main__ would.
        session.mark_blocked(
            {
                "reason": result.reason,
                "needs": result.needs,
                "partial_progress": result.partial_progress,
            }
        )
        session.set_ceo(ceo.id)
        session.snapshot_agents({aid: a.to_dict() for aid, a in Agent.REGISTRY.items()})
        session.save()
        original_session_id = session.session_id
        original_ceo_id = ceo.id

        assert session.state_path.exists()
        state_on_disk = json.loads(session.state_path.read_text())
        assert state_on_disk["status"] == "blocked"
        assert state_on_disk["ceo_id"] == original_ceo_id
        assert len(state_on_disk["agents"]) == 3  # CEO, AnalysisMgr, Quant

        # --- Phase 2: simulate process restart — wipe registry, reload.
        Agent.REGISTRY.clear()
        reloaded = S.load(original_session_id)
        assert reloaded.data.status == "blocked"
        assert reloaded.data.task == "Analyze something needing code execution."

        Agent.bulk_load(reloaded.data.agents)
        assert len(Agent.REGISTRY) == 3
        ceo2 = Agent.REGISTRY[reloaded.data.ceo_id]
        assert ceo2.name == "CEO"
        assert ceo2.status == "blocked"
        # Subordinates should also be back with their state.
        mgr = next(a for a in Agent.REGISTRY.values() if a.name == "AnalysisMgr")
        worker = next(a for a in Agent.REGISTRY.values() if a.name == "Quant")
        assert mgr.status == "blocked"
        assert worker.status == "blocked"
        # Parent links restored
        assert ceo2.children[0].name == "AnalysisMgr"
        assert mgr.parent is ceo2
        assert worker.parent is mgr

        # Messages preserved
        assert len(ceo2.messages) > 1
        assert any(m["role"] == "user" for m in ceo2.messages)

        # --- Phase 3: resume — user provides guidance, CEO completes.
        # New scripted LLM with same script; resume re-uses the message history.
        agent_module.LLM = EscalationLLM()  # type: ignore[assignment]

        # Mirror __main__'s build_unblock_message
        unblock_msg = (
            "USER UNBLOCK: skip the numeric analysis and report partial only.\n\n"
            "Resume your work using your existing subordinates and memory."
        )
        result2 = await ceo2.run(unblock_msg)
        assert result2.kind == "done", result2
        assert "FINAL ANSWER" in result2.report

        reloaded.mark_done(result2.report)
        reloaded.snapshot_agents({aid: a.to_dict() for aid, a in Agent.REGISTRY.items()})
        reloaded.save()
        final_disk = json.loads(reloaded.state_path.read_text())
        assert final_disk["status"] == "done"

        print("[save_load_resume] OK")
    finally:
        object.__setattr__(CONFIG, "data_dir", original_data_dir)
        shutil.rmtree(tmp, ignore_errors=True)
        os.environ.pop("TASKGENIE_PROJECTS_DIR", None)


async def test_tool_diff_on_resume() -> None:
    """Capture a signature, mutate the registry, resume, and confirm the diff
    surfaces tools as added/removed/updated."""
    tmp = tempfile.mkdtemp(prefix="tg-tooldiff-")
    os.environ["TASKGENIE_PROJECTS_DIR"] = tmp + "/projects"
    # Mutate the shared CONFIG singleton instead of reload()-ing config
    # (see test_save_load_resume for why a reload poisons sibling tests).
    original_data_dir = CONFIG.data_dir
    object.__setattr__(CONFIG, "data_dir", tmp)
    S = Session
    try:
        session = S.create("test tool diff")
        session.save()  # captures the current tool signature

        # Add a fake new tool, mutate an existing one, remove another.
        fake_tool = {
            "type": "function",
            "function": {
                "name": "_fake_new_tool",
                "description": "added by the test",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        REGISTRY.register(fake_tool, kind="work", version="9.9", source="test")
        REGISTRY.unregister("exec_code")

        # Re-load saved session, diff against the now-modified registry.
        reloaded = S.load(session.session_id)
        diff = reloaded.diff_tools_against(
            REGISTRY.signature("management"),
            REGISTRY.signature("work"),
            REGISTRY.signature("ceo"),
        )
        assert diff is not None
        assert "_fake_new_tool" in diff and "new" in diff
        assert "exec_code" in diff and "removed" in diff

        # Cleanup the test mutations so other tests aren't poisoned.
        REGISTRY.unregister("_fake_new_tool")
        # Re-register exec_code with the CURRENT version. Hard-coding a
        # stale "0.1" here polluted cross-test state -- any subsequent
        # test inspecting the registry would see a fake older
        # exec_code. Round-three confirmed exec_code is at version 0.3.
        from taskgenie.tools.work import _EXEC_CODE_SCHEMA, _exec_code
        REGISTRY.register(
            _EXEC_CODE_SCHEMA, kind="work", version="0.3", dispatcher=_exec_code
        )
        print("[tool_diff_on_resume] OK")
    finally:
        object.__setattr__(CONFIG, "data_dir", original_data_dir)
        shutil.rmtree(tmp, ignore_errors=True)
        os.environ.pop("TASKGENIE_PROJECTS_DIR", None)


async def main() -> int:
    await test_save_load_resume()
    await test_tool_diff_on_resume()
    print("\nAll persistence tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
