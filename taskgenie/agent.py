from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from typing import Any, Literal

from taskgenie.archivist import (
    CompactionSkipped,
    CompactionTransientFailure,
    compact_messages,
)
from taskgenie.config import CONFIG
from taskgenie.context import count_tokens, dump_large_tool_result, should_compact
from taskgenie.events import BUS, Event
from taskgenie.llm import LLM
from taskgenie.prompts import manager_prompt, worker_prompt
from taskgenie.result import RunResult
from taskgenie.tools import REGISTRY
from taskgenie.tools.work import scratch_for

RoleType = Literal["ceo", "manager", "worker"]
Status = Literal["idle", "working", "done", "blocked", "fired", "exhausted", "error"]


# Match real provider error wordings tightly; substring matches on
# "context length" or "context window" were too broad and fired on noise
# strings like "context length cache hit 0%".
_CTX_LIMIT_RE = re.compile(
    r"(?:"
    r"context[_ ]length[_ ]exceeded"
    r"|maximum (?:context|input) (?:length|window)"
    r"|context (?:window|length)[^.]{0,40}(?:exceed|too (?:long|large|many))"
    r"|exceeds? (?:the )?(?:maximum |max )?(?:context|input)"
    r"|too many (?:input |prompt )?tokens"
    r"|(?:input|prompt) is too long"
    r")",
    re.IGNORECASE,
)


def _is_context_length_error(exc: Exception) -> bool:
    """True if `exc` looks like a context-window-exceeded error from any
    OpenAI-compatible or Anthropic provider. Errs on the side of saying
    no -- a false negative just falls through to normal error reporting;
    a false positive triggers an unnecessary compact+retry."""
    code = getattr(exc, "code", "")
    if isinstance(code, str) and code == "context_length_exceeded":
        return True
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error", {})
        if isinstance(err, dict):
            # OpenAI shape: error.code == "context_length_exceeded".
            if err.get("code") == "context_length_exceeded":
                return True
            # Anthropic shape: error.type == "invalid_request_error" with a
            # message about prompt length. Pair the type-check with a
            # regex match on the message so we don't false-positive on
            # other invalid_request_error variants (bad model, malformed
            # tool args, etc.).
            if err.get("type") == "invalid_request_error":
                msg = err.get("message", "")
                if isinstance(msg, str) and _CTX_LIMIT_RE.search(msg):
                    return True
    return bool(_CTX_LIMIT_RE.search(str(exc)))


class Agent:
    REGISTRY: dict[str, "Agent"] = {}
    # Optional scratch root injected by the active session; if None, falls back
    # to CONFIG.scratch_dir-derived behaviour.
    SCRATCH_ROOT: str | None = None
    # Shared workspace for deliverable-style artifacts (code, docs, etc.).
    # Workers write here via write_workspace; everyone can read via
    # read_workspace / list_workspace.
    WORKSPACE_ROOT: str | None = None

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
        # Paths this agent has called read_workspace on. Persistent across
        # turns so the lost-update warning in _write_workspace still fires
        # after the Archivist has compacted the original tool_call away.
        self.read_workspace_paths: set[str] = set()
        # Message-count high-water mark from the last (successful or
        # no-op) compaction attempt. _maybe_compact uses it to avoid
        # re-triggering every turn when one compact didn't bring the
        # history below threshold.
        self._compaction_floor: int = 0
        self.scratch_dir = scratch_for(self.id, root=Agent.SCRATCH_ROOT)
        self.workspace_dir = Agent.WORKSPACE_ROOT
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
            "read_workspace_paths": sorted(self.read_workspace_paths),
            "compaction_floor": self._compaction_floor,
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
        agent.read_workspace_paths = set(d.get("read_workspace_paths", []) or [])
        # Persisted across save/resume so the R9 repeat-compact guard
        # isn't silently reset every resume. Pre-round-three snapshots
        # don't have this field; fall back to len(messages) so resumed
        # sessions don't immediately re-compact on the first turn after
        # restart.
        agent._compaction_floor = d.get("compaction_floor", len(agent.messages))
        agent.scratch_dir = d["scratch_dir"]
        agent.workspace_dir = Agent.WORKSPACE_ROOT
        cls.REGISTRY[agent.id] = agent
        return agent

    @classmethod
    def bulk_load(cls, agents: dict[str, dict[str, Any]]) -> None:
        """Rehydrate a previously-saved set of agents into REGISTRY.
        Resets REGISTRY first so a fresh process starts clean.

        Re-emits `spawn`/`state`/`blocked` events tagged `replay=True` so
        the live TUI can paint the rehydrated tree while persistent
        consumers (event_logger -> stats.compute) can ignore the
        replays for counter purposes.
        """
        cls.REGISTRY.clear()
        # Two passes: instantiate all, then patch parent/child links so order
        # doesn't matter.
        for d in agents.values():
            cls._rehydrate_one(d)
        # Sort by depth ASC so re-emitted spawn events arrive parent-first.
        # If a child's spawn arrived before its parent's, the TUI would
        # treat the child as root (or build an incomplete tree) until
        # the parent's spawn corrected things.
        ordered = sorted(agents.values(), key=lambda d: d.get("depth", 0))
        for d in ordered:
            agent = cls.REGISTRY[d["id"]]
            parent_id = d.get("parent_id")
            if parent_id:
                agent.parent = cls.REGISTRY.get(parent_id)
            agent.children = [
                cls.REGISTRY[cid] for cid in d.get("child_ids", []) if cid in cls.REGISTRY
            ]
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
                        "replay": True,
                    },
                )
            )
            BUS.publish(
                Event(
                    "state",
                    agent.id,
                    agent.name,
                    data={"status": agent.status, "replay": True},
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
                            "replay": True,
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

    async def _maybe_compact(self, force: bool = False) -> bool:
        """If the message history is approaching the context window,
        hand it to the Archivist and swap in the compacted version.

        `force=True` runs compaction unconditionally (used for reactive
        recovery after a context-length error from the API).

        Returns True if compaction actually shrank the history.

        Emits `compaction_start` / `compaction_end` events on BUS so the
        TUI can surface compaction activity on THIS agent's row. The
        archivist function itself no longer publishes anything to the
        bus. We use try/finally so `compaction_end` ALWAYS lands once
        `compaction_start` has fired -- otherwise a CancelledError mid-
        compact (e.g. parent fires this agent via `_tool_fire` -> task
        cancel) would leave the TUI showing "compacting..." forever.

        Floor advancement policy:
          - Genuine no-op (CompactionSkipped: history structure prevents
            compaction)     -> advance floor; don't retry every turn.
          - Transient failure (CompactionTransientFailure: LLM blip)
                            -> DO NOT advance; retry on next threshold cross.
          - Success         -> advance floor to new len.
        """
        if not force:
            if not should_compact(
                self.messages,
                CONFIG.ctx_window_tokens,
                CONFIG.compact_at_pct,
            ):
                return False
            # Repeat-compaction guard: if we compacted recently and only a
            # couple of new messages have been added since, skip. Stops a
            # feedback loop where the first compact couldn't bring the
            # history below threshold (large tool results stay verbatim)
            # and every subsequent turn runs another archivist LLM call.
            if len(self.messages) - self._compaction_floor < 4:
                return False
        before = len(self.messages)
        before_tokens = count_tokens(self.messages)
        self._emit(
            "compaction_start",
            before_messages=before,
            before_tokens=before_tokens,
            keep_recent=CONFIG.compact_keep_recent,
        )
        start = time.monotonic()
        end_payload: dict[str, Any] = {
            "before_messages": before,
            "before_tokens": before_tokens,
        }
        try:
            try:
                new_messages = await compact_messages(self.name, self.messages)
            except CompactionSkipped as e:
                # Structural no-op -- advance floor so we don't keep
                # invoking the archivist on the same history every
                # turn.
                self._compaction_floor = len(self.messages)
                end_payload.update(
                    success=False,
                    reason=f"skipped: {e.reason}",
                    after_messages=before,
                )
                return False
            except CompactionTransientFailure as e:
                # Transient failure -- don't advance floor; let the
                # next threshold cross retry.
                end_payload.update(
                    success=False,
                    reason=f"transient: {e.reason}",
                    after_messages=before,
                )
                return False
            # Defensive: archivist should always shrink on success path,
            # but a misbehaving stub might not. Treat as skipped.
            if len(new_messages) >= before:
                self._compaction_floor = len(self.messages)
                end_payload.update(
                    success=False,
                    reason="archivist returned no shrink",
                    after_messages=len(new_messages),
                )
                return False
            self.messages = new_messages
            self._compaction_floor = len(self.messages)
            after_tokens = count_tokens(self.messages)
            end_payload.update(
                success=True,
                after_messages=len(new_messages),
                after_tokens=after_tokens,
            )
            return True
        finally:
            end_payload["duration_ms"] = (time.monotonic() - start) * 1000
            self._emit("compaction_end", **end_payload)

    async def run(self, task: str) -> RunResult:
        self.messages.append({"role": "user", "content": task})
        self._set_status("working", task=task)

        empty_turn_count = 0
        for _turn in range(CONFIG.max_turns_per_agent):
            # Pre-call compaction: if we're approaching the context
            # window, summarise older messages via the Archivist BEFORE
            # the API call, so we don't blow the limit and so we burn
            # fewer prompt tokens on this turn.
            await self._maybe_compact()

            try:
                response = await LLM.chat(self.messages, tools=self.tools)
            except Exception as e:
                # Reactive recovery: if the API said we exceeded the
                # context window, force a compaction and retry ONCE. The
                # heuristic in count_tokens occasionally under-counts a
                # heavy tool_result so this path catches the long tail.
                if _is_context_length_error(e):
                    self._emit(
                        "error",
                        message=(
                            f"context window exceeded; compacting and retrying: {e}"
                        ),
                    )
                    compacted = await self._maybe_compact(force=True)
                    if compacted:
                        try:
                            response = await LLM.chat(self.messages, tools=self.tools)
                        except Exception as e2:
                            self._set_status("error", error=str(e2))
                            self._emit(
                                "error",
                                message=f"LLM call failed after compaction: {e2}",
                            )
                            return RunResult(
                                kind="error",
                                error=f"{self.name} LLM failure: {e2}",
                            )
                    else:
                        self._set_status("error", error=str(e))
                        self._emit(
                            "error",
                            message=(
                                f"LLM call failed (compaction couldn't help): {e}"
                            ),
                        )
                        return RunResult(
                            kind="error",
                            error=f"{self.name} LLM failure: {e}",
                        )
                else:
                    self._set_status("error", error=str(e))
                    self._emit("error", message=f"LLM call failed: {e}")
                    return RunResult(
                        kind="error", error=f"{self.name} LLM failure: {e}"
                    )

            usage = response.get("usage") or {}
            self._emit(
                "llm_call",
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                duration_ms=response.get("duration_ms", 0.0),
            )

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

            for i, tc in enumerate(tool_calls):
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
                    self._seal_tool_batch(tool_calls, i, "Completed.")
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
                    self._seal_tool_batch(tool_calls, i, "Blocker reported to parent.")
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
                    self._seal_tool_batch(tool_calls, i, "Awaiting user response.")
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

                # Wrap the whole tool-execute + post-process pipeline in
                # one try. If ANYTHING here raises (dispatcher, json.dumps
                # on a weird return, dump_large_tool_result hitting disk
                # full, _emit failing on a BUS subscriber) we still emit
                # a `tool` reply for this tool_call_id and seal any
                # trailing tool_calls in the batch -- otherwise the next
                # API call 400s with a dangling tool_call_id (same class
                # as the bug PR A1 fixed, shifted one layer down).
                try:
                    try:
                        result = await self._execute_tool(name, args)
                    except Exception as e:
                        result = f"[tool_error] {type(e).__name__}: {e}"
                        self._emit("error", message=str(result))

                    result_str = (
                        result if isinstance(result, str) else json.dumps(result)
                    )
                    # If the result is bigger than the dump threshold,
                    # persist it to scratch and replace the in-memory
                    # message with a pointer. Stops a single fat
                    # read_workspace/exec_code output from dominating the
                    # context window. The full text remains retrievable
                    # via read_file.
                    stored_content, dumped_path = dump_large_tool_result(
                        result_str,
                        self.scratch_dir,
                        _turn,
                        tc["id"],
                        name,
                        CONFIG.dump_tool_output_above_bytes,
                    )
                    self.messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": stored_content,
                        }
                    )
                    if dumped_path:
                        self._emit(
                            "tool_result",
                            tool=name,
                            result=stored_content,
                            dumped_path=dumped_path,
                            original_bytes=len(result_str),
                        )
                    else:
                        self._emit("tool_result", tool=name, result=result_str)
                except Exception as e:
                    err = f"[tool_pipeline_error] {type(e).__name__}: {e}"
                    last = self.messages[-1] if self.messages else {}
                    already_appended = (
                        last.get("role") == "tool"
                        and last.get("tool_call_id") == tc["id"]
                    )
                    if not already_appended:
                        self.messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "content": err,
                            }
                        )
                    for j in range(i + 1, len(tool_calls)):
                        skip_tc = tool_calls[j]
                        self.messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": skip_tc["id"],
                                "content": (
                                    f"[skipped: agent halted on pipeline "
                                    f"error in `{name}`]"
                                ),
                            }
                        )
                    self._emit("error", message=err)
                    self._set_status("error", error=err)
                    return RunResult(
                        kind="error",
                        error=f"{self.name} tool pipeline failure: {e}",
                    )

        self._set_status("exhausted")
        return RunResult(
            kind="exhausted",
            error=f"{self.name} exhausted turn limit of {CONFIG.max_turns_per_agent}",
        )

    def _seal_tool_batch(
        self,
        tool_calls: list[dict[str, Any]],
        finishing_index: int,
        finishing_content: str,
    ) -> None:
        """Append tool-result stubs for every unanswered tool_call_id when
        the run loop is about to return early on `complete` / `report_blocker`
        / `ask_user`. The matching assistant turn has already declared all
        these IDs in its `tool_calls`; if we don't seal them, the next time
        these messages hit the API (e.g., a follow-up turn) the API will
        400 because a tool_call has no matching tool reply.
        """
        if not (0 <= finishing_index < len(tool_calls)):
            raise ValueError(
                f"_seal_tool_batch finishing_index {finishing_index} out of "
                f"range for tool_calls of length {len(tool_calls)}"
            )
        finishing_name = tool_calls[finishing_index]["name"]
        for i in range(finishing_index, len(tool_calls)):
            tc = tool_calls[i]
            if i == finishing_index:
                content = finishing_content
            else:
                content = (
                    f"[skipped: agent returned after `{finishing_name}` in the same turn]"
                )
            self.messages.append(
                {"role": "tool", "tool_call_id": tc["id"], "content": content}
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
        # Use _set_status (not direct assignment) so subscribers see the
        # transition event -- the TUI tree relies on `state` events to
        # re-colour the node; without the event it appeared stuck in the
        # previous status until the next emit on that agent.
        child._set_status("fired", reason=reason)
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
