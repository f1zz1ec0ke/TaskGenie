"""Tests for PR D -- context-window management.

D1. count_tokens(messages) returns sensible estimates that scale with
    content size + tool_call arguments.

D2. dump_large_tool_result dumps tool output to scratch when over
    threshold, returns a pointer with the filename, and is a no-op
    below threshold.

D3. The Archivist (compact_messages) preserves system + original task
    + recent turns; replaces the middle band with a [archived summary]
    message; falls back to the original list on LLM failure.

D4. Agent integration:
    - Tool results bigger than the dump threshold land as pointers in
      self.messages (not the full text).
    - Pre-call compaction triggers when messages exceed the threshold.
    - A context-length error from LLM.chat triggers a force-compact +
      retry path.
    - _is_context_length_error pattern-matches plausible markers.

Run with: python -m tests.test_review_prd
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from taskgenie import agent as agent_module
from taskgenie import archivist as archivist_module
from taskgenie.agent import Agent, _is_context_length_error
from taskgenie.config import CONFIG
from taskgenie.context import (
    count_tokens,
    dump_large_tool_result,
    should_compact,
)
from taskgenie.prompts import CEO_ROLE_PROMPT


def _tc(call_id: str, tool_name: str, **args):
    return {"id": call_id, "name": tool_name, "arguments": json.dumps(args)}


# ---------------------------------------------------------------------------
# D1: token counting
# ---------------------------------------------------------------------------


def test_count_tokens_scales_with_content() -> None:
    """More chars = more estimated tokens (monotonic)."""
    short = [{"role": "user", "content": "hi"}]
    long_ = [{"role": "user", "content": "x" * 4000}]
    assert count_tokens(short) < 50  # constants dominate
    assert count_tokens(long_) > 900  # ~4000/4
    assert count_tokens(long_) > count_tokens(short)
    print("[count_tokens_scales_with_content] OK")


def test_count_tokens_includes_tool_call_args() -> None:
    """Tool-call argument JSON must count -- it's a real prompt-token cost."""
    no_tc = [{"role": "assistant", "content": "fine"}]
    with_tc = [
        {
            "role": "assistant",
            "content": "calling tool",
            "tool_calls": [
                {
                    "id": "x",
                    "function": {
                        "name": "write_workspace",
                        "arguments": json.dumps({"content": "y" * 4000}),
                    },
                }
            ],
        }
    ]
    assert count_tokens(with_tc) > count_tokens(no_tc) + 800
    print("[count_tokens_includes_tool_call_args] OK")


def test_should_compact_threshold() -> None:
    """should_compact respects ctx_window * pct."""
    small = [{"role": "user", "content": "x" * 100}]
    big = [{"role": "user", "content": "x" * 100_000}]
    # ctx_window=10k tokens (40k chars), pct=0.5 => trigger at 5k tokens (20k chars)
    assert should_compact(small, 10_000, 0.5) is False
    assert should_compact(big, 10_000, 0.5) is True
    # Degenerate cases
    assert should_compact(big, 0, 0.5) is False
    assert should_compact(big, 10_000, 0.0) is False
    print("[should_compact_threshold] OK")


# ---------------------------------------------------------------------------
# D2: dump_large_tool_result
# ---------------------------------------------------------------------------


def test_dump_below_threshold_is_noop(tmp_path: Path) -> None:
    """Small result: returned unchanged, no file written."""
    content, path = dump_large_tool_result(
        "small output",
        scratch_dir=str(tmp_path),
        turn_idx=0,
        call_id="abc",
        tool_name="exec_code",
        threshold_bytes=4096,
    )
    assert content == "small output"
    assert path is None
    assert list(tmp_path.iterdir()) == []
    print("[dump_below_threshold_is_noop] OK")


def test_dump_above_threshold_writes_file(tmp_path: Path) -> None:
    """Large result: dumped to scratch, in-memory content becomes a pointer."""
    big = "X" * 8000
    content, path = dump_large_tool_result(
        big,
        scratch_dir=str(tmp_path),
        turn_idx=14,
        call_id="call_xyz",
        tool_name="exec_code",
        threshold_bytes=4096,
    )
    assert path is not None
    # The pointer mentions the tool and gives the filename to read_file.
    assert "tool output dumped" in content
    assert "exec_code" in content
    assert "read_file" in content
    # The on-disk file has the full content verbatim.
    written = Path(path).read_text(encoding="utf-8")
    assert written == big
    # And the filename in the pointer matches what's on disk.
    assert Path(path).name in content
    print("[dump_above_threshold_writes_file] OK")


def test_dump_sanitises_filename(tmp_path: Path) -> None:
    """call_id with weird chars must not poison the filename."""
    big = "Y" * 8000
    _, path = dump_large_tool_result(
        big,
        scratch_dir=str(tmp_path),
        turn_idx=0,
        call_id="../escape; rm -rf /",
        tool_name="evil/tool/name",
        threshold_bytes=4096,
    )
    assert path is not None
    assert ".." not in Path(path).name
    assert "/" not in Path(path).name
    assert ";" not in Path(path).name
    # File still landed inside scratch dir.
    assert tmp_path in Path(path).parents
    print("[dump_sanitises_filename] OK")


# ---------------------------------------------------------------------------
# D3: Archivist
# ---------------------------------------------------------------------------


class _SummaryLLM:
    """Mock LLM that returns a fixed summary paragraph."""

    def __init__(self, summary: str = "the agent did stuff and wrote files") -> None:
        self.summary = summary
        self.last_input: list[dict] | None = None

    async def model_name(self) -> str:
        return "summary-mock"

    async def chat(self, messages, tools=None, **_kwargs):
        self.last_input = messages
        return {"content": self.summary, "tool_calls": [], "usage": {}}


class _FailingLLM:
    async def model_name(self) -> str:
        return "fail-mock"

    async def chat(self, messages, tools=None, **_kwargs):
        raise RuntimeError("simulated LLM failure")


async def test_archivist_compacts_middle_messages() -> None:
    """Archivist preserves system + task + recent N; collapses middle to one
    [archived summary] message."""
    msgs = [
        {"role": "system", "content": "You are W."},
        {"role": "user", "content": "Build the thing."},
    ]
    for i in range(20):
        msgs.append({"role": "assistant", "content": f"step {i}"})
        msgs.append({"role": "tool", "tool_call_id": f"t{i}", "content": f"result {i}"})

    fake = _SummaryLLM(summary="built half the thing; created foo.py, bar.py")
    original_llm = archivist_module.LLM
    archivist_module.LLM = fake
    try:
        new_msgs = await archivist_module.compact_messages("Worker", msgs, keep_recent=6)
    finally:
        archivist_module.LLM = original_llm

    # Structure: system, task, summary, then last 6.
    assert new_msgs[0] == msgs[0]
    assert new_msgs[1] == msgs[1]
    assert new_msgs[2]["role"] == "user"
    assert "[archived summary" in new_msgs[2]["content"]
    assert "built half the thing" in new_msgs[2]["content"]
    assert new_msgs[-6:] == msgs[-6:]
    # And the total shrank.
    assert len(new_msgs) < len(msgs)
    print("[archivist_compacts_middle_messages] OK")


async def test_archivist_falls_back_on_llm_failure() -> None:
    """If the Archivist's own LLM call fails, return the original messages
    so the caller can decide what to do (don't lose history)."""
    msgs = [
        {"role": "system", "content": "..."},
        {"role": "user", "content": "task"},
        *[
            {"role": "assistant", "content": f"step {i}"}
            for i in range(10)
        ],
    ]
    original_llm = archivist_module.LLM
    archivist_module.LLM = _FailingLLM()
    try:
        new_msgs = await archivist_module.compact_messages("W", msgs, keep_recent=2)
    finally:
        archivist_module.LLM = original_llm

    # Returned unchanged.
    assert new_msgs == msgs
    print("[archivist_falls_back_on_llm_failure] OK")


async def test_archivist_noop_for_short_history() -> None:
    """If there's nothing meaningful to compact, return the input."""
    msgs = [
        {"role": "system", "content": "..."},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "done"},
    ]
    # LLM should never be called for a no-op compaction.
    original_llm = archivist_module.LLM
    archivist_module.LLM = _FailingLLM()  # would raise if invoked
    try:
        new_msgs = await archivist_module.compact_messages("W", msgs, keep_recent=6)
    finally:
        archivist_module.LLM = original_llm

    assert new_msgs == msgs
    print("[archivist_noop_for_short_history] OK")


# ---------------------------------------------------------------------------
# D4: Agent integration
# ---------------------------------------------------------------------------


def test_is_context_length_error_pattern_match() -> None:
    assert _is_context_length_error(RuntimeError("context_length_exceeded"))
    assert _is_context_length_error(RuntimeError("maximum context length is 128000"))
    assert _is_context_length_error(RuntimeError("input too many tokens for model"))
    assert _is_context_length_error(RuntimeError("context window exceeded"))
    assert not _is_context_length_error(RuntimeError("connection refused"))
    assert not _is_context_length_error(RuntimeError("rate limit"))
    print("[is_context_length_error_pattern_match] OK")


class _BigToolThenCompleteLLM:
    """First turn: returns a read_file call that produces a huge result via
    a custom tool we'll inject. Second turn: completes."""

    def __init__(self) -> None:
        self._n = 0

    async def model_name(self) -> str:
        return "big-tool"

    async def chat(self, messages, tools=None, **_kwargs):
        self._n += 1
        if self._n == 1:
            return {
                "content": "reading",
                "tool_calls": [_tc("c1", "write_file", path="hello.txt", content="hi")],
            }
        return {
            "content": "done",
            "tool_calls": [_tc("c2", "complete", report="ok")],
        }


def _new_worker(tmp_path: Path) -> Agent:
    """Create a worker-like agent we can run end-to-end with the scripted
    LLM. Worker because it has access to write_file / read_file."""
    Agent.REGISTRY.clear()
    Agent.SCRATCH_ROOT = str(tmp_path)
    Agent.WORKSPACE_ROOT = str(tmp_path)
    return Agent(
        name="Worker",
        role_type="worker",
        system_prompt="You are a worker.",
    )


async def test_large_tool_result_dumped_to_scratch(tmp_path: Path) -> None:
    """End-to-end: a tool that returns a huge string lands in messages as a
    pointer, the full text is on disk in scratch, and no over-threshold
    blob remains inline."""
    Agent.REGISTRY.clear()
    Agent.SCRATCH_ROOT = str(tmp_path / "scratch")
    Agent.WORKSPACE_ROOT = str(tmp_path / "workspace")
    worker = Agent(
        name="HeavyWorker",
        role_type="worker",
        system_prompt="...",
    )

    # Inject a fake tool that returns 8KB. Register it as a work-kind tool.
    from taskgenie.tools.registry import REGISTRY

    big_schema = {
        "type": "function",
        "function": {
            "name": "_fake_huge_tool",
            "description": "test only",
            "parameters": {"type": "object", "properties": {}},
        },
    }

    async def dispatcher(agent, args):
        return "Z" * 8192

    REGISTRY.register(
        big_schema, kind="work", version="1.0", dispatcher=dispatcher
    )
    # Drive the threshold from below the 8KB fake output so this test
    # stays independent of CONFIG.dump_tool_output_above_bytes's default.
    original_dump_threshold = CONFIG.dump_tool_output_above_bytes
    object.__setattr__(CONFIG, "dump_tool_output_above_bytes", 4096)
    try:
        class _LLM:
            _n = 0

            async def model_name(self):
                return "mock"

            async def chat(self, messages, tools=None, **_kwargs):
                self._n += 1
                if self._n == 1:
                    return {
                        "content": "calling big tool",
                        "tool_calls": [_tc("c1", "_fake_huge_tool")],
                    }
                return {
                    "content": "done",
                    "tool_calls": [_tc("c2", "complete", report="ok")],
                }

        original_llm = agent_module.LLM
        agent_module.LLM = _LLM()
        try:
            result = await worker.run("do it")
        finally:
            agent_module.LLM = original_llm

        assert result.kind == "done", result
        # Find the tool message for c1.
        tool_msgs = [
            m for m in worker.messages
            if m.get("role") == "tool" and m.get("tool_call_id") == "c1"
        ]
        assert len(tool_msgs) == 1
        content = tool_msgs[0]["content"]
        # Must be the pointer, not the 8KB blob.
        assert len(content) < 1000, f"in-memory tool content not dumped: {len(content)} bytes"
        assert "tool output dumped" in content
        assert "read_file" in content
        # And the 8KB really is on disk somewhere in scratch.
        scratch_files = list(Path(worker.scratch_dir).glob("tool-*.log"))
        assert scratch_files, "no dumped log file in scratch"
        assert scratch_files[0].read_text(encoding="utf-8") == "Z" * 8192
    finally:
        REGISTRY.unregister("_fake_huge_tool")
        object.__setattr__(
            CONFIG, "dump_tool_output_above_bytes", original_dump_threshold
        )
    print("[large_tool_result_dumped_to_scratch] OK")


class _CountingLLM:
    """Records every messages list it's called with so we can check whether
    compaction shrank the list before the next call."""

    def __init__(self, summary_provider: bool = False) -> None:
        self._n = 0
        self.message_counts: list[int] = []
        self.summary_provider = summary_provider

    async def model_name(self):
        return "counting"

    async def chat(self, messages, tools=None, **_kwargs):
        self.message_counts.append(len(messages))
        self._n += 1
        # If we're acting as both the agent's LLM and the Archivist's
        # LLM, distinguish by the system prompt of message[0].
        first_sys = (messages[0].get("content") or "")[:30] if messages else ""
        if self.summary_provider and "Archivist" in first_sys:
            return {
                "content": "earlier work summarised",
                "tool_calls": [],
                "usage": {},
            }
        # Drive the agent: complete on turn 1 to keep the test short.
        return {
            "content": "done",
            "tool_calls": [_tc(f"c{self._n}", "complete", report="ok")],
        }


async def test_pre_call_compaction_triggers_when_over_threshold(tmp_path: Path) -> None:
    """If self.messages already exceeds the threshold, agent.run() should
    invoke the Archivist BEFORE the LLM call."""
    Agent.REGISTRY.clear()
    Agent.SCRATCH_ROOT = str(tmp_path)
    Agent.WORKSPACE_ROOT = str(tmp_path)
    worker = Agent(name="W", role_type="worker", system_prompt="ssssss" * 100)

    # Stuff its messages with bloat BEFORE run() starts.
    for i in range(40):
        worker.messages.append({"role": "assistant", "content": "x" * 1000})
        worker.messages.append(
            {"role": "tool", "tool_call_id": f"t{i}", "content": "y" * 1000}
        )

    # Force the threshold low so we definitely trip it.
    original_ctx = CONFIG.ctx_window_tokens
    original_pct = CONFIG.compact_at_pct
    object.__setattr__(CONFIG, "ctx_window_tokens", 4000)
    object.__setattr__(CONFIG, "compact_at_pct", 0.5)

    fake = _CountingLLM(summary_provider=True)
    original_llm = agent_module.LLM
    archivist_original = archivist_module.LLM
    agent_module.LLM = fake
    archivist_module.LLM = fake
    try:
        before_count = len(worker.messages)
        result = await worker.run("go")
        after_count = len(worker.messages)
        assert result.kind == "done", result
        # The agent's history got compacted -- ending up SMALLER than
        # before, despite us appending more turns during run().
        assert after_count < before_count, (
            f"expected compaction to shrink {before_count} messages, got {after_count}"
        )
    finally:
        agent_module.LLM = original_llm
        archivist_module.LLM = archivist_original
        object.__setattr__(CONFIG, "ctx_window_tokens", original_ctx)
        object.__setattr__(CONFIG, "compact_at_pct", original_pct)
    print("[pre_call_compaction_triggers_when_over_threshold] OK")


async def test_context_error_triggers_compact_and_retry(tmp_path: Path) -> None:
    """The first LLM call raises a context-length error; the agent must
    force a compaction and retry once. We track retries via a counter."""
    Agent.REGISTRY.clear()
    Agent.SCRATCH_ROOT = str(tmp_path)
    Agent.WORKSPACE_ROOT = str(tmp_path)
    worker = Agent(name="W", role_type="worker", system_prompt="...")
    # Pre-load enough messages that the Archivist will have something to
    # compact (compact_messages no-ops when middle < 2).
    for i in range(15):
        worker.messages.append({"role": "assistant", "content": f"msg {i}"})
        worker.messages.append(
            {"role": "tool", "tool_call_id": f"t{i}", "content": f"r {i}"}
        )

    raised: list[bool] = [False]

    class _ContextErrorLLM:
        _n = 0

        async def model_name(self):
            return "ctx-err"

        async def chat(self, messages, tools=None, **_kwargs):
            self._n += 1
            # The Archivist's LLM call has system="You are an Archivist..."
            first_sys = (messages[0].get("content") or "")[:30] if messages else ""
            if "Archivist" in first_sys:
                return {"content": "summary text", "tool_calls": [], "usage": {}}
            # First worker call: raise context-length error.
            if not raised[0]:
                raised[0] = True
                raise RuntimeError("context_length_exceeded: 200000 > 128000")
            # Retry succeeds.
            return {
                "content": "done",
                "tool_calls": [_tc(f"c{self._n}", "complete", report="ok")],
            }

    fake = _ContextErrorLLM()
    original_llm = agent_module.LLM
    archivist_original = archivist_module.LLM
    agent_module.LLM = fake
    archivist_module.LLM = fake
    try:
        result = await worker.run("go")
    finally:
        agent_module.LLM = original_llm
        archivist_module.LLM = archivist_original

    assert raised[0] is True, "expected first LLM call to have raised"
    assert result.kind == "done", result
    # Archivist event was emitted (we can't easily intercept BUS here,
    # but a successful retry implies the compact happened).
    print("[context_error_triggers_compact_and_retry] OK")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def _async_main() -> int:
    await test_archivist_compacts_middle_messages()
    await test_archivist_falls_back_on_llm_failure()
    await test_archivist_noop_for_short_history()
    with tempfile.TemporaryDirectory() as d:
        await test_large_tool_result_dumped_to_scratch(Path(d))
    with tempfile.TemporaryDirectory() as d:
        await test_pre_call_compaction_triggers_when_over_threshold(Path(d))
    with tempfile.TemporaryDirectory() as d:
        await test_context_error_triggers_compact_and_retry(Path(d))
    return 0


def main() -> int:
    test_count_tokens_scales_with_content()
    test_count_tokens_includes_tool_call_args()
    test_should_compact_threshold()
    with tempfile.TemporaryDirectory() as d:
        test_dump_below_threshold_is_noop(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_dump_above_threshold_writes_file(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_dump_sanitises_filename(Path(d))
    test_is_context_length_error_pattern_match()
    asyncio.run(_async_main())
    print("\nAll PR D regression tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
