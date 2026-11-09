"""Vendor-abstracted LLM access.

Same pattern as ``services/ocr``: a small ``LLMProvider`` interface with
concrete implementations for Anthropic (primary) and OpenAI (secondary), plus
a stub for tests, all selected by env config. Agents call providers, never
SDKs directly.
"""

from app.services.llm.base import (
    JsonSchemaValidationError,
    LLMError,
    LLMProvider,
    LLMRateLimitError,
    LLMResponse,
    LLMTransientError,
)
from app.services.llm.factory import (
    KNOWN_LLM_PROVIDERS,
    get_llm_for_agent,
    get_llm_provider,
    get_primary_llm,
    get_secondary_llm,
    get_secondary_llm_for_agent,
)

__all__ = [
    "JsonSchemaValidationError",
    "KNOWN_LLM_PROVIDERS",
    "LLMError",
    "LLMProvider",
    "LLMRateLimitError",
    "LLMResponse",
    "LLMTransientError",
    "get_llm_for_agent",
    "get_llm_provider",
    "get_primary_llm",
    "get_secondary_llm",
    "get_secondary_llm_for_agent",
]
