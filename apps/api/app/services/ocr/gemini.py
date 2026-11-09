"""Gemini multimodal OCR provider for notices."""

from __future__ import annotations

import base64
import re
from collections.abc import Sequence

import httpx

from app.services.ocr.base import ExtractedDoc, OCRError, OCRProvider, OCRTransientError


class _GeminiBusyError(OCRTransientError):
    """HTTP 429 / 503 from Gemini: overloaded or rate-limited model."""


def _provider_message(res: httpx.Response) -> str:
    """Gemini's own one-line reason ("This model is currently experiencing
    high demand ...") instead of the whole JSON error body."""
    try:
        return str(res.json()["error"]["message"])[:300]
    except Exception:  # noqa: BLE001 — non-JSON bodies
        return res.text[:300]


class GeminiOCRProvider(OCRProvider):
    name = "gemini"

    INSTRUCTION = (
        "Transcribe this document exactly, page by page. Start every page with a line "
        "'=== PAGE n ===' (n = page number starting from 1). Keep the original wording, "
        "numbers, dates, tables (one row per line, cells separated by ' | ') and "
        "paragraph numbering. Do not summarise, translate, correct or add anything. "
        "Output plain text only."
    )

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gemini-3-flash-preview",
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        timeout_seconds: float = 120.0,
        # Models tried in order when ``model`` is overloaded / rate-limited
        # (HTTP 429 / 503) — each Gemini model has its own capacity and quota.
        fallback_models: Sequence[str] = (),
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._fallback_models = tuple(m for m in fallback_models if m and m != model)

    async def extract(self, file_bytes: bytes, mime_type: str) -> ExtractedDoc:
        if not file_bytes:
            raise OCRError("gemini OCR: empty document")
        last: OCRTransientError | None = None
        for model in (self._model, *self._fallback_models):
            try:
                return await self._extract_with(model, file_bytes, mime_type)
            except _GeminiBusyError as e:
                # Overloaded / rate-limited: another model may have capacity.
                last = e
                continue
        assert last is not None
        raise last

    async def _extract_with(
        self, model: str, file_bytes: bytes, mime_type: str
    ) -> ExtractedDoc:
        mime = mime_type if mime_type else "application/pdf"
        b64 = base64.b64encode(file_bytes).decode("utf-8")
        # Key goes in a header, not the query string: httpx logs every
        # request URL at INFO, which wrote the key into the server log.
        url = f"{self._base_url}/models/{model}:generateContent"
        headers = {"x-goog-api-key": self._api_key}
        payload = {
            "contents": [
                {
                    "parts": [
                        {"inline_data": {"mime_type": mime, "data": b64}},
                        {"text": self.INSTRUCTION},
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0.0,
            },
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                res = await client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as e:
            raise OCRTransientError(f"gemini OCR timed out after {self._timeout}s") from e
        except Exception as e:
            raise OCRTransientError(f"gemini OCR request failed: {e}") from e

        if res.status_code != 200:
            err_msg = _provider_message(res)
            if res.status_code in (429, 503):
                raise _GeminiBusyError(
                    f"Gemini OCR is busy ({res.status_code}, {model}): {err_msg}"
                )
            if res.status_code in (500, 502, 504):
                raise OCRTransientError(
                    f"Gemini OCR transient error ({res.status_code}, {model}): {err_msg}"
                )
            raise OCRError(f"Gemini OCR error ({res.status_code}, {model}): {err_msg}")

        data = res.json()
        try:
            text_out = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as e:
            raise OCRError(f"gemini OCR: unexpected response format: {data}") from e

        page_markers = re.findall(r"(?im)^=== PAGE \d+ ===\s*$", text_out)
        page_count = max(1, len(page_markers))

        return ExtractedDoc(
            text=text_out,
            layout_json={"provider": "gemini", "model": model, "page_count": page_count},
            page_count=page_count,
            provider_name=self.name,
        )
