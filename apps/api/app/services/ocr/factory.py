"""OCR factory keyed off ``OCR_PROVIDER_PRIMARY`` / ``OCR_PROVIDER_FALLBACK``.

Switching primary between providers is a config change, not a code change.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache

from app.core.config import get_settings
from app.services.ocr.azure_document_intelligence import AzureDocumentIntelligenceProvider
from app.services.ocr.base import OCRError, OCRProvider
from app.services.ocr.gemini import GeminiOCRProvider
from app.services.ocr.google_document_ai import GoogleDocumentAIProvider
from app.services.ocr.stub import StubOCRProvider

KNOWN_PROVIDERS: frozenset[str] = frozenset(
    {"google_doc_ai", "azure_doc_intel", "gemini", "stub"}
)


def _build_google() -> OCRProvider:
    s = get_settings()
    if not (
        s.google_doc_ai_project_id
        and s.google_doc_ai_location
        and s.google_doc_ai_processor_id
    ):
        raise OCRError(
            "GOOGLE_DOC_AI_PROJECT_ID, GOOGLE_DOC_AI_LOCATION, and "
            "GOOGLE_DOC_AI_PROCESSOR_ID must all be set for google_doc_ai"
        )
    return GoogleDocumentAIProvider(
        project_id=s.google_doc_ai_project_id,
        location=s.google_doc_ai_location,
        processor_id=s.google_doc_ai_processor_id,
    )


def _build_azure() -> OCRProvider:
    s = get_settings()
    if not (s.azure_doc_intel_endpoint and s.azure_doc_intel_api_key):
        raise OCRError(
            "AZURE_DOC_INTEL_ENDPOINT and AZURE_DOC_INTEL_API_KEY must both be set "
            "for azure_doc_intel"
        )
    return AzureDocumentIntelligenceProvider(
        endpoint=s.azure_doc_intel_endpoint,
        api_key=s.azure_doc_intel_api_key,
    )


def _build_gemini() -> OCRProvider:
    s = get_settings()
    api_key = s.gemini_api_key or s.openai_api_key
    if not api_key:
        raise OCRError("GEMINI_API_KEY or OPENAI_API_KEY must be set for gemini OCR")
    return GeminiOCRProvider(
        api_key=api_key,
        model=s.gemini_model or s.openai_model or "gemini-3-flash-preview",
        fallback_models=s.openai_fallback_model_list,
    )


def _build_stub() -> OCRProvider:
    return StubOCRProvider()


_BUILDERS: dict[str, Callable[[], OCRProvider]] = {
    "google_doc_ai": _build_google,
    "azure_doc_intel": _build_azure,
    "gemini": _build_gemini,
    "stub": _build_stub,
}


@lru_cache(maxsize=4)
def get_ocr_provider(name: str) -> OCRProvider:
    if name not in _BUILDERS:
        raise OCRError(f"unknown OCR provider: {name!r} (valid: {sorted(_BUILDERS)})")
    return _BUILDERS[name]()


def get_primary_provider() -> OCRProvider:
    return get_ocr_provider(get_settings().ocr_provider_primary)


def get_fallback_provider() -> OCRProvider | None:
    name = get_settings().ocr_provider_fallback
    if not name or name == get_settings().ocr_provider_primary:
        return None
    return get_ocr_provider(name)
