"""Notice Triage Agent.

Reads a parsed notice + OCR text and produces (a) a partner-facing summary
and (b) a document checklist that tells the partner which evidentiary
documents to gather before drafting. Triggered manually by the partner
clicking "Begin triage" on the matter — never auto-fired after parse.

Orchestration mirrors :mod:`app.agents.drafting`: a prompt-versioned system
message + a templated user prompt, run through the configured primary LLM
with the secondary as a fallback, parsed as strict JSON.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.llm import (
    JsonSchemaValidationError,
    LLMError,
    LLMProvider,
    LLMRateLimitError,
    LLMTransientError,
    get_llm_for_agent,
    get_secondary_llm_for_agent,
)

logger = get_logger(__name__)

PROMPTS_DIR: Final[Path] = Path(__file__).parent / "prompts"
PROMPT_VERSION: Final[str] = "notice_triage_v1"

# Same 20K char default as the drafter — keeps token budget predictable and
# is plenty for the agent to identify what evidence is missing.
TRIAGE_OCR_EXCERPT_CHARS: Final[int] = (
    get_settings().triage_ocr_excerpt_chars
)

# Smaller cap than the drafter — triage output is a summary + ~10 checklist
# items, comfortably under 2K tokens. 4000 leaves headroom for verbose
# rationales without inviting runaway.
TRIAGE_MAX_OUTPUT_TOKENS: Final[int] = (
    get_settings().triage_max_output_tokens
)

# Doc types the agent is allowed to emit. Kept in sync with the prompt and
# with the frontend's icon mapping. ``other`` is the catch-all.
_ALLOWED_DOC_TYPES: Final[frozenset[str]] = frozenset({
    "gstr_3b", "gstr_1", "gstr_2a_2b", "gstr_9", "gstr_9c",
    "itc_ledger", "invoice", "e_way_bill", "bank_statement",
    "reconciliation", "itr", "form_26as", "ais_tis",
    "contract", "ledger_extract", "board_resolution",
    "reply_to_prior_notice", "other",
})


@dataclass(frozen=True, slots=True)
class ChecklistItem:
    label: str
    rationale: str
    doc_type: str
    is_required: bool
    position: int


@dataclass(frozen=True, slots=True)
class GeneratedTriage:
    summary: str
    checklist: list[ChecklistItem]
    prompt_version: str
    model: str
    provider_name: str
    input_tokens: int | None
    output_tokens: int | None


@dataclass(frozen=True, slots=True)
class TriageInput:
    """Everything the triage agent needs. Loaded by :func:`load_triage_input`."""

    notice_id: UUID
    matter_id: UUID
    client_legal_name: str
    client_pan: str
    client_entity_type: str | None
    registration_type: str
    registration_identifier: str
    registration_state_name: str | None
    law: str
    financial_year: str | None
    assessment_year: str | None
    notice: dict[str, Any]
    raw_extracted_json: dict[str, Any]
    notice_ocr_excerpt: str = ""
    existing_documents: list[dict[str, Any]] = field(default_factory=list)


async def load_triage_input(
    session: AsyncSession,
    *,
    notice_id: UUID,
) -> TriageInput:
    row = (
        await session.execute(
            text(
                """
                SELECT m.matter_id, m.registration_id, m.law,
                       m.financial_year, m.assessment_year,
                       c.legal_name AS client_legal_name,
                       c.pan        AS client_pan,
                       c.entity_type AS client_entity_type,
                       r.registration_type, r.identifier_value, r.state_name,
                       n.notice_id, n.document_type, n.notice_number,
                       n.din_or_rfn, n.issue_date, n.due_date, n.hearing_date,
                       n.authority, n.demand_amount, n.lifecycle_status,
                       n.raw_extracted_json,
                       LEFT(COALESCE(ib.ocr_text, ''), :ocr_chars) AS notice_ocr_excerpt
                FROM notices n
                JOIN matters m ON m.matter_id = n.matter_id
                JOIN clients c ON c.client_id = m.client_id
                JOIN client_registrations r ON r.registration_id = m.registration_id
                LEFT JOIN documents_inbox ib ON ib.inbox_id = n.source_inbox_id
                WHERE n.notice_id = :nid AND c.deleted_at IS NULL
                """
            ),
            {"nid": str(notice_id), "ocr_chars": TRIAGE_OCR_EXCERPT_CHARS},
        )
    ).mappings().first()
    if row is None:
        raise ValueError("notice not found")

    doc_rows = (
        await session.execute(
            text(
                """
                SELECT document_id, filename, document_type
                FROM documents
                WHERE matter_id = :mid
                ORDER BY uploaded_at ASC
                """
            ),
            {"mid": str(row["matter_id"])},
        )
    ).mappings().all()

    return TriageInput(
        notice_id=row["notice_id"],
        matter_id=row["matter_id"],
        client_legal_name=row["client_legal_name"],
        client_pan=row["client_pan"],
        client_entity_type=row["client_entity_type"],
        registration_type=row["registration_type"],
        registration_identifier=row["identifier_value"],
        registration_state_name=row["state_name"],
        law=row["law"],
        financial_year=row["financial_year"],
        assessment_year=row["assessment_year"],
        notice={
            "document_type": row["document_type"],
            "notice_number": row["notice_number"],
            "din_or_rfn": row["din_or_rfn"],
            "issue_date": (
                row["issue_date"].isoformat() if row["issue_date"] else None
            ),
            "due_date": row["due_date"].isoformat() if row["due_date"] else None,
            "hearing_date": (
                row["hearing_date"].isoformat() if row["hearing_date"] else None
            ),
            "authority": row["authority"],
            "demand_amount": (
                float(row["demand_amount"]) if row["demand_amount"] is not None else None
            ),
            "lifecycle_status": row["lifecycle_status"],
        },
        raw_extracted_json=(
            row["raw_extracted_json"] if isinstance(row["raw_extracted_json"], dict) else {}
        ),
        notice_ocr_excerpt=row["notice_ocr_excerpt"] or "",
        existing_documents=[dict(r) for r in doc_rows],
    )


def _load_prompt_template() -> tuple[str, str]:
    text_md = (PROMPTS_DIR / f"{PROMPT_VERSION}.md").read_text(encoding="utf-8")
    parts = text_md.split("## User prompt template", 1)
    sys_section = parts[0]
    user_section = parts[1] if len(parts) > 1 else ""
    sys_prompt = _extract_fenced(sys_section)
    user_template = _extract_fenced(user_section)
    if not sys_prompt or not user_template:
        raise LLMError(f"{PROMPT_VERSION}.md is missing the expected fenced blocks")
    return sys_prompt, user_template


def _extract_fenced(s: str) -> str:
    start = s.find("```")
    if start == -1:
        return ""
    nl = s.find("\n", start)
    if nl == -1:
        return ""
    end = s.find("```", nl + 1)
    if end == -1:
        return ""
    return s[nl + 1 : end].strip()


def _render_user_prompt(template: str, ti: TriageInput) -> str:
    state_qualifier = f" ({ti.registration_state_name})" if ti.registration_state_name else ""
    fy_or_ay = (
        f"FY {ti.financial_year}"
        if ti.financial_year
        else (f"AY {ti.assessment_year}" if ti.assessment_year else "—")
    )
    ocr_block = (
        ti.notice_ocr_excerpt.strip()
        if ti.notice_ocr_excerpt
        else "(notice was manually entered; rely on the structured extraction above)"
    )
    return (
        template
        .replace("{client.legal_name}", ti.client_legal_name)
        .replace("{client.pan}", ti.client_pan)
        .replace("{client.entity_type}", ti.client_entity_type or "—")
        .replace("{registration_type}", ti.registration_type)
        .replace("{registration.identifier_value}", ti.registration_identifier)
        .replace("{state_qualifier}", state_qualifier)
        .replace("{law}", ti.law)
        .replace("{fy_or_ay}", fy_or_ay)
        .replace("{notice.document_type}", ti.notice.get("document_type") or "—")
        .replace("{notice.notice_number}", ti.notice.get("notice_number") or "—")
        .replace("{notice.din_or_rfn}", ti.notice.get("din_or_rfn") or "—")
        .replace("{notice.issue_date}", ti.notice.get("issue_date") or "—")
        .replace("{notice.due_date}", ti.notice.get("due_date") or "—")
        .replace("{notice.authority}", ti.notice.get("authority") or "—")
        .replace(
            "{notice.demand_amount}",
            (
                f"₹{ti.notice['demand_amount']:.2f}"
                if ti.notice.get("demand_amount") is not None
                else "—"
            ),
        )
        .replace(
            "{raw_extracted_json}",
            json.dumps(ti.raw_extracted_json, indent=2, ensure_ascii=False),
        )
        .replace("{notice_ocr_excerpt}", ocr_block)
    )


def _validate(payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        raise JsonSchemaValidationError("triage response must be a JSON object")
    summary = payload.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise JsonSchemaValidationError("triage.summary must be a non-empty string")
    checklist = payload.get("checklist")
    if not isinstance(checklist, list) or not checklist:
        raise JsonSchemaValidationError("triage.checklist must be a non-empty list")
    seen_positions: set[int] = set()
    for item in checklist:
        if not isinstance(item, dict):
            raise JsonSchemaValidationError("each checklist item must be an object")
        if not isinstance(item.get("label"), str) or not item["label"].strip():
            raise JsonSchemaValidationError("checklist.label must be a non-empty string")
        if not isinstance(item.get("rationale"), str) or not item["rationale"].strip():
            raise JsonSchemaValidationError("checklist.rationale must be a non-empty string")
        doc_type = item.get("doc_type")
        if not isinstance(doc_type, str) or doc_type not in _ALLOWED_DOC_TYPES:
            # Coerce unknown values to ``other`` rather than reject — the
            # agent occasionally invents a type and we'd rather degrade
            # gracefully than fail the whole triage.
            item["doc_type"] = "other"
        if not isinstance(item.get("is_required"), bool):
            raise JsonSchemaValidationError("checklist.is_required must be a bool")
        pos = item.get("position")
        if not isinstance(pos, int) or pos < 1:
            raise JsonSchemaValidationError("checklist.position must be a positive int")
        if pos in seen_positions:
            raise JsonSchemaValidationError(f"duplicate checklist.position {pos}")
        seen_positions.add(pos)


async def _generate_with(
    provider: LLMProvider, system: str, user: str
) -> tuple[dict[str, Any], Any]:
    response = await provider.generate_text(
        system=system,
        user=user,
        max_output_tokens=TRIAGE_MAX_OUTPUT_TOKENS,
        temperature=0.2,
    )
    raw = response.content.strip()
    if raw.startswith("```"):
        first_nl = raw.find("\n")
        raw = raw[first_nl + 1 :] if first_nl != -1 else raw
        if raw.endswith("```"):
            raw = raw[:-3]
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        out_tokens = getattr(response, "output_tokens", None)
        if out_tokens is not None and out_tokens >= TRIAGE_MAX_OUTPUT_TOKENS - 100:
            raise JsonSchemaValidationError(
                f"triage hit output token cap ({out_tokens} / "
                f"{TRIAGE_MAX_OUTPUT_TOKENS}) and returned truncated JSON. "
                "Bump TRIAGE_MAX_OUTPUT_TOKENS in apps/api/.env (try 6000)."
            ) from e
        raise JsonSchemaValidationError(f"triage returned non-JSON: {e}") from e
    _validate(payload)
    return payload, response


async def generate_triage(ti: TriageInput) -> GeneratedTriage:
    """Run the triage agent. Tries primary then secondary on transient errors."""
    system, user_template = _load_prompt_template()
    user = _render_user_prompt(user_template, ti)

    last_err: Exception | None = None
    # A rate limit on either provider is the most actionable failure for the
    # partner ("wait N seconds") so it wins over e.g. a secondary's bad JSON.
    last_rate_limit: LLMRateLimitError | None = None
    for provider in (
        get_llm_for_agent("triage"),
        get_secondary_llm_for_agent("triage"),
    ):
        if provider is None:
            continue
        try:
            payload, response = await _generate_with(provider, system, user)
            checklist = [
                ChecklistItem(
                    label=item["label"].strip(),
                    rationale=item["rationale"].strip(),
                    doc_type=item["doc_type"],
                    is_required=bool(item["is_required"]),
                    position=int(item["position"]),
                )
                for item in sorted(payload["checklist"], key=lambda x: x["position"])
            ]
            return GeneratedTriage(
                summary=payload["summary"].strip(),
                checklist=checklist,
                prompt_version=PROMPT_VERSION,
                model=response.model,
                provider_name=response.provider_name,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
            )
        except LLMTransientError as e:
            logger.warning("triage transient error from %s: %s", provider.name, e)
            last_err = e
            if isinstance(e, LLMRateLimitError):
                last_rate_limit = e
            continue
        except (LLMError, JsonSchemaValidationError) as e:
            logger.exception("triage failed via %s: %s", provider.name, e)
            last_err = e
            continue
    final_err = last_rate_limit or last_err
    raise LLMError(f"triage failed: {final_err}") from final_err

