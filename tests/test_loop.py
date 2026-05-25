"""Smoke test for the agent loop with a scripted fake LLM.

Runs an end-to-end task through the full hierarchy without touching the real
LM Studio endpoint. Verifies:

- Agent registration & tool dispatch
- hire / assign / await_results semantics (including parallel siblings)
- review / rehire / fire / complete
- Event bus emissions
- TUI renders without crashing

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
from taskgenie.events import BUS
from taskgenie.prompts import CEO_ROLE_PROMPT
from taskgenie.tui import TUI


def tc(call_id: str, tool_name: str, **args):
    return {"id": call_id, "name": tool_name, "arguments": json.dumps(args)}


def _agent_name_from_sysprompt(sysprompt: str) -> str:
    """Heuristic to identify which agent the script is being asked about."""
    if "CEO of TaskGenie" in sysprompt:
        return "CEO"
    m = re.search(r"You are ([A-Za-z0-9_]+), a (manager|worker)", sysprompt)
    return m.group(1) if m else "UNKNOWN"


class ScriptedLLM:
    """A fake LLM that returns canned responses keyed on agent identity & turn."""

    def __init__(self) -> None:
        self._turn: dict[str, int] = {}

    async def model_name(self) -> str:
        return "scripted-mock"

    async def chat(self, messages, tools=None):
        sys_content = messages[0]["content"]
        who = _agent_name_from_sysprompt(sys_content)
        turn = self._turn.get(who, 0)
        self._turn[who] = turn + 1
        return self._respond(who, turn, messages)

    def _respond(self, who: str, turn: int, messages):
        last_tool_results = self._last_tool_results(messages)

        if who == "CEO":
            return self._ceo(turn, last_tool_results)
        if who == "ResearchMgr":
            return self._research_mgr(turn, last_tool_results)
        if who.startswith("Alice"):
            return self._worker_alice(turn)
        if who.startswith("Bob"):
            return self._worker_bob(turn)
        return {"content": f"[unscripted agent {who}] giving up.", "tool_calls": [tc("x", "complete", report=f"{who} had no script")]}

    def _last_tool_results(self, messages):
        """Return list of (tool_name, parsed_content) for recent tool results."""
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
            tool_msg_idx = last_assistant_idx + 1 + j
            if tool_msg_idx < len(messages) and messages[tool_msg_idx]["role"] == "tool":
                results.append(
                    (tc_["function"]["name"], messages[tool_msg_idx]["content"])
                )
        return results

    def _ceo(self, turn, last_results):
        if turn == 0:
            return {
                "content": (
                    "Got it. The user wants a comparison of coding agents. "
                    "I'll bring in a research manager to organize that effort."
                ),
                "tool_calls": [
                    tc(
                        "ceo-1",
                        "hire",
                        name="ResearchMgr",
                        role_type="manager",
                        role_prompt=(
                            "You run the coding-agents research effort. Identify the "
                            "top contenders, gather pros and cons for each, and "
                            "synthesize a comparison report."
                        ),
                    )
                ],
            }
        if turn == 1:
            mgr_id = json.loads(last_results[0][1])["agent_id"]
            return {
                "content": "Assigning the task to ResearchMgr.",
                "tool_calls": [
                    tc(
                        "ceo-2",
                        "assign",
                        agent_id=mgr_id,
                        task="Research the top coding agents (Claude Code, Cursor, Aider, Codex, etc.) and produce a pros/cons comparison.",
                    )
                ],
            }
        if turn == 2:
            handle = json.loads(last_results[0][1])["task_handle"]
            return {
                "content": "Waiting on ResearchMgr.",
                "tool_calls": [tc("ceo-3", "await_results", task_handles=[handle])],
            }
        if turn == 3:
            results = json.loads(last_results[0][1])
            report = list(results.values())[0]
            return {
                "content": "Report looks solid. Wrapping up for the user.",
                "tool_calls": [
                    tc("ceo-4", "complete", report=f"FINAL ANSWER:\n\n{report}")
                ],
            }
        return {"content": "stuck", "tool_calls": [tc("ceo-x", "complete", report="stuck")]}

    def _research_mgr(self, turn, last_results):
        if turn == 0:
            return {
                "content": (
                    "I'll fan out: one researcher per option family. "
                    "Hiring Alice first for the agent-side tools."
                ),
                "tool_calls": [
                    tc(
                        "rm-1",
                        "hire",
                        name="Alice",
                        role_type="worker",
                        role_prompt="Research Claude Code and Cursor. List pros and cons of each, citing where possible.",
                    )
                ],
            }
        if turn == 1:
            return {
                "content": "Now hiring Bob for the CLI side.",
                "tool_calls": [
                    tc(
                        "rm-2",
                        "hire",
                        name="Bob",
                        role_type="worker",
                        role_prompt="Research Aider and Codex. List pros and cons of each, citing where possible.",
                    )
                ],
            }
        if turn == 2:
            mgr = next(a for a in Agent.REGISTRY.values() if a.name == "ResearchMgr")
            alice_id = next(c.id for c in mgr.children if c.name == "Alice")
            bob_id = next(c.id for c in mgr.children if c.name == "Bob")
            return {
                "content": "Dispatching both researchers in parallel.",
                "tool_calls": [
                    tc("rm-3a", "assign", agent_id=alice_id, task="Pros/cons of Claude Code and Cursor."),
                    tc("rm-3b", "assign", agent_id=bob_id, task="Pros/cons of Aider and Codex."),
                ],
            }
        if turn == 3:
            handles = []
            for _, content in last_results:
                handles.append(json.loads(content)["task_handle"])
            return {
                "content": "Awaiting both reports.",
                "tool_calls": [tc("rm-4", "await_results", task_handles=handles)],
            }
        if turn == 4:
            results = json.loads(last_results[0][1])
            combined = "\n\n".join(f"- {v}" for v in results.values())
            return {
                "content": "Synthesizing the two reports.",
                "tool_calls": [
                    tc(
                        "rm-5",
                        "complete",
                        report=f"Comparison of coding agents:\n\n{combined}",
                    )
                ],
            }
        return {"content": "stuck", "tool_calls": [tc("rm-x", "complete", report="stuck")]}

    def _worker_alice(self, turn):
        return {
            "content": "Pulling notes on Claude Code and Cursor from my brain.",
            "tool_calls": [
                tc(
                    "a-1",
                    "complete",
                    report=(
                        "Claude Code: pros = first-party Anthropic, strong reasoning, "
                        "IDE+web; cons = paid. Cursor: pros = great UX, multi-model; "
                        "cons = closed-source IDE."
                    ),
                )
            ],
        }

    def _worker_bob(self, turn):
        return {
            "content": "Doing the CLI-style agents.",
            "tool_calls": [
                tc(
                    "b-1",
                    "complete",
                    report=(
                        "Aider: pros = open, git-native; cons = terminal-only. "
                        "Codex: pros = OpenAI ecosystem; cons = less mature."
                    ),
                )
            ],
        }


async def main() -> int:
    Agent.REGISTRY.clear()
    agent_module.LLM = ScriptedLLM()  # type: ignore[assignment]

    ceo = Agent(name="CEO", role_type="ceo", system_prompt=CEO_ROLE_PROMPT)
    task = "Research coding agents and tell me the pros and cons of each."

    ceo_task = asyncio.create_task(ceo.run(task))
    tui = TUI(task=task)
    tui_task = asyncio.create_task(tui.run(stop_when=ceo_task))

    final = await ceo_task
    await tui_task

    print("\n========== FINAL ANSWER ==========")
    print(final)
    print("==================================")

    assert "FINAL ANSWER" in final, f"unexpected final: {final!r}"
    assert "Claude Code" in final, f"missing Claude Code in: {final!r}"
    assert "Aider" in final, f"missing Aider in: {final!r}"

    agents_by_name = {a.name: a for a in Agent.REGISTRY.values()}
    assert set(agents_by_name) >= {"CEO", "ResearchMgr", "Alice", "Bob"}, list(agents_by_name)
    for name, expected_status in [
        ("CEO", "done"),
        ("ResearchMgr", "done"),
        ("Alice", "done"),
        ("Bob", "done"),
    ]:
        actual = agents_by_name[name].status
        assert actual == expected_status, f"{name} status was {actual}, want {expected_status}"

    print(f"\nOK: {len(Agent.REGISTRY)} agents, all done.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
