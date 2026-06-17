from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

RunKind = Literal["done", "blocked", "question", "exhausted", "error"]


@dataclass
class RunResult:
    kind: RunKind
    report: str = ""
    reason: str = ""
    needs: str = ""
    partial_progress: str = ""
    error: str = ""
    follow_ups: list[dict[str, Any]] = field(default_factory=list)
    question: str = ""
    options: list[str] = field(default_factory=list)
    context: str = ""
    # Compaction telemetry from the agent that produced this result.
    # Snapshot of `agent.compaction_stats` at return time. Surfaced via
    # `to_tool_payload` so a parent can see whether their child had to
    # compact during the assignment -- if the count is high, the task
    # was probably too big for one agent and the parent should split it
    # next time.
    compaction_stats: dict[str, int | float] = field(default_factory=dict)

    def to_tool_payload(self) -> dict[str, Any]:
        """JSON-friendly form sent back to a parent agent via await_results."""
        if self.kind == "done":
            payload: dict[str, Any] = {"status": "done", "report": self.report}
        elif self.kind == "blocked":
            payload = {
                "status": "blocked",
                "reason": self.reason,
                "needs": self.needs,
                "partial_progress": self.partial_progress,
            }
        elif self.kind == "question":
            payload = {
                "status": "question",
                "question": self.question,
                "options": self.options,
                "context": self.context,
            }
        elif self.kind == "exhausted":
            payload = {
                "status": "exhausted",
                "error": self.error or "turn limit reached",
            }
        else:
            payload = {"status": "error", "error": self.error}
        if self.follow_ups:
            payload["follow_ups"] = self.follow_ups
        # Only surface compaction stats when the child actually did
        # something compaction-related. Most workers won't touch the
        # archivist at all; padding their payload with zeros would just
        # be noise in the parent's tool reply.
        if self.compaction_stats and any(
            self.compaction_stats.get(k, 0) for k in ("count", "skipped", "failed")
        ):
            payload["compaction"] = _compact_telemetry(self.compaction_stats)
        return payload

    def summary(self) -> str:
        if self.kind == "done":
            return self.report
        if self.kind == "blocked":
            return f"[BLOCKED] {self.reason} | needs: {self.needs}"
        if self.kind == "question":
            return f"[QUESTION] {self.question}"
        return f"[{self.kind.upper()}] {self.error}"


def _compact_telemetry(stats: dict[str, int | float]) -> dict[str, Any]:
    """Project the agent's compaction_stats into a parent-visible shape.

    Keeps the field names short and the units explicit so the parent LLM
    can parse it without ambiguity. Drops zero-valued entries to keep the
    payload tight when only one branch fired.
    """
    out: dict[str, Any] = {}
    for key in ("count", "skipped", "failed", "messages_compacted", "tokens_saved"):
        v = int(stats.get(key, 0))
        if v:
            out[key] = v
    dur = float(stats.get("duration_ms", 0.0))
    if dur:
        # Round to whole ms so the JSON stays readable.
        out["duration_ms"] = int(dur)
    return out
