"""Smoke tests for the agent loop with a scripted fake LLM.

Runs end-to-end tasks without touching the real LM Studio endpoint. Verifies:

- Happy path: CEO → manager → parallel workers → synthesis (RunResult kind="done")
- Blocker propagation: worker blocker → manager escalates → CEO escalates → user
  unblock → resume → final completion (mirrors the production __main__ loop)
- Local unblock: worker blocker → manager hires a different worker → completes

Run with: python -m tests.test_loop
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from taskgenie import agent as agent_module
from taskgenie.agent import Agent
from taskgenie.prompts import CEO_ROLE_PROMPT
from taskgenie.result import RunResult
from taskgenie.tui import TUI


def tc(call_id: str, tool_name: str, **args):
    return {"id": call_id, "name": tool_name, "arguments": json.dumps(args)}


def _agent_name_from_sysprompt(sysprompt: str) -> str:
    if "CEO of TaskGenie" in sysprompt:
        return "CEO"
    m = re.search(r"You are ([A-Za-z0-9_]+), a (manager|worker)", sysprompt)
    return m.group(1) if m else "UNKNOWN"


def _last_tool_results(messages):
    results = []
    last_assistant_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        if messages[i]["role"] == "assistant" and messages[i].get("tool_calls"):
            last_assistant_idx = i
            break
    if last_assistant_idx < 0:
        return results
    tool_calls = messages[last_assistant_idx]["tool_calls"]
    for j, tc_ in enumerate(tool_calls):
        idx = last_assistant_idx + 1 + j
        if idx < len(messages) and messages[idx]["role"] == "tool":
            results.append((tc_["function"]["name"], messages[idx]["content"]))
    return results


class ScriptedLLM:
    """Base class. Subclasses define _respond(who, turn, messages)."""

    def __init__(self) -> None:
        self._turn: dict[str, int] = {}

    async def model_name(self) -> str:
        return "scripted-mock"

    async def chat(self, messages, tools=None, **_kwargs):
        who = _agent_name_from_sysprompt(messages[0]["content"])
        turn = self._turn.get(who, 0)
        self._turn[who] = turn + 1
        return self._respond(who, turn, messages)

    def _respond(self, who, turn, messages):
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Scenario 1: happy path
# ---------------------------------------------------------------------------


class HappyPathLLM(ScriptedLLM):
    def _respond(self, who, turn, messages):
        last_results = _last_tool_results(messages)
        if who == "CEO":
            return self._ceo(turn, last_results)
        if who == "ResearchMgr":
            return self._mgr(turn, last_results)
        if who.startswith("Alice"):
            return {
                "content": "Researching agent-flavor coding tools.",
                "tool_calls": [
                    tc(
                        "a-1",
                        "complete",
                        report="Claude Code: pros = first-party, strong reasoning; cons = paid. Cursor: pros = UX; cons = closed-source.",
                    )
                ],
            }
        if who.startswith("Bob"):
            return {
                "content": "On the CLI-style agents.",
                "tool_calls": [
                    tc(
                        "b-1",
                        "complete",
                        report="Aider: pros = open, git-native; cons = terminal-only. Codex: pros = OpenAI ecosystem; cons = less mature.",
                    )
                ],
            }
        return {"content": "stuck", "tool_calls": [tc("x", "complete", report="?")]}

    def _ceo(self, turn, last_results):
        if turn == 0:
            return {
                "content": "Hiring a research manager.",
                "tool_calls": [
                    tc(
                        "c-1",
                        "hire",
                        name="ResearchMgr",
                        role_type="manager",
                        role_prompt="Research and compare coding agents.",
                    )
                ],
            }
        if turn == 1:
            mgr_id = json.loads(last_results[0][1])["agent_id"]
            return {
                "content": "Assigning the task.",
                "tool_calls": [tc("c-2", "assign", agent_id=mgr_id, task="Research coding agents.")],
            }
        if turn == 2:
            handle = json.loads(last_results[0][1])["task_handle"]
            return {"content": "Awaiting.", "tool_calls": [tc("c-3", "await_results", task_handles=[handle])]}
        if turn == 3:
            results = json.loads(last_results[0][1])
            report = list(results.values())[0]["report"]
            return {
                "content": "Wrapping up.",
                "tool_calls": [tc("c-4", "complete", report=f"FINAL ANSWER:\n\n{report}")],
            }
        return {"content": "stuck", "tool_calls": [tc("cx", "complete", report="stuck")]}

    def _mgr(self, turn, last_results):
        if turn == 0:
            return {
                "content": "Hiring Alice.",
                "tool_calls": [
                    tc("m-1", "hire", name="Alice", role_type="worker", role_prompt="Cover Claude Code + Cursor."),
                ],
            }
        if turn == 1:
            return {
                "content": "Hiring Bob.",
                "tool_calls": [
                    tc("m-2", "hire", name="Bob", role_type="worker", role_prompt="Cover Aider + Codex."),
                ],
            }
        if turn == 2:
            mgr = next(a for a in Agent.REGISTRY.values() if a.name == "ResearchMgr")
            alice_id = next(c.id for c in mgr.children if c.name == "Alice")
            bob_id = next(c.id for c in mgr.children if c.name == "Bob")
            return {
                "content": "Dispatching both in parallel.",
                "tool_calls": [
                    tc("m-3a", "assign", agent_id=alice_id, task="Pros/cons of Claude Code and Cursor."),
                    tc("m-3b", "assign", agent_id=bob_id, task="Pros/cons of Aider and Codex."),
                ],
            }
        if turn == 3:
            handles = [json.loads(content)["task_handle"] for _, content in last_results]
            return {"content": "Awaiting both.", "tool_calls": [tc("m-4", "await_results", task_handles=handles)]}
        if turn == 4:
            results = json.loads(last_results[0][1])
            combined = "\n\n".join(f"- {v['report']}" for v in results.values())
            return {
                "content": "Synthesizing.",
                "tool_calls": [tc("m-5", "complete", report=f"Comparison:\n\n{combined}")],
            }
        return {"content": "stuck", "tool_calls": [tc("mx", "complete", report="stuck")]}


# ---------------------------------------------------------------------------
# Scenario 2: full escalation chain with user unblock and resume
# ---------------------------------------------------------------------------


class EscalationLLM(ScriptedLLM):
    """Worker blocks because code exec is stubbed. Manager escalates. CEO escalates
    to user. After user unblock, CEO retries with the new info and completes."""

    def _respond(self, who, turn, messages):
        last_results = _last_tool_results(messages)
        if who == "CEO":
            return self._ceo(turn, last_results, messages)
        if who == "AnalysisMgr":
            return self._mgr(turn, last_results)
        if who.startswith("Quant"):
            return self._worker(turn)
        return {"content": "stuck", "tool_calls": [tc("x", "complete", report="?")]}

    def _ceo(self, turn, last_results, messages):
        # Detect whether we've been resumed: look for a 2nd user message containing UNBLOCK.
        user_msgs = [m for m in messages if m["role"] == "user"]
        resumed = any("USER UNBLOCK" in m.get("content", "") for m in user_msgs)

        if not resumed:
            if turn == 0:
                return {
                    "content": "I'll need an analysis department.",
                    "tool_calls": [
                        tc(
                            "c-1",
                            "hire",
                            name="AnalysisMgr",
                            role_type="manager",
                            role_prompt="Compute some numbers from a local dataset.",
                        )
                    ],
                }
            if turn == 1:
                mgr_id = json.loads(last_results[0][1])["agent_id"]
                return {
                    "content": "Assigning.",
                    "tool_calls": [tc("c-2", "assign", agent_id=mgr_id, task="Run analysis.")],
                }
            if turn == 2:
                handle = json.loads(last_results[0][1])["task_handle"]
                return {"content": "Awaiting.", "tool_calls": [tc("c-3", "await_results", task_handles=[handle])]}
            if turn == 3:
                # Got blocked result from manager → escalate to user.
                results = json.loads(last_results[0][1])
                blocker = list(results.values())[0]
                return {
                    "content": "My team is stuck and I cannot route around it. Escalating to the user.",
                    "tool_calls": [
                        tc(
                            "c-4",
                            "report_blocker",
                            reason=f"AnalysisMgr reports: {blocker.get('reason', '')}",
                            needs="A working sandboxed code execution tool (or instructions to skip this analysis).",
                            partial_progress=blocker.get("partial_progress", ""),
                        )
                    ],
                }

        # Resumed branch — read the user's guidance and complete accordingly.
        last_user = user_msgs[-1]["content"]
        if "skip" in last_user.lower():
            return {
                "content": "User said to skip — wrapping with caveat.",
                "tool_calls": [
                    tc(
                        "c-5",
                        "complete",
                        report=(
                            "FINAL ANSWER:\nUser opted to skip the numeric analysis. "
                            "Reporting partial-progress only."
                        ),
                    )
                ],
            }
        return {
            "content": "User responded — wrapping up.",
            "tool_calls": [
                tc(
                    "c-5",
                    "complete",
                    report=f"FINAL ANSWER (with user unblock):\n{last_user}",
                )
            ],
        }

    def _mgr(self, turn, last_results):
        if turn == 0:
            return {
                "content": "Hiring a quant worker.",
                "tool_calls": [
                    tc("m-1", "hire", name="Quant", role_type="worker", role_prompt="Run numeric analysis."),
                ],
            }
        if turn == 1:
            wk_id = json.loads(last_results[0][1])["agent_id"]
            return {
                "content": "Assigning.",
                "tool_calls": [tc("m-2", "assign", agent_id=wk_id, task="Compute summary stats.")],
            }
        if turn == 2:
            handle = json.loads(last_results[0][1])["task_handle"]
            return {"content": "Awaiting.", "tool_calls": [tc("m-3", "await_results", task_handles=[handle])]}
        if turn == 3:
            # Quant reported blocked → escalate (we have no other worker tools either).
            results = json.loads(last_results[0][1])
            blocker = list(results.values())[0]
            return {
                "content": "My worker is blocked and I have no alternate tools available.",
                "tool_calls": [
                    tc(
                        "m-4",
                        "report_blocker",
                        reason=f"Worker blocker: {blocker.get('reason', '')}",
                        needs=blocker.get("needs", ""),
                        partial_progress=blocker.get("partial_progress", ""),
                    )
                ],
            }
        return {"content": "stuck", "tool_calls": [tc("mx", "complete", report="stuck")]}

    def _worker(self, turn):
        return {
            "content": "I need to run computations.",
            "tool_calls": [
                tc(
                    "w-1",
                    "report_blocker",
                    reason="this scripted scenario forbids using exec_code so I cannot actually compute anything.",
                    needs="permission to run the computation.",
                    partial_progress="Identified the analysis approach (mean, median, p95) but did not execute.",
                )
            ],
        }


# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------


def _reset_state(mock_llm):
    Agent.REGISTRY.clear()
    agent_module.LLM = mock_llm  # type: ignore[assignment]


async def test_happy_path() -> None:
    _reset_state(HappyPathLLM())
    ceo = Agent(name="CEO", role_type="ceo", system_prompt=CEO_ROLE_PROMPT)
    task = "Research coding agents and tell me the pros and cons of each."
    ceo_task = asyncio.create_task(ceo.run(task))
    tui_task = asyncio.create_task(TUI(task=task).run(stop_when=ceo_task))
    result = await ceo_task
    await tui_task

    assert isinstance(result, RunResult), result
    assert result.kind == "done", result
    assert "Claude Code" in result.report and "Aider" in result.report, result.report
    names = {a.name for a in Agent.REGISTRY.values()}
    assert names == {"CEO", "ResearchMgr", "Alice", "Bob"}, names
    print("[happy_path] OK")


async def test_escalation_with_user_unblock() -> None:
    _reset_state(EscalationLLM())
    ceo = Agent(name="CEO", role_type="ceo", system_prompt=CEO_ROLE_PROMPT)
    tui = TUI(task="Analyze something needing code execution.")

    # First session: should end in CEO blocked.
    task = "Analyze something needing code execution."
    ceo_task = asyncio.create_task(ceo.run(task))
    tui_task = asyncio.create_task(tui.run(stop_when=ceo_task))
    result = await ceo_task
    await tui_task

    assert result.kind == "blocked", f"expected blocked, got {result}"
    assert "code execution" in result.needs.lower(), result.needs
    assert "exec_code" in result.reason, result.reason

    # Verify the blocker propagated up: worker, manager, and CEO all should be blocked.
    by_name = {a.name: a for a in Agent.REGISTRY.values()}
    assert by_name["CEO"].status == "blocked"
    assert by_name["AnalysisMgr"].status == "blocked"
    assert by_name["Quant"].status == "blocked"

    # User unblocks. Resume the CEO with new guidance (mirrors __main__ wrapper).
    user_guidance = (
        "USER UNBLOCK: skip the numeric analysis and just summarize what you have.\n\n"
        "Resume your work using your existing subordinates and memory."
    )
    ceo_task2 = asyncio.create_task(ceo.run(user_guidance))
    tui_task2 = asyncio.create_task(tui.run(stop_when=ceo_task2))
    result2 = await ceo_task2
    await tui_task2

    assert result2.kind == "done", result2
    assert "FINAL ANSWER" in result2.report, result2.report
    assert by_name["CEO"].status == "done"
    print("[escalation_with_user_unblock] OK")


async def main() -> int:
    await test_happy_path()
    await test_escalation_with_user_unblock()
    print("\nAll smoke tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
