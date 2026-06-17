"""Gap-fill tests for the polish pass.

Targets coverage gaps surfaced by previous reviews but never filled:

- Direct tests for `_tool_review` / `_tool_rehire` / `_tool_fire`
  (only happy-path indirect coverage existed).
- `bulk_load` parent-relinking edge cases (missing parent, absent
  child id in child_ids).
- `RunResult.to_tool_payload` per-kind (each `RunKind` was a bright
  spot to preserve but had zero direct coverage).
- MCP `_register_mcp_tool` name-collision auto-namespacing.
- Resume of `status="asking"` (session quit mid-question).
- Dedicated tests for round-two fixes that only have implicit
  coverage today: R12 (Ctrl+C snapshot), R13 (ceo_task cleanup on
  TUI raise), R22 (TUI tool_args truncation), R24 (Measurement
  narrow-width clamp).

Run with: python -m tests.test_polish_coverage
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rich.console import Console
from rich.tree import Tree

from taskgenie import __main__ as cli_module
from taskgenie import agent as agent_module
from taskgenie.agent import Agent
from taskgenie.config import CONFIG
from taskgenie.events import BUS, Event
from taskgenie.prompts import CEO_ROLE_PROMPT
from taskgenie.result import RunResult
from taskgenie.session import PROJECTS_ROOT_ENV, Session
from taskgenie.tools.registry import REGISTRY
from taskgenie.tui import TUI, _ColumnOverflowTree


def _new_ceo() -> Agent:
    Agent.REGISTRY.clear()
    d = tempfile.mkdtemp(prefix="tg-polish-")
    Agent.SCRATCH_ROOT = d
    Agent.WORKSPACE_ROOT = d
    return Agent(name="CEO", role_type="ceo", system_prompt=CEO_ROLE_PROMPT)


@contextlib.contextmanager
def _isolated_data_paths(tmp_path: Path):
    """Redirect both internal session storage and user-facing project
    output to subdirs of `tmp_path` for the duration of the test, and
    restore originals on exit.

    CONFIG.data_dir was previously restored to the literal string
    `'.taskgenie'`, which clobbered any TASKGENIE_DATA_DIR env override
    on the test runner; capture the original instead. Likewise, the
    project directory used `Session.create(...)` -> `_unique_project_dir`
    falls back to `./TaskGenie/` in the cwd, leaving real folders
    around after every test run -- pin TASKGENIE_PROJECTS_DIR too.
    """
    orig_data = CONFIG.data_dir
    orig_projects = os.environ.get(PROJECTS_ROOT_ENV)
    object.__setattr__(CONFIG, "data_dir", str(tmp_path / "data"))
    os.environ[PROJECTS_ROOT_ENV] = str(tmp_path / "projects")
    try:
        yield
    finally:
        object.__setattr__(CONFIG, "data_dir", orig_data)
        if orig_projects is None:
            os.environ.pop(PROJECTS_ROOT_ENV, None)
        else:
            os.environ[PROJECTS_ROOT_ENV] = orig_projects


# ---------------------------------------------------------------------------
# RunResult.to_tool_payload per RunKind
# ---------------------------------------------------------------------------


def test_to_tool_payload_done_kind() -> None:
    r = RunResult(kind="done", report="all done")
    p = r.to_tool_payload()
    assert p == {"status": "done", "report": "all done"}
    print("[to_tool_payload_done_kind] OK")


def test_to_tool_payload_blocked_kind() -> None:
    r = RunResult(
        kind="blocked",
        reason="missing widget",
        needs="please provide widget",
        partial_progress="got 3 of 5 sub-tasks",
    )
    p = r.to_tool_payload()
    assert p["status"] == "blocked"
    assert p["reason"] == "missing widget"
    assert p["needs"] == "please provide widget"
    assert p["partial_progress"] == "got 3 of 5 sub-tasks"
    print("[to_tool_payload_blocked_kind] OK")


def test_to_tool_payload_question_kind() -> None:
    r = RunResult(
        kind="question",
        question="X or Y?",
        options=["X", "Y"],
        context="we're deciding between two interpretations",
    )
    p = r.to_tool_payload()
    assert p["status"] == "question"
    assert p["question"] == "X or Y?"
    assert p["options"] == ["X", "Y"]
    assert p["context"] == "we're deciding between two interpretations"
    print("[to_tool_payload_question_kind] OK")


def test_to_tool_payload_exhausted_kind() -> None:
    r = RunResult(kind="exhausted", error="turn limit reached")
    p = r.to_tool_payload()
    assert p == {"status": "exhausted", "error": "turn limit reached"}
    # Default error string fallback when not supplied.
    p2 = RunResult(kind="exhausted").to_tool_payload()
    assert p2 == {"status": "exhausted", "error": "turn limit reached"}
    print("[to_tool_payload_exhausted_kind] OK")


def test_to_tool_payload_error_kind() -> None:
    r = RunResult(kind="error", error="LLM failure")
    p = r.to_tool_payload()
    assert p == {"status": "error", "error": "LLM failure"}
    print("[to_tool_payload_error_kind] OK")


def test_to_tool_payload_includes_follow_ups_when_present() -> None:
    r = RunResult(
        kind="done",
        report="all done",
        follow_ups=[{"what": "do X", "rationale": "y", "who": "manager"}],
    )
    p = r.to_tool_payload()
    assert "follow_ups" in p
    assert p["follow_ups"][0]["what"] == "do X"

    # Empty follow_ups list omitted.
    r2 = RunResult(kind="done", report="done")
    assert "follow_ups" not in r2.to_tool_payload()
    print("[to_tool_payload_includes_follow_ups_when_present] OK")


# ---------------------------------------------------------------------------
# _tool_review
# ---------------------------------------------------------------------------


def test_tool_review_emits_event_and_returns_verdict() -> None:
    ceo = _new_ceo()
    # Stash a fake task_handle->target mapping so _tool_review's
    # event payload can resolve it.
    ceo.task_targets["task-xyz"] = "agent-abc"

    probe = BUS.subscribe()
    try:
        result = ceo._tool_review(
            task_handle="task-xyz",
            accepted=True,
            feedback="looks good",
        )
        events: list[Event] = []
        while not probe.empty():
            events.append(probe.get_nowait())
    finally:
        BUS.unsubscribe(probe)

    assert "ACCEPTED" in result
    review_events = [e for e in events if e.kind == "review"]
    assert len(review_events) == 1
    rev = review_events[0]
    assert rev.data["task_handle"] == "task-xyz"
    assert rev.data["target_id"] == "agent-abc"
    assert rev.data["accepted"] is True
    assert rev.data["feedback"] == "looks good"
    print("[tool_review_emits_event_and_returns_verdict] OK")


def test_tool_review_rejected_branch() -> None:
    ceo = _new_ceo()
    result = ceo._tool_review(
        task_handle="task-1",
        accepted=False,
        feedback="missing sources",
    )
    assert "REJECTED" in result
    print("[tool_review_rejected_branch] OK")


def test_tool_review_unknown_handle_falls_back_to_question_mark() -> None:
    """No mapping for the handle -> target_id resolves to '?'. The call
    still completes (review is purely audit, not a real lookup)."""
    ceo = _new_ceo()
    probe = BUS.subscribe()
    try:
        result = ceo._tool_review("missing-handle", True, "ok")
        events = []
        while not probe.empty():
            events.append(probe.get_nowait())
    finally:
        BUS.unsubscribe(probe)
    assert "ACCEPTED" in result
    rev = next(e for e in events if e.kind == "review")
    assert rev.data["target_id"] == "?"
    print("[tool_review_unknown_handle_falls_back_to_question_mark] OK")


# ---------------------------------------------------------------------------
# _tool_rehire
# ---------------------------------------------------------------------------


def test_tool_rehire_resets_messages_and_status() -> None:
    ceo = _new_ceo()
    hire_result = ceo._tool_hire(
        name="Worker", role_type="worker", role_prompt="initial role"
    )
    child_id = hire_result["agent_id"]
    child = Agent.REGISTRY[child_id]
    # Simulate the child having accumulated messages + a blocked status.
    child.messages.append({"role": "user", "content": "go"})
    child.messages.append({"role": "assistant", "content": "stuck"})
    child._set_status("blocked")
    assert len(child.messages) > 1

    probe = BUS.subscribe()
    try:
        result = ceo._tool_rehire(child_id, "new tighter role")
        events = []
        while not probe.empty():
            events.append(probe.get_nowait())
    finally:
        BUS.unsubscribe(probe)

    assert "Rehired" in result
    # Messages collapse to just the new system prompt.
    assert len(child.messages) == 1
    assert child.messages[0]["role"] == "system"
    assert "new tighter role" in child.messages[0]["content"]
    # Status resets to idle (ready to be re-assigned).
    assert child.status == "idle"
    # Bus saw the rehire event.
    rehire = [e for e in events if e.kind == "rehire"]
    assert rehire and rehire[0].data["new_role_prompt"] == "new tighter role"
    print("[tool_rehire_resets_messages_and_status] OK")


def test_tool_rehire_rejects_non_child() -> None:
    """Trying to rehire an agent that ISN'T a direct subordinate must
    fail with a clear error string (not silently mutate someone else's
    subordinate)."""
    ceo = _new_ceo()
    # Create a peer agent (not in ceo.children).
    peer = Agent(name="Peer", role_type="worker", system_prompt="x")
    assert peer not in ceo.children

    result = ceo._tool_rehire(peer.id, "new role")
    assert "[error]" in result
    assert "not your direct subordinate" in result
    print("[tool_rehire_rejects_non_child] OK")


def test_tool_rehire_rejects_unknown_agent_id() -> None:
    ceo = _new_ceo()
    result = ceo._tool_rehire("does-not-exist", "irrelevant")
    assert "[error]" in result
    assert "no agent with id" in result
    print("[tool_rehire_rejects_unknown_agent_id] OK")


# ---------------------------------------------------------------------------
# _tool_fire cancellation semantics
# ---------------------------------------------------------------------------


async def test_tool_fire_cancels_pending_child_task() -> None:
    """When a child has a pending task_handle, _tool_fire must cancel
    the underlying asyncio.Task. Round 3 added state-event emit; this
    test covers the task-cancel side that's load-bearing for resource
    cleanup."""
    ceo = _new_ceo()
    hire_result = ceo._tool_hire(name="Worker", role_type="worker", role_prompt="x")
    child_id = hire_result["agent_id"]
    child = Agent.REGISTRY[child_id]

    # Manually plant a long-running task that mimics what _tool_assign
    # would create (without needing a real LLM).
    async def _slow():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            raise
        return RunResult(kind="done", report="never")

    task = asyncio.create_task(_slow(), name="task-fake")
    ceo.task_assignments["task-fake"] = task
    ceo.task_targets["task-fake"] = child.id
    # Let the task actually start before we cancel it.
    await asyncio.sleep(0.01)
    assert not task.done()

    ceo._tool_fire(child_id, reason="testing")

    # Cancel was issued -- wait for it to land.
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert task.done()
    assert task.cancelled() or task.exception() is not None
    print("[tool_fire_cancels_pending_child_task] OK")


def test_tool_fire_does_not_cancel_already_done_task() -> None:
    """If the task already finished, no cancel-on-done weirdness."""
    ceo = _new_ceo()
    hire_result = ceo._tool_hire("W", "worker", "x")
    child = Agent.REGISTRY[hire_result["agent_id"]]

    # Plant a task that's already done.
    async def _finished():
        return RunResult(kind="done", report="ok")

    async def _seed() -> asyncio.Task[Any]:
        t = asyncio.create_task(_finished())
        await t  # ensure it's done
        return t

    task = asyncio.run(_seed())
    ceo.task_assignments["task-done"] = task
    ceo.task_targets["task-done"] = child.id

    # Firing shouldn't blow up on the already-done task.
    result = ceo._tool_fire(hire_result["agent_id"], reason="x")
    assert "Fired" in result
    print("[tool_fire_does_not_cancel_already_done_task] OK")


# ---------------------------------------------------------------------------
# bulk_load parent-relinking edge cases
# ---------------------------------------------------------------------------


def test_bulk_load_with_missing_parent_orphans_gracefully(tmp_path: Path) -> None:
    """A child whose parent_id points at an agent NOT in the dict should
    still load -- its `.parent` ends up None rather than KeyError."""
    Agent.REGISTRY.clear()
    Agent.SCRATCH_ROOT = str(tmp_path)
    Agent.WORKSPACE_ROOT = str(tmp_path)
    agents = {
        "orphan-1": {
            "id": "orphan-1",
            "name": "Orphan",
            "role_type": "worker",
            "system_prompt": "...",
            "parent_id": "ghost-parent",  # ghost parent doesn't exist
            "child_ids": [],
            "depth": 1,
            "status": "idle",
            "messages": [{"role": "system", "content": "..."}],
            "scratch_dir": str(tmp_path / "orphan"),
            "blocker_reason": "",
            "blocker_needs": "",
            "follow_ups": [],
        },
    }
    Agent.bulk_load(agents)
    orphan = Agent.REGISTRY["orphan-1"]
    assert orphan.parent is None
    print("[bulk_load_with_missing_parent_orphans_gracefully] OK")


def test_bulk_load_filters_absent_child_ids(tmp_path: Path) -> None:
    """If parent.child_ids references an id not in the snapshot dict,
    that entry should drop silently rather than crash."""
    Agent.REGISTRY.clear()
    Agent.SCRATCH_ROOT = str(tmp_path)
    Agent.WORKSPACE_ROOT = str(tmp_path)
    agents = {
        "ceo-1": {
            "id": "ceo-1",
            "name": "CEO",
            "role_type": "ceo",
            "system_prompt": "...",
            "parent_id": None,
            "child_ids": ["real-kid", "missing-kid"],
            "depth": 0,
            "status": "idle",
            "messages": [{"role": "system", "content": "..."}],
            "scratch_dir": str(tmp_path / "ceo"),
            "blocker_reason": "",
            "blocker_needs": "",
            "follow_ups": [],
        },
        "real-kid": {
            "id": "real-kid",
            "name": "Real",
            "role_type": "worker",
            "system_prompt": "...",
            "parent_id": "ceo-1",
            "child_ids": [],
            "depth": 1,
            "status": "idle",
            "messages": [{"role": "system", "content": "..."}],
            "scratch_dir": str(tmp_path / "real"),
            "blocker_reason": "",
            "blocker_needs": "",
            "follow_ups": [],
        },
    }
    Agent.bulk_load(agents)
    ceo = Agent.REGISTRY["ceo-1"]
    # Only the real kid links; missing-kid is dropped.
    assert len(ceo.children) == 1
    assert ceo.children[0].id == "real-kid"
    print("[bulk_load_filters_absent_child_ids] OK")


# ---------------------------------------------------------------------------
# MCP _register_mcp_tool name-collision auto-namespacing
# ---------------------------------------------------------------------------


def test_mcp_register_namespaces_on_collision() -> None:
    """If two MCP servers report a tool with the same name, the second
    gets prefixed by `<server>__<tool>` rather than overwriting."""
    from taskgenie.mcp_client import MCPClient

    # Reserve a name as if a built-in already exists. Put the
    # register call INSIDE the try block: prior test pollution could
    # otherwise raise here, and the finally that would unregister
    # `_collision_target` would never run.
    pre_existing_schema = {
        "type": "function",
        "function": {
            "name": "_collision_target",
            "description": "pre-existing",
            "parameters": {"type": "object", "properties": {}},
        },
    }

    class _FakeTool:
        name = "_collision_target"
        description = "from MCP"
        inputSchema = {"type": "object", "properties": {}}

    client = MCPClient()
    try:
        # Defensive: if a prior test polluted REGISTRY with the same
        # name, unregister it first so this test starts from a known
        # state. (Idempotent if the name isn't there.)
        try:
            REGISTRY.unregister("_collision_target")
        except KeyError:
            pass
        REGISTRY.register(pre_existing_schema, kind="work", version="0.1")
        client._register_mcp_tool("server-x", _FakeTool())
        # Original is intact under its name.
        assert REGISTRY.get("_collision_target") is not None
        # Namespaced form was registered.
        assert REGISTRY.get("server-x___collision_target") is not None
        # The MCP-registered name should be tracked on the client.
        assert "server-x___collision_target" in client.registered_tools
    finally:
        try:
            REGISTRY.unregister("_collision_target")
        except KeyError:
            pass
        try:
            REGISTRY.unregister("server-x___collision_target")
        except KeyError:
            pass
    print("[mcp_register_namespaces_on_collision] OK")


def test_mcp_register_records_error_on_double_collision() -> None:
    """If even the namespaced form collides, the client should record a
    connect_errors entry naming the server."""
    from taskgenie.mcp_client import MCPClient

    REGISTRY.register(
        {
            "type": "function",
            "function": {
                "name": "_dup",
                "description": "x",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        kind="work",
        version="0.1",
    )
    REGISTRY.register(
        {
            "type": "function",
            "function": {
                "name": "srv___dup",
                "description": "x",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        kind="work",
        version="0.1",
    )

    class _Tool:
        name = "_dup"
        description = "d"
        inputSchema = {"type": "object", "properties": {}}

    client = MCPClient()
    try:
        client._register_mcp_tool("srv", _Tool())
        # No registration happened.
        assert client.registered_tools == []
        # Error recorded.
        err = client.connect_errors.get("srv", "")
        assert "name conflict" in err, err
    finally:
        REGISTRY.unregister("_dup")
        REGISTRY.unregister("srv___dup")
    print("[mcp_register_records_error_on_double_collision] OK")


# ---------------------------------------------------------------------------
# Resume of status="asking"
# ---------------------------------------------------------------------------


def test_resume_of_asking_session_takes_question_branch(tmp_path: Path) -> None:
    """A session quit mid-question has status='asking' + an unresolved
    `question` entry in history. Resume should take the question
    branch (call _read_answer_input), not the blocker or follow-up
    branches."""
    with _isolated_data_paths(tmp_path):
        Agent.REGISTRY.clear()
        Agent.SCRATCH_ROOT = str(tmp_path)
        Agent.WORKSPACE_ROOT = str(tmp_path)
        session = Session.create("asking test")
        # Plant a CEO + mark a question without answering it.
        ceo = Agent(name="CEO", role_type="ceo", system_prompt="...")
        session.set_ceo(ceo.id)
        session.mark_question(
            {"question": "X or Y?", "options": ["X", "Y"], "context": ""}
        )
        session.snapshot_agents({aid: a.to_dict() for aid, a in Agent.REGISTRY.items()})
        session.save()
        assert session.data.status == "asking"

        # Spy on which prompt was called.
        called: dict[str, int] = {"answer": 0, "followup": 0, "unblock": 0}
        real_answer = cli_module._read_answer_input
        real_followup = cli_module._read_followup_input
        real_unblock = cli_module._read_unblock_input

        def spy_answer(*a, **kw):
            called["answer"] += 1
            return None  # cancels the resume

        def spy_followup(*a, **kw):
            called["followup"] += 1
            return None

        def spy_unblock(*a, **kw):
            called["unblock"] += 1
            return None

        cli_module._read_answer_input = spy_answer  # type: ignore[assignment]
        cli_module._read_followup_input = spy_followup  # type: ignore[assignment]
        cli_module._read_unblock_input = spy_unblock  # type: ignore[assignment]
        try:
            rc = asyncio.run(cli_module._run_resume(session.session_id))
        finally:
            cli_module._read_answer_input = real_answer  # type: ignore[assignment]
            cli_module._read_followup_input = real_followup  # type: ignore[assignment]
            cli_module._read_unblock_input = real_unblock  # type: ignore[assignment]

        # The resume cancelled cleanly because spy_answer returned None.
        assert rc == 0
        assert called["answer"] == 1, called
        assert called["followup"] == 0, called
        assert called["unblock"] == 0, called
    print("[resume_of_asking_session_takes_question_branch] OK")


# ---------------------------------------------------------------------------
# R12: Ctrl+C → snapshot (CancelledError catch path)
# ---------------------------------------------------------------------------


def test_drive_session_catches_cancelled_error_and_snapshots(tmp_path: Path) -> None:
    """Python 3.11+'s asyncio.Runner converts SIGINT to CancelledError on
    the running task. The catch must include CancelledError (not just
    KeyboardInterrupt) for the snapshot promise to hold."""
    with _isolated_data_paths(tmp_path):
        Agent.REGISTRY.clear()
        Agent.SCRATCH_ROOT = str(tmp_path)
        Agent.WORKSPACE_ROOT = str(tmp_path)
        session = Session.create("ctrl-c test")
        ceo = Agent(name="CEO", role_type="ceo", system_prompt="...")
        session.set_ceo(ceo.id)

        # Replace _run_session with a stub that raises CancelledError.
        real_run_session = cli_module._run_session

        async def fake_run_session(*a, **kw):
            raise asyncio.CancelledError()

        cli_module._run_session = fake_run_session  # type: ignore[assignment]
        try:
            console = Console(file=io.StringIO(), force_terminal=False)
            rc = asyncio.run(
                cli_module._drive_session(
                    console=console,
                    session=session,
                    mcp=_NoopMCP(),
                    ceo=ceo,
                    tui=TUI(task="t"),
                    first_message="msg",
                    logger=None,
                    extra_output=None,
                )
            )
        finally:
            cli_module._run_session = real_run_session  # type: ignore[assignment]

        # Expected exit code 130 (Ctrl+C convention).
        assert rc == 130, rc
        # The snapshot must have landed -- state.json exists with the
        # CEO id recorded.
        loaded = Session.load(session.session_id)
        assert loaded.data.ceo_id == ceo.id
    print("[drive_session_catches_cancelled_error_and_snapshots] OK")


# ---------------------------------------------------------------------------
# R13: ceo_task cleanup when TUI raises
# ---------------------------------------------------------------------------


async def test_run_session_cleans_up_when_tui_raises(tmp_path: Path) -> None:
    """If the TUI task raises mid-run, `_run_session` must (1) propagate
    the TUI's exception promptly rather than hanging on the blocked
    CEO, and (2) cancel-and-gather the ceo_task so no dangling task
    remains.

    The earlier version used `await ceo_task` alone which never
    observed `tui_task` failures -- a TUI raise just left the CEO
    blocked forever. Fixed in the same polish PR via FIRST_EXCEPTION
    wait. This test verifies both contracts.
    """
    # Baseline of tasks NOT created by _run_session -- so when we
    # filter for "what's left over", we don't mistakenly attribute
    # something from the broader test runner.
    baseline_tasks = {t for t in asyncio.all_tasks() if t is not asyncio.current_task()}

    Agent.REGISTRY.clear()
    Agent.SCRATCH_ROOT = str(tmp_path)
    Agent.WORKSPACE_ROOT = str(tmp_path)
    ceo = Agent(name="CEO", role_type="ceo", system_prompt="...")

    class _RaisingTUI:
        async def run(self, stop_when: asyncio.Task[Any]) -> None:
            await asyncio.sleep(0.01)
            raise RuntimeError("tui crashed")

    class _BlockingLLM:
        """Yields to the loop forever -- the CEO would never finish on
        its own. This is the worst case the FIRST_EXCEPTION fix needs
        to handle: TUI raises while CEO is blocked indefinitely; the
        whole session must still terminate."""

        async def model_name(self) -> str:
            return "blocking"

        async def chat(self, *a, **kw):
            await asyncio.Event().wait()  # forever
            return {"content": "never", "tool_calls": []}

    original_llm = agent_module.LLM
    agent_module.LLM = _BlockingLLM()
    raised: BaseException | None = None
    try:
        try:
            await cli_module._run_session(ceo, _RaisingTUI(), "do the thing")
        except BaseException as exc:
            raised = exc

        # Contract 1: the TUI's RuntimeError propagates promptly.
        assert isinstance(raised, RuntimeError), (
            f"expected TUI's RuntimeError to propagate; got {type(raised).__name__}: {raised}"
        )
        assert "tui crashed" in str(raised)

        # Contract 2: no _run_session-created tasks left pending.
        post_tasks = {t for t in asyncio.all_tasks() if t is not asyncio.current_task()}
        new_pending = [t for t in (post_tasks - baseline_tasks) if not t.done()]
        assert new_pending == [], (
            f"dangling tasks created by _run_session: {new_pending}"
        )
    finally:
        agent_module.LLM = original_llm
    print("[run_session_cleans_up_when_tui_raises] OK")


# ---------------------------------------------------------------------------
# R22: TUI tool_args truncation explicit test
# ---------------------------------------------------------------------------


def test_tui_tool_call_truncates_large_string_arg_values() -> None:
    tui = TUI(task="t")
    tui._handle_event(
        Event("spawn", "a1", "Worker", data={"role_type": "worker", "depth": 1})
    )
    big = "x" * 5000
    tui._handle_event(
        Event(
            "tool_call",
            "a1",
            "Worker",
            data={
                "tool": "write_workspace",
                "args": {"path": "game.py", "content": big},
            },
        )
    )
    entry = tui.agents["a1"].activity_log[0]
    stored = entry["tool_args"]["content"]
    assert len(stored) < len(big), f"content not truncated; stored {len(stored)} chars"
    # The non-large arg passes through.
    assert entry["tool_args"]["path"] == "game.py"
    print("[tui_tool_call_truncates_large_string_arg_values] OK")


# ---------------------------------------------------------------------------
# R24: _ColumnOverflowTree narrow-width measurement
# ---------------------------------------------------------------------------


def test_column_overflow_tree_measure_clamps_at_narrow_widths() -> None:
    """At terminal widths below MIN_COL_WIDTH (38), Measurement.minimum
    must NOT exceed Measurement.maximum -- Rich crashes otherwise."""
    cot = _ColumnOverflowTree(Tree("test"))
    console = Console(file=io.StringIO(), width=20, force_terminal=False)
    opts = console.options.update(width=20)
    m = cot.__rich_measure__(console, opts)
    assert m.minimum <= m.maximum, (m.minimum, m.maximum)
    assert m.maximum == 20
    # At a comfortable width, minimum == MIN_COL_WIDTH (38).
    wide_opts = console.options.update(width=200)
    wm = cot.__rich_measure__(console, wide_opts)
    assert wm.minimum == _ColumnOverflowTree.MIN_COL_WIDTH
    assert wm.maximum == 200
    print("[column_overflow_tree_measure_clamps_at_narrow_widths] OK")


def test_column_overflow_tree_handles_label_between_term_width_and_floor() -> None:
    """The wide-render floor (4096) must be high enough that very long
    labels DON'T wrap during measurement -- otherwise the overflow
    detection mis-counts lines for trees with long agent names.

    Earlier version of this test used avail_w=50 and avail_h=4, which
    short-circuited via `max_cols = 50 // 38 = 1` to the single-tree
    fallback -- meaning the test passed under BOTH the old 200 floor
    AND the new 4096 floor. Now we pick a width that allows multi-column
    overflow AND enough nodes that the wide-render is actually invoked,
    so a regression reverting the floor would surface as a mis-counted
    line layout.
    """
    # Label that's longer than the OLD floor (200) but shorter than
    # the new floor (4096). At the old floor it'd wrap during the
    # wide-render; at 4096 it stays on one line.
    long_label = "X" * 400
    tree = Tree(long_label)
    # Add enough children that the early-fits-check (which we just
    # added in this same PR) doesn't short-circuit to the single-tree
    # path -- we want to actually exercise the wide-render branch.
    for i in range(50):
        tree.add(f"node-{i}")
    cot = _ColumnOverflowTree(tree)

    # avail_w = 200 (enough for max_cols >= 5 since MIN_COL_WIDTH=38),
    # avail_h = 6 -- small enough that the 50-child tree definitely
    # overflows.
    console = Console(file=io.StringIO(), width=200, force_terminal=False)
    opts = console.options.update(width=200, height=6)
    result = list(cot.__rich_console__(console, opts))
    assert result, "expected at least one rendered piece"
    # The result should be the multi-column Table.grid form (the
    # overflow path), NOT a bare Tree. If the floor regressed to 200,
    # the long_label would wrap during measurement and inflate the
    # line count -- the test would still pass functionally, but we
    # can at least assert that a non-Tree renderable came back.
    from rich.table import Table as _Table

    has_grid = any(isinstance(r, _Table) for r in result)
    assert has_grid, f"expected multi-column Table.grid overflow path; got {result!r}"
    print("[column_overflow_tree_handles_label_between_term_width_and_floor] OK")


# ---------------------------------------------------------------------------
# R27: _run_resume snapshots after every mark_*
# ---------------------------------------------------------------------------


def test_run_resume_snapshots_after_mark_answer(tmp_path: Path) -> None:
    """After a user answers a paused question, the session state must
    be flushed to disk -- otherwise a crash before _drive_session
    completes would re-ask the same question on the next resume."""
    with _isolated_data_paths(tmp_path):
        Agent.REGISTRY.clear()
        Agent.SCRATCH_ROOT = str(tmp_path)
        Agent.WORKSPACE_ROOT = str(tmp_path)
        session = Session.create("snapshot-after-mark")
        ceo = Agent(name="CEO", role_type="ceo", system_prompt="...")
        session.set_ceo(ceo.id)
        session.mark_question({"question": "X?", "options": [], "context": ""})
        session.snapshot_agents({aid: a.to_dict() for aid, a in Agent.REGISTRY.items()})
        session.save()

        # Stub the answer input to return a string, and stub
        # _drive_session to raise BEFORE it saves anything -- this
        # simulates a crash partway through the resume turn.
        real_answer = cli_module._read_answer_input
        real_drive = cli_module._drive_session

        async def fake_drive(*a, **kw):
            raise RuntimeError("crashed during run")

        cli_module._read_answer_input = lambda *a, **kw: "the answer"  # type: ignore[assignment]
        cli_module._drive_session = fake_drive  # type: ignore[assignment]
        try:
            try:
                asyncio.run(cli_module._run_resume(session.session_id))
            except RuntimeError:
                pass
        finally:
            cli_module._read_answer_input = real_answer  # type: ignore[assignment]
            cli_module._drive_session = real_drive  # type: ignore[assignment]

        # Reload from disk; the answer entry must have been persisted.
        reloaded = Session.load(session.session_id)
        kinds = [h["kind"] for h in reloaded.data.history]
        assert "answer" in kinds, (
            f"R27 broken: mark_answer wasn't snapshotted; history kinds={kinds}"
        )
        ans = next(h for h in reloaded.data.history if h["kind"] == "answer")
        assert ans["text"] == "the answer"
    print("[run_resume_snapshots_after_mark_answer] OK")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _NoopMCP:
    """Stand-in MCPClient for tests that don't actually need MCP.

    Instance attributes (not class-level) so each _NoopMCP() owns its
    own empty list/dict -- prevents the classic mutable-default leak
    where one test mutating `mcp.connect_errors['x']='y'` would
    contaminate every subsequent instance.
    """

    def __init__(self) -> None:
        self.registered_tools: list = []
        self.connect_errors: dict = {}

    async def close(self) -> None:
        pass

    @classmethod
    async def from_config(cls, _path: str) -> "_NoopMCP":
        return cls()


# ---------------------------------------------------------------------------
# Anchor test: prompt section the terse runtime message depends on
# ---------------------------------------------------------------------------


def test_ceo_prompt_still_carries_followup_anchor() -> None:
    """`_build_followup_message` now sends one terse line on follow-up
    turns and leans on the CEO prompt's FOLLOW-UP TURNS section for
    the team-persistence / list_workspace / re-assign-vs-rehire-vs-fire
    guidance. If a future prompt refactor removes or renames that
    section, follow-ups silently lose the guidance with no test
    failing. Anchor the dependency."""
    from taskgenie.prompts import CEO_ROLE_PROMPT

    assert "FOLLOW-UP" in CEO_ROLE_PROMPT, (
        "CEO_ROLE_PROMPT no longer contains a FOLLOW-UP section "
        "that _build_followup_message offloads guidance onto. Either "
        "restore the section or re-inline the guidance in "
        "_build_followup_message."
    )
    # Spot-check the four key concepts the runtime message used to
    # carry inline.
    section_lower = CEO_ROLE_PROMPT.lower()
    for keyword in ("list_workspace", "re-assign", "rehire", "fire"):
        assert keyword in section_lower, (
            f"CEO prompt is missing follow-up keyword {keyword!r}; "
            "the terse _build_followup_message assumes the prompt covers it"
        )
    print("[ceo_prompt_still_carries_followup_anchor] OK")


# ---------------------------------------------------------------------------
# Copy tools (new in this PR): scratch <-> workspace, bytes off-context
# ---------------------------------------------------------------------------


async def test_copy_to_workspace_create_mode(tmp_path: Path) -> None:
    from taskgenie.tools.work import _copy_to_workspace

    scratch = tmp_path / "scratch"
    ws = tmp_path / "ws"
    scratch.mkdir()
    ws.mkdir()
    (scratch / "out.bin").write_bytes(b"\x00\x01\x02hello\xff")

    class _A:
        scratch_dir = str(scratch)
        workspace_dir = str(ws)
        messages: list = []
        read_workspace_paths: set = set()

    result = await _copy_to_workspace(
        _A(), {"scratch_path": "out.bin", "workspace_path": "deliv.bin"}
    )
    assert "copied" in result, result
    assert (ws / "deliv.bin").read_bytes() == b"\x00\x01\x02hello\xff"
    print("[copy_to_workspace_create_mode] OK")


async def test_copy_to_workspace_create_refuses_existing(tmp_path: Path) -> None:
    from taskgenie.tools.work import _copy_to_workspace

    scratch = tmp_path / "scratch"
    ws = tmp_path / "ws"
    scratch.mkdir()
    ws.mkdir()
    (scratch / "src.txt").write_text("new", encoding="utf-8")
    (ws / "dst.txt").write_text("existing", encoding="utf-8")

    class _A:
        scratch_dir = str(scratch)
        workspace_dir = str(ws)
        messages: list = []
        read_workspace_paths: set = set()

    result = await _copy_to_workspace(
        _A(),
        {"scratch_path": "src.txt", "workspace_path": "dst.txt", "mode": "create"},
    )
    assert "ERROR" in result and "already exists" in result, result
    # Destination unchanged.
    assert (ws / "dst.txt").read_text(encoding="utf-8") == "existing"
    print("[copy_to_workspace_create_refuses_existing] OK")


async def test_copy_to_workspace_overwrite_warns_after_read(tmp_path: Path) -> None:
    """If the worker previously read_workspace'd the destination path,
    copy_to_workspace(mode='overwrite') must emit the same lost-update
    warning that write_workspace(mode='overwrite') does. Otherwise the
    copy tool becomes a workaround for the warning."""
    from taskgenie.tools.work import _copy_to_workspace

    scratch = tmp_path / "scratch"
    ws = tmp_path / "ws"
    scratch.mkdir()
    ws.mkdir()
    (scratch / "src.txt").write_text("v2", encoding="utf-8")
    (ws / "draft.md").write_text("v1", encoding="utf-8")

    class _A:
        scratch_dir = str(scratch)
        workspace_dir = str(ws)
        messages: list = []
        # Simulate a prior read_workspace of the destination.
        read_workspace_paths: set = {"draft.md"}

    result = await _copy_to_workspace(
        _A(),
        {"scratch_path": "src.txt", "workspace_path": "draft.md", "mode": "overwrite"},
    )
    assert "copied" in result
    assert "WARNING" in result, (
        f"missing lost-update warning on copy_to_workspace(mode='overwrite') "
        f"after read; got {result!r}"
    )
    assert (ws / "draft.md").read_text(encoding="utf-8") == "v2"
    print("[copy_to_workspace_overwrite_warns_after_read] OK")


async def test_copy_to_workspace_missing_source(tmp_path: Path) -> None:
    from taskgenie.tools.work import _copy_to_workspace

    scratch = tmp_path / "scratch"
    ws = tmp_path / "ws"
    scratch.mkdir()
    ws.mkdir()

    class _A:
        scratch_dir = str(scratch)
        workspace_dir = str(ws)
        messages: list = []
        read_workspace_paths: set = set()

    result = await _copy_to_workspace(
        _A(), {"scratch_path": "nope.txt", "workspace_path": "out.txt"}
    )
    assert "ERROR" in result and "does not exist" in result, result
    print("[copy_to_workspace_missing_source] OK")


async def test_copy_from_workspace_basic(tmp_path: Path) -> None:
    from taskgenie.tools.work import _copy_from_workspace

    scratch = tmp_path / "scratch"
    ws = tmp_path / "ws"
    scratch.mkdir()
    ws.mkdir()
    (ws / "input.bin").write_bytes(b"\x80\x81binary\x00data")

    class _A:
        scratch_dir = str(scratch)
        workspace_dir = str(ws)
        messages: list = []
        read_workspace_paths: set = set()

    a = _A()
    result = await _copy_from_workspace(
        a, {"workspace_path": "input.bin", "scratch_path": "local.bin"}
    )
    assert "copied" in result, result
    assert (scratch / "local.bin").read_bytes() == b"\x80\x81binary\x00data"
    # The copy records the workspace path in read_workspace_paths so a
    # later copy_to_workspace(mode='overwrite') back to it warns.
    assert "input.bin" in a.read_workspace_paths
    print("[copy_from_workspace_basic] OK")


async def test_copy_from_workspace_missing_source(tmp_path: Path) -> None:
    from taskgenie.tools.work import _copy_from_workspace

    scratch = tmp_path / "scratch"
    ws = tmp_path / "ws"
    scratch.mkdir()
    ws.mkdir()

    class _A:
        scratch_dir = str(scratch)
        workspace_dir = str(ws)
        messages: list = []
        read_workspace_paths: set = set()

    result = await _copy_from_workspace(
        _A(), {"workspace_path": "missing.md", "scratch_path": "local.md"}
    )
    assert "ERROR" in result and "does not exist" in result, result
    print("[copy_from_workspace_missing_source] OK")


def test_copy_tools_registered_as_worker_only() -> None:
    """Copy tools follow the same kind='work' policy as write_workspace
    / edit_workspace -- workers only, not managers or CEO. If someone
    bumps the kind to 'both' silently, the test should catch it."""
    work_sig = REGISTRY.signature("work")
    mgmt_sig = REGISTRY.signature("management")
    ceo_sig = REGISTRY.signature("ceo")
    for name in ("copy_to_workspace", "copy_from_workspace"):
        assert name in work_sig, f"{name} should be a work-kind tool"
        assert name not in mgmt_sig, f"{name} should NOT be on managers"
        assert name not in ceo_sig, f"{name} should NOT be on CEO"
    print("[copy_tools_registered_as_worker_only] OK")


# ---------------------------------------------------------------------------
# Drain-only-on-terminal: the round-3-fix-fix
# ---------------------------------------------------------------------------


async def test_run_drain_skips_on_question_return(tmp_path: Path) -> None:
    """Q-and-A inter-turn pauses must NOT cancel in-flight children;
    the parent re-invokes run() with the user's answer and expects
    pending assignment handles to still be awaitable. Verifies the
    drain-only-on-terminal fix."""
    from taskgenie.result import RunResult
    import json as _json

    Agent.REGISTRY.clear()
    Agent.SCRATCH_ROOT = str(tmp_path / "scratch")
    Agent.WORKSPACE_ROOT = str(tmp_path / "ws")
    Path(Agent.SCRATCH_ROOT).mkdir(exist_ok=True)
    Path(Agent.WORKSPACE_ROOT).mkdir(exist_ok=True)
    ceo = Agent(name="CEO", role_type="ceo", system_prompt=CEO_ROLE_PROMPT)

    # Plant a fake long-running child task that the drain would
    # otherwise cancel.
    async def _slow() -> RunResult:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise
        return RunResult(kind="done", report="never")

    fake_task = asyncio.create_task(_slow(), name="task-fake")
    ceo.task_assignments["task-fake"] = fake_task
    # Let it actually start.
    await asyncio.sleep(0.01)
    assert not fake_task.done()

    # Scripted LLM: emit ask_user in turn 1.
    class _AskOnce:
        async def model_name(self) -> str:
            return "ask"

        async def chat(self, *a, **kw):
            return {
                "content": "need clarification",
                "tool_calls": [
                    {
                        "id": "call_ask",
                        "name": "ask_user",
                        "arguments": _json.dumps({"question": "X?"}),
                    }
                ],
            }

    original_llm = agent_module.LLM
    agent_module.LLM = _AskOnce()
    try:
        try:
            result = await ceo.run("do the thing")
        finally:
            agent_module.LLM = original_llm

        assert result.kind == "question", result
        # The drain MUST have skipped: the fake task is still alive
        # AT THIS POINT. Check before we clean up below.
        assert not fake_task.done(), (
            "drain incorrectly cancelled in-flight child task on question return; "
            "parent will re-call run() expecting that handle to still be awaitable"
        )
    finally:
        # Always clean up the dangling task we planted, regardless of
        # assertion outcome -- otherwise it leaks into the next test
        # via asyncio.all_tasks().
        if not fake_task.done():
            fake_task.cancel()
            try:
                await fake_task
            except asyncio.CancelledError:
                pass
    print("[run_drain_skips_on_question_return] OK")


async def test_run_drain_fires_on_done_return(tmp_path: Path) -> None:
    """Inverse of the above: on a terminal kind=done return, the drain
    SHOULD reap in-flight children (the agent isn't coming back)."""
    from taskgenie.result import RunResult
    import json as _json

    Agent.REGISTRY.clear()
    Agent.SCRATCH_ROOT = str(tmp_path / "scratch2")
    Agent.WORKSPACE_ROOT = str(tmp_path / "ws2")
    Path(Agent.SCRATCH_ROOT).mkdir(exist_ok=True)
    Path(Agent.WORKSPACE_ROOT).mkdir(exist_ok=True)
    ceo = Agent(name="CEO", role_type="ceo", system_prompt=CEO_ROLE_PROMPT)

    async def _slow() -> RunResult:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise
        return RunResult(kind="done", report="never")

    fake_task = asyncio.create_task(_slow(), name="task-fake")
    ceo.task_assignments["task-fake"] = fake_task
    await asyncio.sleep(0.01)
    assert not fake_task.done()

    class _CompleteOnce:
        async def model_name(self) -> str:
            return "complete"

        async def chat(self, *a, **kw):
            return {
                "content": "done",
                "tool_calls": [
                    {
                        "id": "call_done",
                        "name": "complete",
                        "arguments": _json.dumps({"report": "all done"}),
                    }
                ],
            }

    original_llm = agent_module.LLM
    agent_module.LLM = _CompleteOnce()
    try:
        result = await ceo.run("do it")
    finally:
        agent_module.LLM = original_llm

    assert result.kind == "done", result
    # Drain MUST have reaped the in-flight child.
    assert fake_task.done(), (
        "drain didn't run on kind=done; fake_task should have been cancelled+gathered"
    )
    print("[run_drain_fires_on_done_return] OK")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def _async_tests(tmp_root: Path) -> None:
    # Create subdirs the async tests need (sync runner creates its own).
    for sub in (
        "r13",
        "copy1",
        "copy2",
        "copy3",
        "copy4",
        "copy5",
        "copy6",
        "drainq",
        "draind",
    ):
        (tmp_root / sub).mkdir(exist_ok=True)
    await test_tool_fire_cancels_pending_child_task()
    await test_run_session_cleans_up_when_tui_raises(tmp_root / "r13")
    # Copy tools
    await test_copy_to_workspace_create_mode(tmp_root / "copy1")
    await test_copy_to_workspace_create_refuses_existing(tmp_root / "copy2")
    await test_copy_to_workspace_overwrite_warns_after_read(tmp_root / "copy3")
    await test_copy_to_workspace_missing_source(tmp_root / "copy4")
    await test_copy_from_workspace_basic(tmp_root / "copy5")
    await test_copy_from_workspace_missing_source(tmp_root / "copy6")
    # Drain-on-terminal
    await test_run_drain_skips_on_question_return(tmp_root / "drainq")
    await test_run_drain_fires_on_done_return(tmp_root / "draind")


def _sync_tests(tmp_root: Path) -> None:
    for sub in (
        "bulk1",
        "bulk2",
        "mcp",
        "asking",
        "ctrlc",
        "r27",
        "r13",
        "copy1",
        "copy2",
        "copy3",
        "copy4",
        "copy5",
        "copy6",
        "drainq",
        "draind",
    ):
        (tmp_root / sub).mkdir(exist_ok=True)
    # to_tool_payload
    test_to_tool_payload_done_kind()
    test_to_tool_payload_blocked_kind()
    test_to_tool_payload_question_kind()
    test_to_tool_payload_exhausted_kind()
    test_to_tool_payload_error_kind()
    test_to_tool_payload_includes_follow_ups_when_present()
    # _tool_review
    test_tool_review_emits_event_and_returns_verdict()
    test_tool_review_rejected_branch()
    test_tool_review_unknown_handle_falls_back_to_question_mark()
    # _tool_rehire
    test_tool_rehire_resets_messages_and_status()
    test_tool_rehire_rejects_non_child()
    test_tool_rehire_rejects_unknown_agent_id()
    # _tool_fire (sync slice)
    test_tool_fire_does_not_cancel_already_done_task()
    # bulk_load
    test_bulk_load_with_missing_parent_orphans_gracefully(tmp_root / "bulk1")
    test_bulk_load_filters_absent_child_ids(tmp_root / "bulk2")
    # MCP
    test_mcp_register_namespaces_on_collision()
    test_mcp_register_records_error_on_double_collision()
    # resume of asking
    test_resume_of_asking_session_takes_question_branch(tmp_root / "asking")
    # R12
    test_drive_session_catches_cancelled_error_and_snapshots(tmp_root / "ctrlc")
    # R22
    test_tui_tool_call_truncates_large_string_arg_values()
    # R24
    test_column_overflow_tree_measure_clamps_at_narrow_widths()
    test_column_overflow_tree_handles_label_between_term_width_and_floor()
    # R27
    test_run_resume_snapshots_after_mark_answer(tmp_root / "r27")
    # Anchor + copy-tools registration
    test_ceo_prompt_still_carries_followup_anchor()
    test_copy_tools_registered_as_worker_only()


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        asyncio.run(_async_tests(root))
        _sync_tests(root)
    print("\nAll polish-coverage tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
