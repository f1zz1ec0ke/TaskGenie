from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, Literal

from taskgenie.config import CONFIG
from taskgenie.events import BUS, Event
from taskgenie.llm import LLM
from taskgenie.prompts import manager_prompt, worker_prompt
from taskgenie.result import RunResult
from taskgenie.tools import REGISTRY
from taskgenie.tools.work import scratch_for

RoleType = Literal["ceo", "manager", "worker"]
Status = Literal["idle", "working", "done", "blocked", "fired", "exhausted", "error"]


class Agent:
    REGISTRY: dict[str, "Agent"] = {}
    # Optional scratch root injected by the active session; if None, falls back
    # to CONFIG.scratch_dir-derived behaviour.
    SCRATCH_ROOT: str | None = None

    def __init__(
        self,
        name: str,
        role_type: RoleType,
        system_prompt: str,
        parent: "Agent | None" = None,
        depth: int = 0,
        agent_id: str | None = None,
    ) -> None:
        self.id = agent_id or f"{name}-{uuid.uuid4().hex[:6]}"
        self.name = name
        self.role_type: RoleType = role_type
        self.system_prompt = system_prompt
        self.parent = parent
        self.depth = depth
        self.children: list[Agent] = []
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        self.status: Status = "idle"
        self.task_assignments: dict[str, asyncio.Task[RunResult]] = {}
        self.task_targets: dict[str, str] = {}
        self.blocker_reason: str = ""
        self.blocker_needs: str = ""
        self.follow_ups: list[dict[str, Any]] = []
        self.scratch_dir = scratch_for(self.id, root=Agent.SCRATCH_ROOT)
        Agent.REGISTRY[self.id] = self
        BUS.publish(
            Event(
                "spawn",
                self.id,
                self.name,
                data={
                    "role_type": role_type,
                    "parent_id": parent.id if parent else None,
                    "parent_name": parent.name if parent else None,
                    "depth": depth,
                    "system_prompt": system_prompt,
                },
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "role_type": self.role_type,
            "system_prompt": self.system_prompt,
            "parent_id": self.parent.id if self.parent else None,
            "child_ids": [c.id for c in self.children],
            "depth": self.depth,
            "status": self.status,
            "messages": self.messages,
            "scratch_dir": self.scratch_dir,
            "blocker_reason": self.blocker_reason,
            "blocker_needs": self.blocker_needs,
            "follow_ups": self.follow_ups,
        }

    @classmethod
    def _rehydrate_one(cls, d: dict[str, Any]) -> "Agent":
        agent = cls.__new__(cls)
        agent.id = d["id"]
        agent.name = d["name"]
        agent.role_type = d["role_type"]
        agent.system_prompt = d["system_prompt"]
        agent.parent = None  # patched by bulk_load
        agent.depth = d["depth"]
        agent.children = []  # patched by bulk_load
        agent.messages = d["messages"]
        agent.status = d["status"]
        agent.task_assignments = {}
        agent.task_targets = {}
        agent.blocker_reason = d.get("blocker_reason", "")
        agent.blocker_needs = d.get("blocker_needs", "")
        agent.follow_ups = d.get("follow_ups", [])
        agent.scratch_dir = d["scratch_dir"]
        cls.REGISTRY[agent.id] = agent
        return agent

    @classmethod
    def bulk_load(cls, agents: dict[str, dict[str, Any]]) -> None:
        """Rehydrate a previously-saved set of agents into REGISTRY.
        Resets REGISTRY first so a fresh process starts clean."""
        cls.REGISTRY.clear()
        # Two passes: instantiate all, then patch parent/child links so order
        # doesn't matter.
        for d in agents.values():
            cls._rehydrate_one(d)
        for d in agents.values():
            agent = cls.REGISTRY[d["id"]]
            parent_id = d.get("parent_id")
            if parent_id:
                agent.parent = cls.REGISTRY.get(parent_id)
            agent.children = [
                cls.REGISTRY[cid] for cid in d.get("child_ids", []) if cid in cls.REGISTRY
            ]
            # Re-emit a spawn event so any subscriber (e.g. a TUI bootstrapping
            # from the bus) can paint the rehydrated tree.
            BUS.publish(
                Event(
                    "spawn",
                    agent.id,
                    agent.name,
                    data={
                        "role_type": agent.role_type,
                        "parent_id": agent.parent.id if agent.parent else None,
                        "parent_name": agent.parent.name if agent.parent else None,
                        "depth": agent.depth,
                        "system_prompt": agent.system_prompt,
                    },
                )
            )
            BUS.publish(
                Event(
                    "state",
                    agent.id,
                    agent.name,
                    data={"status": agent.status},
                )
            )
            if agent.status == "blocked":
                BUS.publish(
                    Event(
                        "blocked",
                        agent.id,
                        agent.name,
                        data={
                            "reason": agent.blocker_reason,
                            "needs": agent.blocker_needs,
                            "partial_progress": "",
                        },
                    )
                )

    @property
    def tools(self) -> list[dict[str, Any]]:
        if self.role_type == "worker":
            return REGISTRY.work_tools()
        tools = list(REGISTRY.management_tools())
        if self.role_type == "ceo":
            tools.extend(REGISTRY.ceo_only_tools())
        return tools

    def _emit(self, kind: str, **data: Any) -> None:
        BUS.publish(Event(kind, self.id, self.name, data=data))  # type: ignore[arg-type]

    def _set_status(self, status: Status, **extra: Any) -> None:
        self.status = status
        self._emit("state", status=status, **extra)

    async def run(self, task: str) -> RunResult:
        self.messages.append({"role": "user", "content": task})
        self._set_status("working", task=task)

        empty_turn_count = 0
        for _turn in range(CONFIG.max_turns_per_agent):
            try:
                response = await LLM.chat(self.messages, tools=self.tools)
            except Exception as e:
                self._set_status("error", error=str(e))
                self._emit("error", message=f"LLM call failed: {e}")
                return RunResult(kind="error", error=f"{self.name} LLM failure: {e}")

            content = response.get("content", "") or ""
            tool_calls = response.get("tool_calls", []) or []
            self.messages.append(self._build_assistant_message(content, tool_calls))

            if content.strip():
                self._emit("narrate", text=content.strip())

            if not tool_calls:
                empty_turn_count += 1
                if empty_turn_count >= 2:
                    self._set_status("done", report=content)
                    self._emit("complete", report=content)
                    return RunResult(kind="done", report=content or "[no output]")
                self.messages.append(
                    {
                        "role": "user",
                        "content": (
                            "You did not call a tool. Continue your work by calling a tool, "
                            "or call `complete` with your final report if you are done. "
                            "If you cannot make progress, call `report_blocker`."
                        ),
                    }
                )
                continue
            empty_turn_count = 0

            for tc in tool_calls:
                name = tc["name"]
                try:
                    args = json.loads(tc["arguments"]) if tc["arguments"] else {}
                except json.JSONDecodeError as e:
                    err = f"[tool_args_error] Invalid JSON for {name}: {e}"
                    self.messages.append(
                        {"role": "tool", "tool_call_id": tc["id"], "content": err}
                    )
                    self._emit("error", message=err)
                    continue

                self._emit("tool_call", tool=name, args=args)

                if name == "complete":
                    report = args.get("report", content or "")
                    self._set_status("done", report=report)
                    self._emit("complete", report=report)
                    return RunResult(
                        kind="done", report=report, follow_ups=list(self.follow_ups)
                    )

                if name == "report_blocker":
                    reason = args.get("reason", "")
                    needs = args.get("needs", "")
                    partial = args.get("partial_progress", "")
                    self.blocker_reason = reason
                    self.blocker_needs = needs
                    self._set_status("blocked", reason=reason, needs=needs)
                    self._emit(
                        "blocked",
                        reason=reason,
                        needs=needs,
                        partial_progress=partial,
                    )
                    return RunResult(
                        kind="blocked",
                        reason=reason,
                        needs=needs,
                        partial_progress=partial,
                        follow_ups=list(self.follow_ups),
                    )

                if name == "ask_user":
                    question = args.get("question", "")
                    options = args.get("options", []) or []
                    qctx = args.get("context", "")
                    self._set_status("blocked", question=question)
                    self._emit(
                        "question",
                        question=question,
                        options=options,
                        context=qctx,
                    )
                    return RunResult(
                        kind="question",
                        question=question,
                        options=list(options),
                        context=qctx,
                        follow_ups=list(self.follow_ups),
                    )

                if name == "suggest_follow_up":
                    entry = {
                        "what": args.get("what_is_needed", ""),
                        "rationale": args.get("rationale", ""),
                        "who": args.get("who_should_do_it", ""),
                    }
                    self.follow_ups.append(entry)
                    self._emit("follow_up_suggested", **entry)
                    self.messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": (
                                f"Follow-up recorded: {entry['what']}. "
                                "It will be passed to your parent when you complete. "
                                "Keep working on your assigned task."
                            ),
                        }
                    )
                    continue

                try:
                    result = await self._execute_tool(name, args)
                except Exception as e:
                    result = f"[tool_error] {type(e).__name__}: {e}"
                    self._emit("error", message=str(result))

                result_str = result if isinstance(result, str) else json.dumps(result)
                self.messages.append(
                    {"role": "tool", "tool_call_id": tc["id"], "content": result_str}
                )
                self._emit("tool_result", tool=name, result=result_str)

        self._set_status("exhausted")
        return RunResult(
            kind="exhausted",
            error=f"{self.name} exhausted turn limit of {CONFIG.max_turns_per_agent}",
        )

    def _build_assistant_message(
        self, content: str, tool_calls: list[dict[str, Any]]
    ) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": tc["arguments"]},
                }
                for tc in tool_calls
            ]
        return msg

    async def _execute_tool(self, name: str, args: dict[str, Any]) -> Any:
        if name == "hire":
            return self._tool_hire(**args)
        if name == "assign":
            return self._tool_assign(**args)
        if name == "await_results":
            return await self._tool_await_results(**args)
        if name == "review":
            return self._tool_review(**args)
        if name == "rehire":
            return self._tool_rehire(**args)
        if name == "fire":
            return self._tool_fire(**args)
        dispatcher = REGISTRY.dispatcher(name)
        if dispatcher is not None:
            return await dispatcher(self, args)
        return f"[unknown_tool] {name}"

    def _tool_hire(self, name: str, role_type: str, role_prompt: str) -> dict[str, Any]:
        if self.role_type == "worker":
            return {"error": "workers cannot hire others"}
        if role_type not in ("manager", "worker"):
            return {"error": f"invalid role_type {role_type!r}"}
        if self.depth >= CONFIG.max_depth:
            return {
                "error": (
                    f"max hierarchy depth ({CONFIG.max_depth}) reached — "
                    "hire a worker instead of nesting another manager."
                )
            }

        if role_type == "manager":
            full_prompt = manager_prompt(name, self.name, role_prompt)
        else:
            full_prompt = worker_prompt(name, self.name, role_prompt)

        child = Agent(
            name=name,
            role_type=role_type,  # type: ignore[arg-type]
            system_prompt=full_prompt,
            parent=self,
            depth=self.depth + 1,
        )
        self.children.append(child)
        self._emit(
            "hire",
            child_id=child.id,
            child_name=name,
            role_type=role_type,
            role_prompt=role_prompt,
        )
        return {"agent_id": child.id, "name": name, "role_type": role_type}

    def _tool_assign(self, agent_id: str, task: str) -> dict[str, Any]:
        child = self._lookup_child(agent_id)
        if isinstance(child, str):
            return {"error": child}
        if child.status == "fired":
            return {"error": f"{child.name} was fired and cannot be assigned tasks"}
        if child.status == "working":
            return {
                "error": (
                    f"{child.name} is already running another task. "
                    "Call `await_results` on the existing task_handle before re-assigning."
                )
            }

        task_handle = f"task-{uuid.uuid4().hex[:6]}"
        coro_task = asyncio.create_task(child.run(task), name=task_handle)
        self.task_assignments[task_handle] = coro_task
        self.task_targets[task_handle] = child.id
        self._emit(
            "assign",
            task_handle=task_handle,
            agent_id=agent_id,
            agent_name=child.name,
            task=task,
        )
        return {"task_handle": task_handle, "agent_id": agent_id, "agent_name": child.name}

    async def _tool_await_results(self, task_handles: list[str]) -> dict[str, dict[str, Any]]:
        results: dict[str, dict[str, Any]] = {}
        for h in task_handles:
            t = self.task_assignments.get(h)
            if not t:
                results[h] = {"status": "error", "error": "unknown task_handle"}
                continue
            try:
                r = await t
                results[h] = r.to_tool_payload() if isinstance(r, RunResult) else {
                    "status": "done",
                    "report": str(r),
                }
            except asyncio.CancelledError:
                results[h] = {"status": "cancelled"}
            except Exception as e:
                results[h] = {"status": "error", "error": f"{type(e).__name__}: {e}"}
        return results

    def _tool_review(self, task_handle: str, accepted: bool, feedback: str) -> str:
        target_id = self.task_targets.get(task_handle, "?")
        self._emit(
            "review",
            task_handle=task_handle,
            target_id=target_id,
            accepted=accepted,
            feedback=feedback,
        )
        verdict = "ACCEPTED" if accepted else "REJECTED"
        return f"Review {verdict} for {task_handle}. Feedback recorded."

    def _tool_rehire(self, agent_id: str, new_role_prompt: str) -> str:
        child = self._lookup_child(agent_id)
        if isinstance(child, str):
            return f"[error] {child}"
        if child.role_type == "manager":
            full_prompt = manager_prompt(child.name, self.name, new_role_prompt)
        else:
            full_prompt = worker_prompt(child.name, self.name, new_role_prompt)
        child.system_prompt = full_prompt
        child.messages = [{"role": "system", "content": full_prompt}]
        child.status = "idle"
        self._emit("rehire", agent_id=agent_id, new_role_prompt=new_role_prompt)
        return f"Rehired {child.name} with new charter."

    def _tool_fire(self, agent_id: str, reason: str) -> str:
        child = self._lookup_child(agent_id)
        if isinstance(child, str):
            return f"[error] {child}"
        child.status = "fired"
        for handle, t in list(self.task_assignments.items()):
            if self.task_targets.get(handle) == child.id and not t.done():
                t.cancel()
        self._emit("fire", agent_id=agent_id, agent_name=child.name, reason=reason)
        return f"Fired {child.name}. Reason: {reason}"

    def _lookup_child(self, agent_id: str) -> "Agent | str":
        child = Agent.REGISTRY.get(agent_id)
        if not child:
            return f"no agent with id {agent_id}"
        if child not in self.children:
            return f"{agent_id} is not your direct subordinate"
        return child
