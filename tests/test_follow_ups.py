"""Bottom-up follow-up suggestions: workers and managers can flag adjacent work
mid-task; the suggestions ride up with their final report and the parent
decides whether to act on them.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from taskgenie import agent as agent_module
from taskgenie.agent import Agent
from taskgenie.prompts import CEO_ROLE_PROMPT
from taskgenie.result import RunResult
from taskgenie.tui import TUI

from tests.test_loop import ScriptedLLM, tc, _last_tool_results


class FollowUpLLM(ScriptedLLM):
    """CEO -> Manager -> 2 workers. Worker A suggests a follow-up before
    completing. Manager relays it via its own suggest_follow_up before
    completing to CEO. CEO sees it in its await_results payload, narrates
    that it was raised, and completes."""

    def _respond(self, who, turn, messages):
        last_results = _last_tool_results(messages)
        if who == "CEO":
            return self._ceo(turn, last_results)
        if who == "Mgr":
            return self._mgr(turn, last_results)
        if who.startswith("Alpha"):
            return self._alpha(turn)
        if who.startswith("Beta"):
            return self._beta(turn)
        return {"content": "?", "tool_calls": [tc("x", "complete", report="?")]}

    def _ceo(self, turn, last_results):
        if turn == 0:
            return {
                "content": "Hiring a manager.",
                "tool_calls": [
                    tc("c-1", "hire", name="Mgr", role_type="manager", role_prompt="Run analysis.")
                ],
            }
        if turn == 1:
            mgr_id = json.loads(last_results[0][1])["agent_id"]
            return {
                "content": "Assigning.",
                "tool_calls": [tc("c-2", "assign", agent_id=mgr_id, task="Do the work.")],
            }
        if turn == 2:
            handle = json.loads(last_results[0][1])["task_handle"]
            return {
                "content": "Awaiting.",
                "tool_calls": [tc("c-3", "await_results", task_handles=[handle])],
            }
        if turn == 3:
            payload = json.loads(last_results[0][1])
            result = list(payload.values())[0]
            follow_ups = result.get("follow_ups", [])
            note = ""
            if follow_ups:
                note = (
                    " Follow-ups raised: "
                    + "; ".join(fu.get("what", "") for fu in follow_ups)
                )
            return {
                "content": "Wrapping up.",
                "tool_calls": [
                    tc(
                        "c-4",
                        "complete",
                        report=f"FINAL ANSWER:\n{result.get('report', '')}{note}",
                    )
                ],
            }
        return {"content": "stuck", "tool_calls": [tc("cx", "complete", report="stuck")]}

    def _mgr(self, turn, last_results):
        if turn == 0:
            return {
                "content": "Hire Alpha.",
                "tool_calls": [
                    tc("m-1", "hire", name="Alpha", role_type="worker", role_prompt="Pull data.")
                ],
            }
        if turn == 1:
            return {
                "content": "Hire Beta.",
                "tool_calls": [
                    tc("m-2", "hire", name="Beta", role_type="worker", role_prompt="Pull data.")
                ],
            }
        if turn == 2:
            mgr = next(a for a in Agent.REGISTRY.values() if a.name == "Mgr")
            alpha_id = next(c.id for c in mgr.children if c.name == "Alpha")
            beta_id = next(c.id for c in mgr.children if c.name == "Beta")
            return {
                "content": "Dispatch.",
                "tool_calls": [
                    tc("m-3a", "assign", agent_id=alpha_id, task="A"),
                    tc("m-3b", "assign", agent_id=beta_id, task="B"),
                ],
            }
        if turn == 3:
            handles = [json.loads(c)["task_handle"] for _, c in last_results]
            return {
                "content": "Awaiting.",
                "tool_calls": [tc("m-4", "await_results", task_handles=handles)],
            }
        if turn == 4:
            payload = json.loads(last_results[0][1])
            # Aggregate any follow_ups from subordinates.
            aggregated = []
            for v in payload.values():
                for fu in v.get("follow_ups", []) or []:
                    aggregated.append(fu)
            # Relay one upward via the manager's own suggest_follow_up before completing.
            calls = []
            for i, fu in enumerate(aggregated):
                calls.append(
                    tc(
                        f"m-fu-{i}",
                        "suggest_follow_up",
                        what_is_needed=f"(relayed) {fu.get('what', '')}",
                        rationale=f"From my team: {fu.get('rationale', '')}",
                    )
                )
            return {
                "content": "Relaying follow-ups before completing.",
                "tool_calls": calls or [tc("m-5", "complete", report="(no follow-ups)")],
            }
        if turn == 5:
            return {
                "content": "Completing.",
                "tool_calls": [
                    tc(
                        "m-5",
                        "complete",
                        report="Synthesis: Alpha + Beta findings combined.",
                    )
                ],
            }
        return {"content": "stuck", "tool_calls": [tc("mx", "complete", report="stuck")]}

    def _alpha(self, turn):
        if turn == 0:
            return {
                "content": "Noticed something adjacent. Buffering a follow-up first.",
                "tool_calls": [
                    tc(
                        "a-fu",
                        "suggest_follow_up",
                        what_is_needed="Research adjacent topic X",
                        rationale="Came up repeatedly in my sources",
                    )
                ],
            }
        if turn == 1:
            return {
                "content": "Now completing my actual task.",
                "tool_calls": [tc("a-c", "complete", report="Alpha report body.")],
            }
        return {"content": "?", "tool_calls": [tc("ax", "complete", report="?")]}

    def _beta(self, turn):
        return {
            "content": "Beta on it.",
            "tool_calls": [tc("b-c", "complete", report="Beta report body.")],
        }


def _reset(mock_llm):
    Agent.REGISTRY.clear()
    agent_module.LLM = mock_llm  # type: ignore[assignment]


async def test_follow_up_propagation() -> None:
    _reset(FollowUpLLM())
    ceo = Agent(name="CEO", role_type="ceo", system_prompt=CEO_ROLE_PROMPT)
    ceo_task = asyncio.create_task(ceo.run("test task"))
    tui_task = asyncio.create_task(TUI(task="test task").run(stop_when=ceo_task))
    result = await ceo_task
    await tui_task

    assert isinstance(result, RunResult)
    assert result.kind == "done", result

    # Alpha buffered one follow-up; verify it persisted on the agent and
    # rode up through the manager via its own suggest_follow_up.
    alpha = next(a for a in Agent.REGISTRY.values() if a.name == "Alpha")
    assert len(alpha.follow_ups) == 1
    assert "adjacent topic X" in alpha.follow_ups[0]["what"]

    mgr = next(a for a in Agent.REGISTRY.values() if a.name == "Mgr")
    # Mgr relayed Alpha's suggestion as its own (prefixed with "(relayed)").
    assert len(mgr.follow_ups) == 1
    assert "(relayed)" in mgr.follow_ups[0]["what"]
    assert "adjacent topic X" in mgr.follow_ups[0]["what"]

    # CEO sees the relayed follow-up in its await_results payload and
    # mentions it in the final report.
    assert "Follow-ups raised" in result.report
    assert "adjacent topic X" in result.report
    print("[follow_up_propagation] OK")


async def main() -> int:
    await test_follow_up_propagation()
    print("\nAll follow-up tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
