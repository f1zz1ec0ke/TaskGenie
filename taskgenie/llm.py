from __future__ import annotations

from typing import Any

from openai import AsyncOpenAI

from taskgenie.config import CONFIG


class LMStudioClient:
    """Thin async wrapper around LM Studio's OpenAI-compatible endpoint."""

    def __init__(self) -> None:
        self._client = AsyncOpenAI(base_url=CONFIG.base_url, api_key=CONFIG.api_key)
        self._model: str | None = CONFIG.model or None

    async def model_name(self) -> str:
        if self._model:
            return self._model
        models = await self._client.models.list()
        if not models.data:
            raise RuntimeError(f"No models loaded at {CONFIG.base_url}")
        self._model = models.data[0].id
        return self._model

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        model = await self.model_name()
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": CONFIG.temperature,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        resp = await self._client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        msg = choice.message
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
        }


LLM = LMStudioClient()
