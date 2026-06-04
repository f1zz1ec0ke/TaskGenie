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
# Note: all kinds the agent emits via `_emit` should be in this Literal.
# If you add a new emit call site, add the kind here too -- otherwise
# the type checker requires `# type: ignore[arg-type]` at the call site.


@dataclass
class Event:
    kind: EventKind
    agent_id: str
    agent_name: str
    ts: float = field(default_factory=time.time)
    data: dict[str, Any] = field(default_factory=dict)


DEFAULT_QUEUE_MAXSIZE: int = 10_000


class _BoundedSubscription:
    """Internal wrapper pairing a bounded queue with a drop counter."""

    __slots__ = ("queue", "dropped")

    def __init__(self, maxsize: int) -> None:
        self.queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self.dropped: int = 0


class EventBus:
    def __init__(self) -> None:
        self._subscribers: list[_BoundedSubscription] = []

    # -- public API ----------------------------------------------------------

    def subscribe(
        self, maxsize: int = DEFAULT_QUEUE_MAXSIZE
    ) -> asyncio.Queue[Event]:
        """Create a bounded subscription and return its queue.

        *maxsize* controls the upper bound on queued events (default
        :data:`DEFAULT_QUEUE_MAXSIZE`).  When a slow consumer lets the
        queue fill up, the oldest event is silently dropped so that
        publishers are never blocked.
        """
        sub = _BoundedSubscription(maxsize)
        self._subscribers.append(sub)
        return sub.queue

    def unsubscribe(self, q: asyncio.Queue[Event]) -> None:
        for i, sub in enumerate(self._subscribers):
            if sub.queue is q:
                del self._subscribers[i]
                return

    def publish(self, event: Event) -> None:
        for sub in self._subscribers:
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                # Drop the oldest event to make room.
                try:
                    sub.queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass  # theoretically impossible, but be safe
                sub.dropped += 1
                try:
                    sub.queue.put_nowait(event)
                except asyncio.QueueFull:
                    # Should not happen after a successful get_nowait,
                    # but guard against unlikely races.
                    sub.dropped += 1

    # -- diagnostics ---------------------------------------------------------

    @property
    def subscriber_count(self) -> int:
        """Number of active subscribers."""
        return len(self._subscribers)

    def dropped_count(self, q: asyncio.Queue[Event]) -> int:
        """Return the number of events dropped for *q* due to overflow.

        Returns ``0`` if *q* is not (or no longer) a subscriber.
        """
        for sub in self._subscribers:
            if sub.queue is q:
                return sub.dropped
        return 0


BUS = EventBus()
