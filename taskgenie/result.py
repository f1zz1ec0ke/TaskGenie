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
            payload = {"status": "exhausted", "error": self.error or "turn limit reached"}
        else:
            payload = {"status": "error", "error": self.error}
        if self.follow_ups:
            payload["follow_ups"] = self.follow_ups
        return payload

    def summary(self) -> str:
        if self.kind == "done":
            return self.report
        if self.kind == "blocked":
            return f"[BLOCKED] {self.reason} | needs: {self.needs}"
        if self.kind == "question":
            return f"[QUESTION] {self.question}"
        return f"[{self.kind.upper()}] {self.error}"
