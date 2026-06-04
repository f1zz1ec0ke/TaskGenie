"""Context window management: token counting + tool-output dumping.

This module is the seam where TaskGenie keeps long-running agents from
blowing through their model's context window. Two complementary tools:

1. `count_tokens(messages)` -- rough token estimate (4-char heuristic).
   Cheap, no dependencies; can be swapped for a real tokenizer later
   without changing call sites. Used by `agent.run()` to decide when to
   compact before an LLM call.

2. `dump_large_tool_result(...)` -- when a tool reply is bigger than
   `CONFIG.dump_tool_output_above_bytes`, write the full content to a
   file in the agent's scratch dir and return a one-line pointer string
   for the in-memory message. Generic across all tools (built-in + MCP)
   so a single chokepoint covers everything. Agents who need the full
   text later can `read_file` the pointer.

A 3-char-per-token heuristic optimises for code a bit (tokens average
~3.5-4 chars in English, less for code), which is fine for a safety
budget. The compaction threshold is set conservatively (80% of the
window by default) so the slack absorbs heuristic error.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

CHARS_PER_TOKEN = 3  # heuristic; tune as needed


def count_tokens(messages: list[dict[str, Any]]) -> int:
    """Estimate the token count of a chat messages list.

    Covers `content` (string OR list-of-parts), `tool_calls` with their
    `function.name` + `function.arguments`, and the tiny constants for
    role markers. The result is intentionally a SAFE upper bound -- we'd
    rather compact a turn early than discover the limit by 4xx.
    """
    total_chars = 0
    for msg in messages:
        # Tiny constant for the role + bookkeeping (e.g. tool_call_id).
        total_chars += 20

        content = msg.get("content", "")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            # Some providers nest content as a list of {type, text} parts.
            for part in content:
                if isinstance(part, dict):
                    total_chars += len(str(part.get("text", "")))
                else:
                    total_chars += len(str(part))

        for tc in msg.get("tool_calls", []) or []:
            fn = tc.get("function", {})
            total_chars += len(str(fn.get("name", "")))
            total_chars += len(str(fn.get("arguments", "")))

    # Round up so we never under-count.
    return (total_chars + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def should_compact(messages: list[dict[str, Any]], ctx_window: int, pct: float) -> bool:
    """True if the messages exceed `pct` (0..1) of the model's context window."""
    if ctx_window <= 0 or pct <= 0:
        return False
    return count_tokens(messages) >= int(ctx_window * pct)


_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")


def dump_large_tool_result(
    result_str: str,
    scratch_dir: str,
    turn_idx: int,
    call_id: str,
    tool_name: str,
    threshold_bytes: int,
    chunk_margin: int = 4096,
) -> tuple[str, list[str] | None]:
    if not result_str or len(result_str) <= threshold_bytes:
        return result_str, None

    Path(scratch_dir).mkdir(parents=True, exist_ok=True)
    safe_call = _SAFE_NAME_RE.sub("", call_id)[:24] or "x"
    safe_tool = _SAFE_NAME_RE.sub("", tool_name)[:24] or "tool"
    call_hash = hashlib.sha1(call_id.encode("utf-8")).hexdigest()[:8]
    base = f"tool-t{turn_idx}-{safe_tool}-{safe_call}-{call_hash}"

    chunk_size = max(1, threshold_bytes - chunk_margin)
    chunks = [result_str[i:i + chunk_size]
              for i in range(0, len(result_str), chunk_size)]
    n = len(chunks)
    width = len(str(n))

    paths: list[str] = []
    for idx, chunk in enumerate(chunks, start=1):
        filename = f"{base}-{idx:0{width}d}.log"
        target = Path(scratch_dir) / filename
        target.write_text(chunk, encoding="utf-8")
        paths.append(str(target))

    total_kb = max(1, len(result_str) // 1024)
    part_names = [f"{base}-{i:0{width}d}.log" for i in range(1, n + 1)]
    if n <= 12:
        how = "the parts in order: " + ", ".join(repr(p) for p in part_names)
    else:
        how = (f"{n} parts, {part_names[0]!r} through {part_names[-1]!r} "
               f"(part k is '{base}-<k>.log', zero-padded to {width} digits)")
    pointer = (
        f"[tool output dumped: {total_kb}KB from {tool_name!r}, too large to "
        f"inline; split into {how}. Call `read_file` on each part in order and "
        f"concatenate the results to reconstruct the full output.]"
    )
    return pointer, paths