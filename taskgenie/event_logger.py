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
from typing import Any

from taskgenie.events import BUS, Event

# Cap any individual string field in an event's `data` payload to keep the
# log line size bounded. Large tool outputs are already dump-routed to
# scratch by `dump_large_tool_result`, but `tool_call.args` (the original
# arguments -- a DICT) and very long narrations would otherwise blow the
# file size up to MBs per session, especially now that max_turns_per_agent
# defaults to 1000. Round-three confirmed the previous shallow truncator
# missed the largest realistic offender (write_workspace's content arg
# nested inside the args dict).
_MAX_EVENT_FIELD_CHARS = 16 * 1024


def _truncate_for_log(data: dict[str, Any]) -> dict[str, Any]:
    """Recursively cap every string leaf in `data`. Walks dicts and lists
    so nested content (notably `tool_call.args.content`) is truncated."""

    def trunc(v: Any) -> Any:
        if isinstance(v, str):
            if len(v) > _MAX_EVENT_FIELD_CHARS:
                return (
                    v[:_MAX_EVENT_FIELD_CHARS]
                    + f"…[truncated for log; original {len(v)} chars]"
                )
            return v
        if isinstance(v, dict):
            return {k: trunc(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [trunc(x) for x in v]
        if isinstance(v, (bytes, bytearray)):
            if len(v) > _MAX_EVENT_FIELD_CHARS:
                return (
                    repr(bytes(v[:_MAX_EVENT_FIELD_CHARS]))
                    + f"…[truncated for log; original {len(v)} bytes]"
                )
            return repr(bytes(v))
        return v

    return {k: trunc(v) for k, v in data.items()}


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

    async def drain(self, timeout: float = 1.0) -> None:
        """Wait for any queued events to be flushed to disk.

        Uses `Queue.join()` (paired with `task_done()` in the writer) so we
        actually block until every published-but-not-yet-flushed event has
        been processed. The old poll-loop on `queue.empty()` was racy:
        the writer could be mid-`f.write`/`f.flush` when we sampled empty
        and decided we were done.
        """
        if self._queue is None:
            return
        try:
            await asyncio.wait_for(self._queue.join(), timeout=timeout)
        except asyncio.TimeoutError:
            pass

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
                try:
                    row = asdict(event)
                    if isinstance(row.get("data"), dict):
                        row["data"] = _truncate_for_log(row["data"])
                    f.write(json.dumps(row, default=str) + "\n")
                    f.flush()
                finally:
                    # Always mark done so `drain()` via Queue.join() can
                    # progress even if serialisation raised. Otherwise a
                    # single un-serialisable event would deadlock drain
                    # for the rest of the session.
                    self._queue.task_done()


def read_events(path: Path) -> list[dict]:
    """Read every event from a session log. Tolerates partial last lines.

    Streams line-by-line rather than slurping the whole file into memory
    so a long-running session (potentially hundreds of MBs of events.jsonl
    when `max_turns_per_agent=1000`) doesn't OOM stats.compute.
    """
    if not path.exists():
        return []
    out: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out
