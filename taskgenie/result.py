from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

RunKind = Literal["done", "blocked", "exhausted", "error"]


@dataclass
class RunResult:
    kind: RunKind
    report: str = ""
    reason: str = ""
    needs: str = ""
    partial_progress: str = ""
    error: str = ""

    def to_tool_payload(self) -> dict[str, Any]:
        """JSON-friendly form sent back to a parent agent via await_results."""
        if self.kind == "done":
            return {"status": "done", "report": self.report}
        if self.kind == "blocked":
            return {
                "status": "blocked",
                "reason": self.reason,
                "needs": self.needs,
                "partial_progress": self.partial_progress,
            }
        if self.kind == "exhausted":
            return {"status": "exhausted", "error": self.error or "turn limit reached"}
        return {"status": "error", "error": self.error}

    def summary(self) -> str:
        if self.kind == "done":
            return self.report
        if self.kind == "blocked":
            return f"[BLOCKED] {self.reason} | needs: {self.needs}"
        return f"[{self.kind.upper()}] {self.error}"
