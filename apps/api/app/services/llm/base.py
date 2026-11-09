"""LLM provider contract."""

from __future__ import annotations

import abc
import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar

T = TypeVar("T")

_logger = logging.getLogger(__name__)


class LLMError(Exception):
    """Permanent LLM failure (auth, model-unavailable, etc.)."""


class LLMTransientError(LLMError):
    """Temporary failure (timeout, 5xx, throttling) — safe to retry."""


class LLMRateLimitError(LLMTransientError):
    """Provider refused the call for quota / rate / capacity reasons (HTTP
    429, Gemini RESOURCE_EXHAUSTED, 503 overloaded, Anthropic 529). Carries
    the provider's suggested wait so routes can surface a clean HTTP 429 to
    the UI."""

    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class JsonSchemaValidationError(LLMError):
    """LLM returned valid JSON but it doesn't satisfy the requested schema."""


# Upper bound on how long we are willing to sleep inside a request before
# handing the rate limit back to the caller. Anything longer (daily quota
# exhaustion, multi-minute Retry-After) is better surfaced to the partner.
MAX_INLINE_RETRY_WAIT_SECONDS: float = 30.0

_RETRY_DELAY_RE = re.compile(
    r"retry[_ -]?(?:delay|after)['\"]?\s*[:=]?\s*['\"]?(\d+(?:\.\d+)?)", re.I
)
_RETRY_IN_RE = re.compile(r"retry in (\d+(?:\.\d+)?)\s*s", re.I)


def is_rate_limit_exception(exc: BaseException) -> bool:
    """True when an SDK exception represents a quota / throttling / overload
    refusal. Gemini's 503 "model is overloaded" (UNAVAILABLE) is treated the
    same as a 429: it clears in seconds and another model may be free."""
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in (429, 503, 529):
        return True
    msg = str(exc).lower()
    return (
        "error code: 429" in msg
        or "error code: 503" in msg
        or "overloaded" in msg
        or "resource_exhausted" in msg
        or "rate limit" in msg
        or "rate_limit" in msg
        or "overloaded_error" in msg
    )


def retry_after_from_exception(exc: BaseException) -> float | None:
    """Best-effort extraction of the provider's suggested wait in seconds.

    Checks the ``Retry-After`` response header first, then Gemini's
    ``retryDelay: "23s"`` / "Please retry in 23.4s" hints in the body.
    """
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is not None:
        try:
            raw = headers.get("retry-after")
            if raw is not None:
                return float(raw)
        except (TypeError, ValueError):
            pass
    msg = str(exc)
    for rx in (_RETRY_DELAY_RE, _RETRY_IN_RE):
        m = rx.search(msg)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
    return None


async def retry_on_rate_limit(
    call: Callable[[], Awaitable[T]],
    *,
    delays: Sequence[float],
    label: str,
) -> T:
    """Run ``call``; on :class:`LLMRateLimitError` back off and retry.

    ``delays`` is the backoff schedule (e.g. ``(2, 5)`` → up to 3 attempts).
    When the provider asks for a wait longer than
    :data:`MAX_INLINE_RETRY_WAIT_SECONDS` we stop retrying immediately and
    re-raise so the partner sees "try again in N seconds" rather than a
    request that hangs for minutes.
    """
    attempt = 0
    while True:
        try:
            return await call()
        except LLMRateLimitError as e:
            if attempt >= len(delays):
                raise
            hinted = e.retry_after_seconds
            if hinted is not None and hinted > MAX_INLINE_RETRY_WAIT_SECONDS:
                raise
            wait = max(delays[attempt], hinted or 0.0)
            attempt += 1
            _logger.warning(
                "%s rate limited; retry %d/%d in %.1fs", label, attempt, len(delays), wait
            )
            await asyncio.sleep(wait)


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """Output of a single LLM call."""

    content: str
    model: str
    provider_name: str
    # input / output token counts when the SDK exposes them; useful for cost
    # accounting downstream.
    input_tokens: int | None = None
    output_tokens: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class LLMProvider(abc.ABC):
    """Async LLM provider interface.

    The contract is deliberately minimal — one synchronous-feeling
    ``generate_json`` method that always returns parsed JSON. Agents that
    need free-form text can call ``generate_text``.
    """

    name: str
    model: str

    @abc.abstractmethod
    async def generate_text(
        self,
        *,
        system: str,
        user: str,
        max_output_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> LLMResponse:
        ...

    async def generate_json(
        self,
        *,
        system: str,
        user: str,
        max_output_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> dict[str, Any]:
        """Convenience: call :meth:`generate_text` and JSON-parse the result.

        Subclasses can override if their SDK exposes a structured-output
        mode that's strictly better than parsing free text.
        """
        import json

        response = await self.generate_text(
            system=system,
            user=user,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
        )
        # Anthropic / OpenAI sometimes wrap JSON in ```json ... ``` fences;
        # strip those before parsing.
        text = _strip_code_fence(response.content)
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise JsonSchemaValidationError(
                f"{self.name} returned non-JSON content: {e}"
            ) from e


def _strip_code_fence(text: str) -> str:
    s = text.strip()
    if not s.startswith("```"):
        return s
    # Strip leading ```[lang]\n and trailing ```
    lines = s.splitlines()
    if len(lines) < 2:
        return s
    first = lines[0].strip()
    if first.startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines)
