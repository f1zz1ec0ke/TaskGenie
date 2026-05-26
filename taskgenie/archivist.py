"""Archivist: compact an agent's message history when it's about to blow
the context window.

Design choice: NOT a full Agent. Calls LLM directly with a focused
summarization prompt. Emits spawn / state / complete events on the bus
so the TUI surfaces compaction visibly even though there's no Agent
object in the REGISTRY.

The "background agent" framing the user asked for is honoured at the
event-bus level (you'll see "Archivist[WorkerName]" appear in the
hierarchy / activity log when it fires) while keeping the implementation
a single async function with deterministic behaviour:

  1. Preserve verbatim: system prompt, original user task, the last
     `keep_recent` messages.
  2. Hand the middle band to the LLM and ask for a dense paragraph
     summary.
  3. Splice the summary in between the original task and the recent
     band as a single `[archived summary]` user message.

If the LLM call fails or returns nothing useful, we return the original
messages unchanged. The caller is responsible for not retrying forever
(see `Agent._maybe_compact`'s `_compacted_at` tracking).
"""
from __future__ import annotations

import secrets
from typing import Any

from taskgenie.config import CONFIG
from taskgenie.events import BUS, Event
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
    "  - Results of any code execution that matter for future decisions.\n\n"
    "DROP:\n"
    "  - Verbatim narrations.\n"
    "  - Tool-call argument JSON.\n"
    "  - Long file contents (just note the path was read).\n"
    "  - Redundant exploration.\n\n"
    "Reply with ONLY the summary paragraph. No preamble. No JSON. No quotes."
)


def _format_messages_for_summary(messages: list[dict[str, Any]]) -> str:
    """Render the middle band into a readable transcript for the Archivist."""
    lines: list[str] = []
    for i, msg in enumerate(messages):
        role = msg.get("role", "?")
        content = msg.get("content", "") or ""
        if isinstance(content, list):
            content = " ".join(str(p.get("text", p)) for p in content)
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
    """Return a compacted copy of `messages`. On failure, returns the
    input unchanged so the caller can decide what to do.

    Visible side-effect: emits spawn/state/complete events on BUS so the
    TUI can surface that a compaction happened.
    """
    if keep_recent is None:
        keep_recent = CONFIG.compact_keep_recent
    if len(messages) <= keep_recent + 2:
        return messages  # nothing meaningful to compact

    system_msg = messages[0]
    # Find the first non-system message; that's the original task.
    first_real_idx = 1
    while first_real_idx < len(messages) and messages[first_real_idx].get("role") == "system":
        first_real_idx += 1
    if first_real_idx >= len(messages):
        return messages
    original_task = messages[first_real_idx]

    middle = messages[first_real_idx + 1 : -keep_recent]
    recent = messages[-keep_recent:]
    if len(middle) < 2:
        return messages  # not enough fat to be worth a round-trip

    archivist_id = f"Archivist-{secrets.token_hex(3)}"
    archivist_name = f"Archivist[{target_name}]"
    BUS.publish(
        Event(
            "spawn",
            archivist_id,
            archivist_name,
            data={
                "role_type": "worker",
                "depth": 99,
                "parent_id": None,
            },
        )
    )
    BUS.publish(
        Event(
            "state",
            archivist_id,
            archivist_name,
            data={
                "status": "working",
                "task": (
                    f"Compact {len(middle)} middle messages for {target_name} "
                    f"(keeping system + task + last {keep_recent} turns)"
                ),
            },
        )
    )

    try:
        response = await LLM.chat(
            messages=[
                {"role": "system", "content": ARCHIVIST_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"Compress this conversation history (messages "
                        f"{first_real_idx + 1}-{len(messages) - keep_recent - 1} "
                        f"of {len(messages)}). Reply with one dense paragraph."
                        f"\n\n--- TRANSCRIPT ---\n"
                        + _format_messages_for_summary(middle)
                    ),
                },
            ],
            tools=None,
        )
    except Exception as e:
        BUS.publish(
            Event(
                "error",
                archivist_id,
                archivist_name,
                data={"message": f"compaction LLM call failed: {e}"},
            )
        )
        BUS.publish(
            Event(
                "state",
                archivist_id,
                archivist_name,
                data={"status": "error"},
            )
        )
        return messages

    summary = (response.get("content") or "").strip()
    if not summary:
        BUS.publish(
            Event(
                "error",
                archivist_id,
                archivist_name,
                data={"message": "compaction LLM returned empty content"},
            )
        )
        BUS.publish(
            Event(
                "state",
                archivist_id,
                archivist_name,
                data={"status": "error"},
            )
        )
        return messages

    summary_msg = {
        "role": "user",
        "content": (
            f"[archived summary of {len(middle)} earlier messages]: {summary}"
        ),
    }
    compacted = [system_msg, original_task, summary_msg, *recent]

    BUS.publish(
        Event(
            "complete",
            archivist_id,
            archivist_name,
            data={
                "report": (
                    f"compacted {len(messages)} -> {len(compacted)} messages "
                    f"({len(middle)} middle messages summarised)"
                ),
            },
        )
    )
    BUS.publish(
        Event(
            "state",
            archivist_id,
            archivist_name,
            data={"status": "done"},
        )
    )
    return compacted
