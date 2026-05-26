from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Literal

EventKind = Literal[
    "spawn",
    "narrate",
    "tool_call",
    "tool_result",
    "hire",
    "assign",
    "review",
    "fire",
    "rehire",
    "complete",
    "error",
    "state",
    "llm_call",
    "blocked",
    "question",
    "follow_up_suggested",
    "compaction_start",
    "compaction_end",
]


@dataclass
class Event:
    kind: EventKind
    agent_id: str
    agent_name: str
    ts: float = field(default_factory=time.time)
    data: dict[str, Any] = field(default_factory=dict)


class EventBus:
    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue[Event]] = []

    def subscribe(self) -> asyncio.Queue[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[Event]) -> None:
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    def publish(self, event: Event) -> None:
        for q in self._subscribers:
            q.put_nowait(event)


BUS = EventBus()
