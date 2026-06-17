"""Archivist: compact an agent's message history when it's about to blow
the context window.

Design choice: NOT a full Agent and NOT a bus event source. Pure async
function that takes a message list and returns a compacted one. The
calling Agent is responsible for emitting `compaction_start` /
`compaction_end` events around the call so the TUI can show the
compaction on the TARGET agent's activity log (rather than as a spurious
depth-99 tree node).

Lifecycle:

  1. Preserve verbatim: system prompt, original user task, the last
     `keep_recent` messages (snapped FORWARD to avoid starting the
     recent band with a `tool` message whose matching assistant turn
     would land in the about-to-be-summarised middle).
  2. Hand the middle band to the LLM and ask for a dense paragraph
     summary.
  3. Splice the summary in between the original task and the recent
     band as a single `[archived summary]` user message.

If the LLM call fails or returns nothing useful, we return the original
messages unchanged. Caller is responsible for not retrying forever
(see `Agent._maybe_compact`'s `_compaction_floor` tracking).
"""

from __future__ import annotations

from typing import Any

from taskgenie.config import CONFIG
from taskgenie.llm import LLM

ARCHIVIST_SYSTEM_PROMPT = (
    "You are an Archivist. Your job: take an agent's message history and "
    "compress it into a single dense paragraph (200 words MAX) that "
    "preserves what matters and drops what doesn't.\n\n"
    "PRESERVE:\n"
    "  - The task being worked on and current progress.\n"
    "  - Key decisions made and WHY.\n"
    "  - Files created, modified, or read (mention paths).\n"
    "  - Blockers encountered and how they were resolved.\n"
    "  - Results of any code execution that matter for future decisions, "
    "INCLUDING which exec tool ran it (exec_code = scratch / private, "
    "exec_workspace = shared deliverables) -- the CWD changes what the "
    "result means for next steps.\n\n"
    "DROP:\n"
    "  - Verbatim narrations.\n"
    "  - Tool-call argument JSON.\n"
    "  - Long file contents (just note the path was read).\n"
    "  - Redundant exploration.\n\n"
    "Reply with ONLY the summary paragraph. No preamble. No JSON. No quotes."
)


class CompactionSkipped(Exception):
    """Raised by `compact_messages` when the history cannot be safely
    compacted (e.g. degenerate alternation that would slice past the
    original task, or a non-user message at the task index). The caller
    distinguishes this from transient LLM failures so the repeat-compact
    floor is only advanced for GENUINE no-ops, not for retryable blips.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class CompactionTransientFailure(Exception):
    """Raised by `compact_messages` when the Archivist's LLM call failed
    in a way that COULD succeed on retry (network, rate limit, empty
    content). Caller does NOT advance the floor for these so the next
    threshold cross retries.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _safe_recent_keep(messages: list[dict[str, Any]], keep_recent: int) -> int:
    """Snap `keep_recent` forward so messages[-keep_recent:] never STARTS
    with a `tool` message.

    Why: a `tool` reply only makes sense after the `assistant` turn whose
    `tool_calls` declared the matching `tool_call_id`. If the recent band
    begins with `tool` and the matching `assistant` is in the middle band
    (about to be summarised away), the next API call 400s on a dangling
    tool_call_id. Sliding forward (= keeping MORE messages) pulls the
    matching assistant into the recent band.
    """
    n = len(messages)
    if keep_recent <= 0 or keep_recent >= n:
        return keep_recent
    while keep_recent < n and messages[n - keep_recent].get("role") == "tool":
        keep_recent += 1
    return keep_recent


def _format_messages_for_summary(messages: list[dict[str, Any]]) -> str:
    """Render the middle band into a readable transcript for the Archivist."""
    lines: list[str] = []
    for i, msg in enumerate(messages):
        role = msg.get("role", "?")
        content = msg.get("content", "") or ""
        if isinstance(content, list):
            content = " ".join(
                str(p.get("text", p)) if isinstance(p, dict) else str(p)
                for p in content
            )
        # Cap each individual message excerpt so an outsized tool result
        # can't dominate the summary input. The original full text is
        # already on disk via dump_large_tool_result by the time we get
        # here, so this is a belt-and-braces shorter cap.
        excerpt = str(content)[:2000]
        tool_calls = msg.get("tool_calls", []) or []
        if tool_calls:
            names = ", ".join(
                tc.get("function", {}).get("name", "?") for tc in tool_calls
            )
            excerpt = f"{excerpt} [called: {names}]"
        lines.append(f"[{i}] {role}: {excerpt}")
    return "\n".join(lines)


async def compact_messages(
    target_name: str,
    messages: list[dict[str, Any]],
    keep_recent: int | None = None,
) -> list[dict[str, Any]]:
    """Return a compacted copy of `messages`.

    Raises `CompactionSkipped` when the history can't be safely compacted
    (degenerate alternation, non-user original task, not enough middle).
    Raises `CompactionTransientFailure` when the Archivist's own LLM call
    failed in a way that could succeed on retry. Caller distinguishes
    these so the repeat-compact floor only advances for genuine skips.

    Does NOT publish bus events; the caller (Agent) emits
    `compaction_start` / `compaction_end` around the await so the TUI
    can route activity to the right target agent's display.
    """
    if keep_recent is None:
        keep_recent = CONFIG.compact_keep_recent
    # Snap keep_recent forward if the slice would dangle a tool_call_id.
    adjusted_keep = _safe_recent_keep(messages, keep_recent)
    if len(messages) <= adjusted_keep + 2:
        # Snap consumed everything (or nearly everything). This is the
        # degenerate alternating-history case: the recent band swallowed
        # the would-be middle. Not a transient failure -- the history
        # structure itself prevents compaction.
        raise CompactionSkipped(
            f"recent band ({adjusted_keep} messages) too large vs total "
            f"({len(messages)}) to leave a meaningful middle"
        )

    system_msg = messages[0]
    # Find the first non-system message; that's the original task.
    first_real_idx = 1
    while (
        first_real_idx < len(messages)
        and messages[first_real_idx].get("role") == "system"
    ):
        first_real_idx += 1
    if first_real_idx >= len(messages):
        raise CompactionSkipped("no non-system messages in history")
    original_task = messages[first_real_idx]
    # The compacted output puts the original task at position 1. If that
    # message is an assistant turn WITH tool_calls, the matching tool
    # replies (now in the summarised middle) become dangling refs and
    # the next API call 400s. Bail in that specific case. Plain
    # assistant messages without tool_calls are fine (tests pre-populate
    # this shape; production always starts with role="user").
    if original_task.get("role") == "assistant" and (
        original_task.get("tool_calls") or []
    ):
        raise CompactionSkipped(
            f"original task at index {first_real_idx} is an assistant "
            "turn with tool_calls; compacting would orphan tool_call_ids"
        )

    middle = messages[first_real_idx + 1 : -adjusted_keep]
    recent = messages[-adjusted_keep:]
    if len(middle) < 2:
        raise CompactionSkipped(
            f"middle band only {len(middle)} message(s) -- not worth a round-trip"
        )

    try:
        # bypass_semaphore: archivist calls go through a dedicated
        # semaphore inside LLM.chat (not the agent semaphore), so concurrent
        # archivist fanout can't DOS the local model. Past comments in
        # this module claimed the caller "already holds" the agent
        # semaphore -- that was wrong; _maybe_compact runs BEFORE the
        # agent's own LLM.chat.
        response = await LLM.chat(
            messages=[
                {"role": "system", "content": ARCHIVIST_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"Compress this conversation history (messages "
                        f"{first_real_idx + 1}-{len(messages) - adjusted_keep - 1} "
                        f"of {len(messages)} for agent {target_name!r}). "
                        f"Reply with one dense paragraph."
                        f"\n\n--- TRANSCRIPT ---\n"
                        + _format_messages_for_summary(middle)
                    ),
                },
            ],
            tools=None,
            bypass_semaphore=True,
        )
    except Exception as e:
        raise CompactionTransientFailure(
            f"archivist LLM call raised: {type(e).__name__}: {e}"
        ) from e

    summary = (response.get("content") or "").strip()
    if not summary:
        raise CompactionTransientFailure("archivist LLM returned empty content")

    summary_msg = {
        "role": "user",
        "content": (f"[archived summary of {len(middle)} earlier messages]: {summary}"),
    }
    return [system_msg, original_task, summary_msg, *recent]
