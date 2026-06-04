"""Story Mode narrator: sarcastic running commentary on the agent run.

A background asyncio task that periodically feeds recent events to the
LLM and gets back 2-3 sentences of dry, sarcastic narration. The TUI
reads `.entries` for rendering on the Story Mode screen.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from taskgenie.events import BUS, Event
from taskgenie.llm import LLM

NARRATOR_SYSTEM_PROMPT = (
    "You are a sarcastic narrator watching a team of AI agents attempt to "
    "complete a task. Think Douglas Adams meets a bored IT department "
    "observer who's seen it all.\n\n"
    "Rules:\n"
    "- Exactly 2-3 sentences per update.\n"
    "- Dry, witty. Occasionally throw shade at the bureaucracy.\n"
    "- Never mean-spirited -- amused, not hostile.\n"
    "- Reference specific agents by name and what they're doing.\n"
    "- No emojis. No hashtags. Plain text.\n"
    "- If things go well, grudgingly acknowledge it.\n"
    "- If things go badly, savor it.\n"
    "- You're narrating a nature documentary about middle management, "
    "except everyone is an AI and nobody has a body.\n"
    "Reply with ONLY your narration. No preamble."
)

NARRATE_INTERVAL = 20.0
MIN_NEW_EVENTS = 5
MAX_EVENTS_IN_PROMPT = 20


@dataclass
class NarrationEntry:
    text: str
    ts: float = field(default_factory=time.time)


def _format_events(events: list[Event]) -> str:
    lines: list[str] = []
    for e in events:
        d = e.data
        if e.kind == "spawn":
            detail = f"appeared (role: {d.get('role_type', '?')})"
        elif e.kind == "hire":
            detail = f"hired {d.get('child_name', '?')} as {d.get('role_type', '?')}"
        elif e.kind == "assign":
            task = d.get("task", "")[:100]
            detail = f"assigned: {task}"
        elif e.kind == "narrate":
            text = d.get("text", "")[:100]
            detail = f'thinking: "{text}"'
        elif e.kind == "tool_call":
            detail = f"called {d.get('tool', '?')}"
        elif e.kind == "complete":
            report = d.get("report", "")[:80]
            detail = f"finished: {report}"
        elif e.kind == "blocked":
            detail = f"stuck: {d.get('reason', '?')}"
        elif e.kind == "error":
            detail = f"error: {d.get('message', '?')}"
        elif e.kind == "fire":
            detail = f"fired {d.get('agent_name', '?')}"
        elif e.kind == "review":
            verdict = "approved" if d.get("accepted") else "rejected"
            detail = f"{verdict} work"
        elif e.kind == "rehire":
            detail = "got a second chance"
        else:
            detail = e.kind
        lines.append(f"- {e.agent_name}: {detail}")
    return "\n".join(lines)


class StoryNarrator:
    def __init__(self) -> None:
        self.entries: list[NarrationEntry] = []
        self._buf: list[Event] = []
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="story-narrator")

    async def stop(self) -> None:
        self._stop.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _loop(self) -> None:
        queue = BUS.subscribe()
        last_narration = 0.0
        try:
            while not self._stop.is_set():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=1.0)
                    self._buf.append(event)
                    if len(self._buf) > 50:
                        self._buf = self._buf[-50:]
                except asyncio.TimeoutError:
                    pass

                now = time.time()
                new_count = sum(1 for e in self._buf if e.ts > last_narration)
                if (
                    now - last_narration >= NARRATE_INTERVAL
                    and new_count >= MIN_NEW_EVENTS
                    and len(self._buf) >= 3
                ):
                    try:
                        text = await self._generate(self._buf[-MAX_EVENTS_IN_PROMPT:])
                        if text:
                            self.entries.append(NarrationEntry(text=text))
                            if len(self.entries) > 50:
                                self.entries = self.entries[-50:]
                    except Exception:
                        pass
                    last_narration = time.time()
        except asyncio.CancelledError:
            pass
        finally:
            BUS.unsubscribe(queue)

    async def _generate(self, events: list[Event]) -> str:
        prev = ""
        if self.entries:
            prev = (
                f"\n\nYour previous narration: \"{self.entries[-1].text}\"\n"
                "Don't repeat yourself. Build on the story."
            )
        response = await LLM.chat(
            messages=[
                {"role": "system", "content": NARRATOR_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"Here's what the agents have been up to:\n\n"
                        f"{_format_events(events)}{prev}\n\n"
                        f"Narrate this."
                    ),
                },
            ],
            tools=None,
            bypass_semaphore=True,
        )
        return (response.get("content") or "").strip()
