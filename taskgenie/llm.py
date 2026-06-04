from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    InternalServerError,
    RateLimitError,
)

from taskgenie.config import CONFIG

logger = logging.getLogger(__name__)

_TRANSIENT = (APITimeoutError, APIConnectionError, InternalServerError, RateLimitError)


class LMStudioClient:
    """Async wrapper around an OpenAI-compatible endpoint with retry + a
    concurrency semaphore so a manager fanning out N children can't hammer
    the model server."""

    def __init__(self) -> None:
        self._client = AsyncOpenAI(base_url=CONFIG.base_url, api_key=CONFIG.api_key)
        self._model: str | None = CONFIG.model or None
        self._sem = asyncio.Semaphore(CONFIG.max_concurrent_llm)

    async def model_name(self) -> str:
        if self._model:
            return self._model
        models = await self._client.models.list()
        if not models.data:
            raise RuntimeError(f"No models loaded at {CONFIG.base_url}")
        self._model = models.data[0].id
        logger.info(f"Auto-detected model: {self._model}")
        return self._model

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        bypass_semaphore: bool = False,
        model_override: str | None = None,
    ) -> dict[str, Any]:
        """Send a chat-completion request with retry-on-transient errors.

        `bypass_semaphore=True` lets system calls (the Archivist) skip the
        concurrency cap that exists to throttle agent fan-out. Without
        this, two workers compacting at once would consume the entire
        `max_concurrent_llm` budget and starve any real agent calls.

        `model_override` selects a specific model for this call. When None
        (the default), the client's configured or auto-detected model is
        used. This keeps the change backward-compatible for callers that
        don't pass it (e.g. the Archivist).
        """
        model = model_override or await self.model_name()
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": CONFIG.temperature,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        logger.debug(f"LLM call: {len(messages)} messages, {len(tools or [])} tools")

        last_exc: Exception | None = None
        start = time.monotonic()
        for attempt in range(4):  # initial + 3 retries
            try:
                if bypass_semaphore:
                    resp = await self._client.chat.completions.create(**kwargs)
                else:
                    async with self._sem:
                        resp = await self._client.chat.completions.create(**kwargs)
                break
            except _TRANSIENT as e:
                last_exc = e
                logger.warning(f"LLM retry {attempt+1}/3: {type(e).__name__}: {e}")
                if attempt == 3:
                    raise
                sleep = (2 ** attempt) + random.uniform(0, 0.5)
                await asyncio.sleep(sleep)
        else:  # never executed (loop breaks on success or raises on last attempt)
            raise last_exc  # type: ignore[misc]

        duration_ms = (time.monotonic() - start) * 1000.0
        choice = resp.choices[0]
        msg = choice.message
        usage = getattr(resp, "usage", None)
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        return {
            "content": msg.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                }
                for tc in (msg.tool_calls or [])
            ],
            "finish_reason": choice.finish_reason,
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            },
            "duration_ms": duration_ms,
        }


LLM = LMStudioClient()
