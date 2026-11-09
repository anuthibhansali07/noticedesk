"""OpenAI provider (secondary LLM)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Any

from app.services.llm.base import (
    LLMError,
    LLMProvider,
    LLMRateLimitError,
    LLMResponse,
    LLMTransientError,
    is_rate_limit_exception,
    retry_after_from_exception,
    retry_on_rate_limit,
)

logger = logging.getLogger(__name__)


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gpt-4o",
        client: Any | None = None,
        timeout_seconds: float = 180.0,
        # When set, points the SDK at any OpenAI-compatible endpoint —
        # Groq, Cerebras, Together, Fireworks, OpenRouter, Ollama, etc.
        # Leave None to talk to api.openai.com.
        base_url: str | None = None,
        # Backoff schedule (seconds) for HTTP 429 / RESOURCE_EXHAUSTED before
        # giving up on a model. (2, 5) → up to three attempts per model.
        rate_limit_retry_delays: Sequence[float] = (2.0, 5.0),
        # Models tried in order when ``model`` stays rate-limited after its
        # retries — e.g. another Gemini free-tier model with its own quota.
        fallback_models: Sequence[str] = (),
    ) -> None:
        self._api_key = api_key
        self.model = model
        self._client = client
        self._timeout = timeout_seconds
        self._base_url = base_url
        self._retry_delays = tuple(rate_limit_retry_delays)
        self._fallback_models = tuple(m for m in fallback_models if m and m != model)

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from openai import AsyncOpenAI  # type: ignore[import-not-found]
        except ImportError as e:
            raise LLMError(
                "openai SDK not installed; install it or pick a different provider"
            ) from e
        kwargs: dict[str, Any] = {"api_key": self._api_key}
        if self._base_url:
            kwargs["base_url"] = self._base_url
        self._client = AsyncOpenAI(**kwargs)
        return self._client

    async def generate_text(
        self,
        *,
        system: str,
        user: str,
        max_output_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> LLMResponse:
        client = self._ensure_client()
        last_rate_limit: LLMRateLimitError | None = None
        for model in (self.model, *self._fallback_models):
            try:
                return await retry_on_rate_limit(
                    lambda model=model: self._call_once(
                        client,
                        model=model,
                        system=system,
                        user=user,
                        max_output_tokens=max_output_tokens,
                        temperature=temperature,
                    ),
                    delays=self._retry_delays,
                    label=f"openai:{model}",
                )
            except LLMRateLimitError as e:
                logger.warning("openai model %s exhausted rate-limit retries: %s", model, e)
                last_rate_limit = e
                continue
        assert last_rate_limit is not None
        raise last_rate_limit

    async def _call_once(
        self,
        client: Any,
        *,
        model: str,
        system: str,
        user: str,
        max_output_tokens: int,
        temperature: float,
    ) -> LLMResponse:
        async def _call() -> LLMResponse:
            try:
                resp = await client.chat.completions.create(
                    model=model,
                    temperature=temperature,
                    max_tokens=max_output_tokens,
                    response_format={"type": "json_object"},
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                )
            except Exception as e:  # noqa: BLE001
                if is_rate_limit_exception(e):
                    raise LLMRateLimitError(
                        f"openai rate limited ({model}): {e}",
                        retry_after_seconds=retry_after_from_exception(e),
                    ) from e
                raise LLMTransientError(f"openai call failed: {e}") from e

            content = resp.choices[0].message.content or ""
            usage = getattr(resp, "usage", None)
            return LLMResponse(
                content=content,
                model=model,
                provider_name=self.name,
                input_tokens=getattr(usage, "prompt_tokens", None) if usage else None,
                output_tokens=getattr(usage, "completion_tokens", None) if usage else None,
                raw={"id": getattr(resp, "id", None)},
            )

        try:
            return await asyncio.wait_for(_call(), timeout=self._timeout)
        except TimeoutError as e:
            raise LLMTransientError(f"openai timed out after {self._timeout}s") from e
