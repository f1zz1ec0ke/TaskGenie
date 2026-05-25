"""Append every bus event to a session's events.jsonl as an audit log.

Runs as a background asyncio task; one JSON object per line. Used both for
post-hoc debugging and as the source for the deterministic stats panel
shown after a run completes.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from pathlib import Path

from taskgenie.events import BUS, Event


class JSONLEventLogger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._task: asyncio.Task[None] | None = None
        self._queue: asyncio.Queue[Event] | None = None
        self._stop: asyncio.Event = asyncio.Event()

    def start(self) -> None:
        if self._task is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._queue = BUS.subscribe()
        self._task = asyncio.create_task(self._run(), name="event-logger")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout=2.0)
        except asyncio.TimeoutError:
            self._task.cancel()
        if self._queue is not None:
            BUS.unsubscribe(self._queue)
        self._task = None
        self._queue = None

    async def _run(self) -> None:
        assert self._queue is not None
        # Open in append mode so resumes extend rather than replace.
        with self.path.open("a", encoding="utf-8", newline="\n") as f:
            while not (self._stop.is_set() and self._queue.empty()):
                try:
                    event = await asyncio.wait_for(self._queue.get(), timeout=0.25)
                except asyncio.TimeoutError:
                    continue
                f.write(json.dumps(asdict(event), default=str) + "\n")
                f.flush()


def read_events(path: Path) -> list[dict]:
    """Read every event from a session log. Tolerates partial last lines."""
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
