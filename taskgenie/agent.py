from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, Literal

from taskgenie.config import CONFIG
from taskgenie.events import BUS, Event
from taskgenie.llm import LLM
from taskgenie.prompts import manager_prompt, worker_prompt
from taskgenie.tools import MANAGEMENT_TOOLS, WORK_TOOLS
from taskgenie.tools.work import exec_code, read_file, scratch_for, write_file

RoleType = Literal["ceo", "manager", "worker"]
Status = Literal["idle", "working", "done", "fired", "exhausted", "error"]


class Agent:
    REGISTRY: dict[str, "Agent"] = {}

    def __init__(
        self,
        name: str,
        role_type: RoleType,
        system_prompt: str,
        parent: "Agent | None" = None,
        depth: int = 0,
    ) -> None:
        self.id = f"{name}-{uuid.uuid4().hex[:6]}"
        self.name = name
        self.role_type: RoleType = role_type
        self.system_prompt = system_prompt
        self.parent = parent
        self.depth = depth
        self.children: list[Agent] = []
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        self.status: Status = "idle"
        self.task_assignments: dict[str, asyncio.Task[str]] = {}
        self.task_targets: dict[str, str] = {}
        self.scratch_dir = scratch_for(self.id)
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

    @property
    def tools(self) -> list[dict[str, Any]]:
        if self.role_type == "worker":
            return WORK_TOOLS
        return MANAGEMENT_TOOLS

    def _emit(self, kind: str, **data: Any) -> None:
        BUS.publish(Event(kind, self.id, self.name, data=data))  # type: ignore[arg-type]

    def _set_status(self, status: Status, **extra: Any) -> None:
        self.status = status
        self._emit("state", status=status, **extra)

    async def run(self, task: str) -> str:
        self.messages.append({"role": "user", "content": task})
        self._set_status("working", task=task)

        empty_turn_count = 0
        for turn in range(CONFIG.max_turns_per_agent):
            try:
                response = await LLM.chat(self.messages, tools=self.tools)
            except Exception as e:
                self._set_status("error", error=str(e))
                self._emit("error", message=f"LLM call failed: {e}")
                return f"[ERROR] {self.name} failed: {e}"

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
                    return content or "[no output]"
                self.messages.append(
                    {
                        "role": "user",
                        "content": (
                            "You did not call a tool. Continue your work by calling a tool, "
                            "or call `complete` with your final report if you are done."
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
                    return report

                try:
                    result = await self._execute_tool(name, args)
                except Exception as e:
                    result = f"[tool_error] {type(e).__name__}: {e}"
                    self._emit("error", message=result)

                result_str = result if isinstance(result, str) else json.dumps(result)
                self.messages.append(
                    {"role": "tool", "tool_call_id": tc["id"], "content": result_str}
                )
                self._emit("tool_result", tool=name, result=result_str)

        self._set_status("exhausted")
        return f"[{self.name} exhausted turn limit of {CONFIG.max_turns_per_agent}]"

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
        if name == "read_file":
            return read_file(self.scratch_dir, args["path"])
        if name == "write_file":
            return write_file(self.scratch_dir, args["path"], args["content"])
        if name == "exec_code":
            return exec_code(args["language"], args["code"])
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

    async def _tool_await_results(self, task_handles: list[str]) -> dict[str, str]:
        results: dict[str, str] = {}
        for h in task_handles:
            t = self.task_assignments.get(h)
            if not t:
                results[h] = "[error] unknown task_handle"
                continue
            try:
                results[h] = await t
            except asyncio.CancelledError:
                results[h] = "[cancelled]"
            except Exception as e:
                results[h] = f"[error] {type(e).__name__}: {e}"
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
