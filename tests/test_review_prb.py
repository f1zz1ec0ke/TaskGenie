"""Tests for PR B -- resume hardening + production-grade tests for paths
that were previously uncovered.

B1. Session.load now filters unknown fields, raises ValueError on
    corrupt JSON or missing required fields, and exposes unknown fields
    as _unknown_fields_on_load for diagnostics.

B2. diff_tools_against now also covers ceo-only tools (ask_user etc.).

B3. LLM transport failure -> RunResult(kind="error")
B4. Turn-limit exhaustion -> RunResult(kind="exhausted")
B5. _tool_hire returns an error dict when depth >= max_depth
B6. Malformed tool JSON arguments don't crash; agent recovers and
    continues with a [tool_args_error] tool message.

Run with: python -m tests.test_review_prb
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from taskgenie import agent as agent_module
from taskgenie.agent import Agent
from taskgenie.config import CONFIG
from taskgenie.prompts import CEO_ROLE_PROMPT
from taskgenie.session import Session, SessionData, STATE_FILE
from taskgenie.tools.registry import REGISTRY


# ---------------------------------------------------------------------------
# Shared LLM mocks
# ---------------------------------------------------------------------------


def _tc(call_id: str, tool_name: str, **args):
    return {"id": call_id, "name": tool_name, "arguments": json.dumps(args)}


class _RaisingLLM:
    """Always raises -- simulates a transport error."""

    async def model_name(self) -> str:
        return "raises"

    async def chat(self, messages, tools=None):
        raise ConnectionError("boom: cannot reach LLM")


class _LoopingNoOpLLM:
    """Returns a tool call that does NOT terminate the agent loop, every
    turn. Specifically `suggest_follow_up` -- it appends a tool message
    and continues; it never sets done/blocked/exhausted on its own. So
    the outer for-_turn loop hits max_turns_per_agent and falls through
    to the "exhausted" path. Using `complete` or empty turns instead
    would short-circuit before exhaustion."""

    def __init__(self) -> None:
        self._n = 0

    async def model_name(self) -> str:
        return "loop"

    async def chat(self, messages, tools=None):
        self._n += 1
        return {
            "content": "still going",
            "tool_calls": [
                _tc(
                    f"fu_{self._n}",
                    "suggest_follow_up",
                    what_is_needed=f"thing #{self._n}",
                    rationale="reason",
                    who_should_do_it="someone",
                )
            ],
        }


class _MalformedToolArgsLLM:
    """Returns one bad-JSON tool call, then on retry returns complete.
    Verifies the agent's loop recovers from the JSON error."""

    def __init__(self) -> None:
        self._turn = 0

    async def model_name(self) -> str:
        return "bad-json"

    async def chat(self, messages, tools=None):
        turn = self._turn
        self._turn += 1
        if turn == 0:
            # Hand-craft an arguments string that isn't valid JSON.
            return {
                "content": "trying tool",
                "tool_calls": [
                    {"id": "bad_1", "name": "hire", "arguments": "{not: json}"}
                ],
            }
        return {
            "content": "done",
            "tool_calls": [_tc("good_1", "complete", report="ok")],
        }


def _new_ceo() -> Agent:
    Agent.REGISTRY.clear()
    with tempfile.TemporaryDirectory() as d:
        Agent.SCRATCH_ROOT = d
        Agent.WORKSPACE_ROOT = d
        return Agent(name="CEO", role_type="ceo", system_prompt=CEO_ROLE_PROMPT)


# ---------------------------------------------------------------------------
# B1: Session.load defensive parsing
# ---------------------------------------------------------------------------


def _make_session_with_state(tmp_path: Path, state: dict) -> str:
    """Write a state.json directly to disk and return the session_id."""
    object.__setattr__(CONFIG, "data_dir", str(tmp_path / "data"))
    CONFIG.sessions_dir.mkdir(parents=True, exist_ok=True)
    session_id = state["session_id"]
    sub = CONFIG.sessions_dir / session_id
    sub.mkdir(parents=True, exist_ok=True)
    (sub / STATE_FILE).write_text(json.dumps(state), encoding="utf-8")
    return session_id


def _valid_state(session_id: str = "test-sess-1") -> dict:
    """Minimal valid state.json shape."""
    return asdict(
        SessionData(
            session_id=session_id,
            task="test",
            started_at="2026-01-01T00:00:00",
            last_saved_at="2026-01-01T00:00:00",
            status="running",
            ceo_id=None,
        )
    )


def test_load_filters_unknown_future_fields(tmp_path: Path) -> None:
    """A state.json written by a newer version with extra fields should
    still load on an older binary, with the extras tracked for diagnostics."""
    state = _valid_state("load-future-1")
    state["_unrecognised_future_field"] = "from-the-future"
    state["agents_v2"] = {"some": "shape"}
    session_id = _make_session_with_state(tmp_path, state)

    session = Session.load(session_id)
    assert session.data.session_id == session_id
    assert session.data.task == "test"
    # Unknown fields are tracked but didn't crash the load.
    unknown = getattr(session, "_unknown_fields_on_load", [])
    assert "_unrecognised_future_field" in unknown
    assert "agents_v2" in unknown
    print("[load_filters_unknown_future_fields] OK")


def test_load_rejects_corrupt_json(tmp_path: Path) -> None:
    """Half-written / hand-mangled state.json should raise a friendly
    ValueError, not a bare JSONDecodeError that leaks the message."""
    object.__setattr__(CONFIG, "data_dir", str(tmp_path / "data"))
    CONFIG.sessions_dir.mkdir(parents=True, exist_ok=True)
    session_id = "corrupt-sess"
    sub = CONFIG.sessions_dir / session_id
    sub.mkdir(parents=True, exist_ok=True)
    (sub / STATE_FILE).write_text("{ this is not json", encoding="utf-8")

    try:
        Session.load(session_id)
    except ValueError as e:
        assert "corrupt" in str(e).lower()
        assert session_id in str(e)
        print("[load_rejects_corrupt_json] OK")
        return
    raise AssertionError("expected ValueError for corrupt state.json")


def test_load_rejects_missing_required_field(tmp_path: Path) -> None:
    """An incomplete state.json (missing a required dataclass field) must
    raise a friendly ValueError naming what's missing."""
    state = _valid_state("missing-sess")
    del state["task"]  # task is required, no default
    session_id = _make_session_with_state(tmp_path, state)
    try:
        Session.load(session_id)
    except ValueError as e:
        assert "missing" in str(e).lower()
        print("[load_rejects_missing_required_field] OK")
        return
    raise AssertionError("expected ValueError for missing required field")


def test_load_rejects_non_object_root(tmp_path: Path) -> None:
    """state.json must be a JSON object, not a list or scalar."""
    object.__setattr__(CONFIG, "data_dir", str(tmp_path / "data"))
    CONFIG.sessions_dir.mkdir(parents=True, exist_ok=True)
    session_id = "list-sess"
    sub = CONFIG.sessions_dir / session_id
    sub.mkdir(parents=True, exist_ok=True)
    (sub / STATE_FILE).write_text("[1, 2, 3]", encoding="utf-8")
    try:
        Session.load(session_id)
    except ValueError:
        print("[load_rejects_non_object_root] OK")
        return
    raise AssertionError("expected ValueError for non-object root")


# ---------------------------------------------------------------------------
# B2: CEO-only tools are persisted + diffed
# ---------------------------------------------------------------------------


def test_ceo_tool_change_surfaces_in_diff(tmp_path: Path) -> None:
    """A change to a ceo-only tool (e.g. ask_user) must show up in the
    resume-time tool diff. Pre-fix, the ceo bucket was never persisted."""
    object.__setattr__(CONFIG, "data_dir", str(tmp_path / "data"))
    CONFIG.sessions_dir.mkdir(parents=True, exist_ok=True)
    Agent.REGISTRY.clear()
    session = Session.create("ceo-diff-test")
    session.save()

    # Persisted signature must include a "ceo" bucket.
    assert "ceo" in session.data.tool_signature
    saved_ceo_names = set(session.data.tool_signature["ceo"])
    assert "ask_user" in saved_ceo_names

    # Simulate the ceo-only schema changing by registering a NEW ceo-only tool.
    fake_schema = {
        "type": "function",
        "function": {
            "name": "_fake_ceo_tool",
            "description": "test only",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    REGISTRY.register(fake_schema, kind="ceo", version="1.0")
    try:
        # Reload and diff.
        reloaded = Session.load(session.session_id)
        diff = reloaded.diff_tools_against(
            REGISTRY.signature("management"),
            REGISTRY.signature("work"),
            REGISTRY.signature("ceo"),
        )
        assert diff is not None
        assert "_fake_ceo_tool" in diff
        assert "CEO-only tools" in diff
    finally:
        REGISTRY.unregister("_fake_ceo_tool")
    print("[ceo_tool_change_surfaces_in_diff] OK")


# ---------------------------------------------------------------------------
# B3: LLM transport failure
# ---------------------------------------------------------------------------


async def test_llm_transport_failure_returns_error_result() -> None:
    ceo = _new_ceo()
    agent_module.LLM = _RaisingLLM()
    result = await ceo.run("do the thing")
    assert result.kind == "error", result
    assert "LLM failure" in (result.error or "")
    # Agent status is set to error.
    assert ceo.status == "error"
    print("[llm_transport_failure_returns_error_result] OK")


# ---------------------------------------------------------------------------
# B4: turn-limit exhaustion
# ---------------------------------------------------------------------------


async def test_turn_limit_exhaustion_returns_exhausted() -> None:
    """If the LLM keeps responding with no tool calls forever, the agent
    eventually returns kind="exhausted". Monkeypatch the cap to keep this
    test fast."""
    original_max = CONFIG.max_turns_per_agent
    object.__setattr__(CONFIG, "max_turns_per_agent", 3)
    try:
        ceo = _new_ceo()
        agent_module.LLM = _LoopingNoOpLLM()
        result = await ceo.run("loop forever")
        assert result.kind == "exhausted", result
        assert ceo.status == "exhausted"
    finally:
        object.__setattr__(CONFIG, "max_turns_per_agent", original_max)
    print("[turn_limit_exhaustion_returns_exhausted] OK")


# ---------------------------------------------------------------------------
# B5: max_depth cap on hire
# ---------------------------------------------------------------------------


def test_hire_at_max_depth_returns_error() -> None:
    """An agent at depth >= max_depth must refuse to hire and return an
    error dict. Pre-fix this was never tested -- off-by-one prone."""
    Agent.REGISTRY.clear()
    with tempfile.TemporaryDirectory() as d:
        Agent.SCRATCH_ROOT = d
        Agent.WORKSPACE_ROOT = d
        # Build a chain up to exactly max_depth.
        ceo = Agent(name="CEO", role_type="ceo", system_prompt="...")
        current = ceo
        for i in range(CONFIG.max_depth):
            child = Agent(
                name=f"Mgr{i}",
                role_type="manager",
                system_prompt="...",
                parent=current,
                depth=current.depth + 1,
            )
            current.children.append(child)
            current = child
        # `current` is now at depth == CONFIG.max_depth.
        assert current.depth == CONFIG.max_depth
        result = current._tool_hire(
            name="OneTooDeep", role_type="worker", role_prompt="..."
        )
        assert isinstance(result, dict)
        assert "error" in result
        assert "max hierarchy depth" in result["error"]
        # Critical: nothing got added to REGISTRY.
        assert not any(a.name == "OneTooDeep" for a in Agent.REGISTRY.values())
    print("[hire_at_max_depth_returns_error] OK")


def test_hire_below_max_depth_succeeds() -> None:
    """Sanity check that hires below the cap still work -- so we know
    the cap isn't off-by-one in the opposite direction."""
    Agent.REGISTRY.clear()
    with tempfile.TemporaryDirectory() as d:
        Agent.SCRATCH_ROOT = d
        Agent.WORKSPACE_ROOT = d
        ceo = Agent(name="CEO", role_type="ceo", system_prompt="...")
        assert ceo.depth == 0
        result = ceo._tool_hire(
            name="Mgr", role_type="manager", role_prompt="..."
        )
        # _tool_hire returns a dict with "agent_id" on success.
        assert isinstance(result, dict)
        assert "error" not in result, result
        assert "agent_id" in result
    print("[hire_below_max_depth_succeeds] OK")


# ---------------------------------------------------------------------------
# B6: malformed JSON in tool args
# ---------------------------------------------------------------------------


async def test_malformed_tool_args_does_not_crash() -> None:
    """The agent must recover from a tool_call whose arguments aren't valid
    JSON, log a [tool_args_error] tool message, and continue the loop."""
    ceo = _new_ceo()
    agent_module.LLM = _MalformedToolArgsLLM()
    result = await ceo.run("try a thing")
    assert result.kind == "done", result
    # Find the [tool_args_error] message in history.
    err_msgs = [
        m for m in ceo.messages
        if m.get("role") == "tool" and "[tool_args_error]" in m.get("content", "")
    ]
    assert len(err_msgs) == 1, f"expected one tool_args_error, got {err_msgs}"
    print("[malformed_tool_args_does_not_crash] OK")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def _async_main() -> int:
    await test_llm_transport_failure_returns_error_result()
    await test_turn_limit_exhaustion_returns_exhausted()
    await test_malformed_tool_args_does_not_crash()
    return 0


def main() -> int:
    asyncio.run(_async_main())
    with tempfile.TemporaryDirectory() as d:
        test_load_filters_unknown_future_fields(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_load_rejects_corrupt_json(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_load_rejects_missing_required_field(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_load_rejects_non_object_root(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_ceo_tool_change_surfaces_in_diff(Path(d))
    test_hire_at_max_depth_returns_error()
    test_hire_below_max_depth_succeeds()
    print("\nAll PR B regression tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
