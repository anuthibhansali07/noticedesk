"""Demo-mode banner endpoint.

Exposes the active provider configuration so the UI can show "Live · Claude
Opus 4.7" vs "Demo · Stub" badges. Partners get visibility into whether
they're looking at a real LLM-generated draft or a deterministic template.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.core.config import get_settings

router = APIRouter()


@router.get("/demo/providers")
async def active_providers() -> dict[str, Any]:
    """Return which provider is wired into each pluggable surface. No auth
    required — this is purely about the running config, not tenant data.
    """
    s = get_settings()
    return {
        "llm": {
            "primary": s.llm_provider_primary,
            "secondary": s.llm_provider_secondary,
            "model": s.anthropic_model
            if s.llm_provider_primary == "anthropic"
            else (s.openai_model if s.llm_provider_primary == "openai" else "stub"),
            "is_stub": s.llm_provider_primary == "stub",
        },
        "ocr": {
            "primary": s.ocr_provider_primary,
            "is_stub": s.ocr_provider_primary == "stub",
        },
        "citation": {
            "primary": get_settings().citation_provider,
            "is_stub": get_settings().citation_provider == "stub",
        },
        "auth": {
            "provider": s.auth_provider,
            "is_dev": s.auth_provider == "dev",
        },
        "storage": {
            "backend": s.storage_backend,
            "is_local": s.storage_backend == "local",
        },
        "workflow_backend": s.workflow_backend,
        "environment": s.environment,
    }
