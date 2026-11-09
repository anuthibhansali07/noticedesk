"""Drafting Agent.

LLM-backed generator of a 15-section reply draft for a notice. The agent is
strict about registration scoping — it sees facts only from the matter's own
registration, never from sibling GST registrations belonging to the same
client, even though they share a PAN.

The orchestration shape mirrors :mod:`app.agents.document_parsing`: a
prompt-versioned system message + a templated user prompt, run through the
configured primary LLM with the secondary as a fallback. The response is a
strict JSON shape we persist into ``drafts.sections``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.regime_guidance import (
    FormatProfile,
    render_format_profile,
    render_regime_guidance,
)
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
PROMPT_VERSION: Final[str] = "drafting_v4"

# Per-document excerpt cap for triage-attached supporting evidence. 5K chars
# (~1.2K tokens) is enough for the drafter to extract figures + key dates;
# bigger uploads (full GSTR-3B PDFs, ITC ledger dumps) get truncated. Total
# block size is capped further by SUPPORTING_EVIDENCE_MAX_CHARS.
SUPPORTING_DOC_EXCERPT_CHARS: Final[int] = (
    get_settings().drafting_supporting_doc_excerpt_chars
)
# Overall ceiling so a partner who attaches 20 docs doesn't blow the input
# token budget. Items beyond this cap are listed by filename but excerpt is
# omitted; the drafter can still reference them by name.
SUPPORTING_EVIDENCE_MAX_CHARS: Final[int] = (
    get_settings().drafting_supporting_evidence_max_chars
)

# How many characters of notice OCR text to feed the drafter. 20K handles
# the typical real-world range (1-page ASMT-10 through 15-page DRC-01 SCN);
# partners with long-tail notices can bump via DRAFTING_OCR_EXCERPT_CHARS
# in apps/api/.env. Claude Opus 4.7 has 200K context so the model is fine;
# the cap exists for cost predictability (each char ≈ 0.25 input tokens).
NOTICE_OCR_EXCERPT_CHARS: Final[int] = (
    get_settings().drafting_ocr_excerpt_chars
)

# Output cap for the drafting call. The v2 prompt produces ALL 15 sections
# including a substantive Para-wise Reply (1.5K+ tokens) and HTML-list
# Filing Checklist — a typical full draft lands at 6-10K output tokens, a
# big multi-issue notice at 12-16K. Hitting the cap returns truncated
# (invalid) JSON which the parser then rejects, so we set a comfortable
# default and let partners crank further via env if a notice still trips.
# Output tokens are charged only when actually emitted, so a high cap costs
# nothing on short drafts.
DRAFTING_MAX_OUTPUT_TOKENS: Final[int] = (
    get_settings().drafting_max_output_tokens
)

Tone = Literal["formal", "assertive", "conciliatory"]


@dataclass(frozen=True, slots=True)
class DraftSection:
    num: int
    title: str
    body_html: str


@dataclass(frozen=True, slots=True)
class SupportingDoc:
    """One triage-attached document. Carries the checklist context that
    made the partner choose it, so the drafter can tie evidence → allegation
    precisely instead of treating it as an undifferentiated doc dump."""

    requirement_label: str
    requirement_rationale: str
    requirement_doc_type: str | None
    filename: str
    document_type: str | None
    excerpt: str  # truncated to SUPPORTING_DOC_EXCERPT_CHARS by the loader


@dataclass(frozen=True, slots=True)
class PendingRequirement:
    """A checklist item the partner has NOT attached a doc to and has NOT
    marked N/A. The drafter sees these as "[DOCUMENT REQUESTED]" warrants."""

    label: str
    rationale: str
    is_required: bool


@dataclass(frozen=True, slots=True)
class GeneratedDraft:
    sections: list[DraftSection]
    internal_partner_note: str
    client_summary: str
    prompt_version: str
    model: str
    provider_name: str
    input_tokens: int | None
    output_tokens: int | None


@dataclass(frozen=True, slots=True)
class DraftingInput:
    """Everything the agent needs. Loaded by :func:`load_drafting_input`."""

    matter_id: UUID
    tone: Tone
    partner_instructions: str
    client_legal_name: str
    client_pan: str
    client_entity_type: str | None
    registration_type: str  # "GST" or "IT"
    registration_identifier: str
    registration_state_name: str | None
    law: str  # "GST" or "IT"
    financial_year: str | None
    assessment_year: str | None
    notice: dict[str, Any]
    raw_extracted_json: dict[str, Any]
    # Up to ~6K chars of OCR text from the source PDF — gives the model the
    # numbered paragraphs of the notice so Para-wise Reply can mirror them
    # instead of punting. Empty when the notice was manually entered (no
    # source_inbox_id).
    notice_ocr_excerpt: str = ""
    prior_matters: list[dict[str, Any]] = field(default_factory=list)
    sibling_notices: list[dict[str, Any]] = field(default_factory=list)
    documents: list[dict[str, Any]] = field(default_factory=list)
    cross_registration_context: list[dict[str, Any]] = field(default_factory=list)
    # Triage-attached evidence. Empty when the partner skipped triage —
    # the drafter degrades cleanly to the v2 behaviour in that case.
    supporting_documents: list[SupportingDoc] = field(default_factory=list)
    pending_requirements: list[PendingRequirement] = field(default_factory=list)
    # Reply shape the partner picked (full 15-section / tabular / adjournment).
    format_profile: FormatProfile = "full_submission"


async def load_drafting_input(
    session: AsyncSession,
    *,
    matter_id: UUID,
    notice_id: UUID,
    tone: Tone,
    partner_instructions: str,
    include_cross_registration: bool = False,
    format_profile: FormatProfile = "full_submission",
) -> DraftingInput:
    """Pull every fact the drafter is allowed to see, scoped to the matter's
    own registration. ``include_cross_registration`` is the explicit
    partner-toggle for Section 09; default False keeps registration
    scoping airtight.
    """
    row = (
        await session.execute(
            text(
                """
                SELECT m.matter_id, m.registration_id, m.client_id, m.law,
                       m.financial_year, m.assessment_year,
                       c.legal_name AS client_legal_name,
                       c.pan        AS client_pan,
                       c.entity_type AS client_entity_type,
                       r.registration_type, r.identifier_value, r.state_name,
                       n.notice_id, n.document_type, n.notice_number,
                       n.din_or_rfn, n.issue_date, n.due_date, n.hearing_date,
                       n.authority, n.demand_amount, n.lifecycle_status,
                       n.raw_extracted_json,
                       n.source_inbox_id,
                       LEFT(COALESCE(ib.ocr_text, ''), :ocr_chars) AS notice_ocr_excerpt
                FROM matters m
                JOIN clients c ON c.client_id = m.client_id
                JOIN client_registrations r ON r.registration_id = m.registration_id
                JOIN notices n ON n.notice_id = :nid
                LEFT JOIN documents_inbox ib ON ib.inbox_id = n.source_inbox_id
                WHERE m.matter_id = :mid AND c.deleted_at IS NULL
                """
            ),
            {
                "mid": str(matter_id),
                "nid": str(notice_id),
                "ocr_chars": NOTICE_OCR_EXCERPT_CHARS,
            },
        )
    ).mappings().first()
    if row is None:
        raise ValueError("matter or notice not found")

    # Strict scope: only matters that share this matter's registration_id.
    prior_matters_rows = (
        await session.execute(
            text(
                """
                SELECT matter_id, financial_year, assessment_year, opening_date,
                       closing_date, status
                FROM matters
                WHERE registration_id = :rid AND matter_id <> :mid
                ORDER BY opening_date DESC NULLS LAST
                LIMIT 10
                """
            ),
            {"rid": str(row["registration_id"]), "mid": str(matter_id)},
        )
    ).mappings().all()

    sibling_rows = (
        await session.execute(
            text(
                """
                SELECT notice_id, document_type, due_date, lifecycle_status,
                       raw_extracted_json
                FROM notices
                WHERE registration_id = :rid AND notice_id <> :nid
                  AND lifecycle_status IN ('issued','in_progress','due','due_date_over')
                ORDER BY due_date ASC NULLS LAST
                LIMIT 10
                """
            ),
            {"rid": str(row["registration_id"]), "nid": str(notice_id)},
        )
    ).mappings().all()

    doc_rows = (
        await session.execute(
            text(
                """
                SELECT document_id, filename, document_type, lifecycle_stage,
                       LEFT(COALESCE(extracted_text, ''), 4000) AS excerpt
                FROM documents
                WHERE matter_id = :mid
                ORDER BY uploaded_at ASC
                """
            ),
            {"mid": str(matter_id)},
        )
    ).mappings().all()

    # Triage-attached evidence (status='uploaded') with each doc's extracted
    # text excerpt, plus the checklist context (label + rationale) so the
    # drafter knows which allegation each doc was attached against.
    supporting_rows = (
        await session.execute(
            text(
                """
                SELECT r.label        AS req_label,
                       r.rationale    AS req_rationale,
                       r.doc_type     AS req_doc_type,
                       d.filename     AS doc_filename,
                       d.document_type AS doc_document_type,
                       LEFT(COALESCE(d.extracted_text, ''), :doc_chars) AS doc_excerpt
                FROM notice_document_requirements r
                JOIN documents d ON d.document_id = r.document_id
                WHERE r.notice_id = :nid AND r.status = 'uploaded'
                ORDER BY r.position ASC
                """
            ),
            {"nid": str(notice_id), "doc_chars": SUPPORTING_DOC_EXCERPT_CHARS},
        )
    ).mappings().all()
    supporting_docs = [
        SupportingDoc(
            requirement_label=r["req_label"],
            requirement_rationale=r["req_rationale"],
            requirement_doc_type=r["req_doc_type"],
            filename=r["doc_filename"],
            document_type=r["doc_document_type"],
            excerpt=r["doc_excerpt"] or "",
        )
        for r in supporting_rows
    ]

    pending_rows = (
        await session.execute(
            text(
                """
                SELECT label, rationale, is_required
                FROM notice_document_requirements
                WHERE notice_id = :nid AND status = 'pending'
                ORDER BY position ASC
                """
            ),
            {"nid": str(notice_id)},
        )
    ).mappings().all()
    pending_reqs = [
        PendingRequirement(
            label=r["label"],
            rationale=r["rationale"],
            is_required=bool(r["is_required"]),
        )
        for r in pending_rows
    ]

    cross_block: list[dict[str, Any]] = []
    if include_cross_registration:
        cross_block = [
            dict(r)
            for r in (
                await session.execute(
                    text(
                        """
                        SELECT n.notice_id, n.document_type, n.due_date,
                               n.lifecycle_status, n.law,
                               r.identifier_value, r.state_name,
                               LEFT(COALESCE(n.raw_extracted_json::TEXT, ''), 600) AS excerpt
                        FROM notices n
                        JOIN client_registrations r ON r.registration_id = n.registration_id
                        WHERE r.client_id = :cid AND n.registration_id <> :rid
                          AND n.lifecycle_status IN ('issued','in_progress','due','due_date_over')
                        ORDER BY n.due_date ASC NULLS LAST
                        LIMIT 10
                        """
                    ),
                    {"cid": str(row["client_id"]), "rid": str(row["registration_id"])},
                )
            ).mappings().all()
        ]

    return DraftingInput(
        matter_id=row["matter_id"],
        tone=tone,
        partner_instructions=partner_instructions or "",
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
            "notice_id": str(row["notice_id"]),
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
            "issue": (
                (row["raw_extracted_json"] or {}).get("issue")
                if isinstance(row["raw_extracted_json"], dict)
                else None
            ),
        },
        raw_extracted_json=(
            row["raw_extracted_json"] if isinstance(row["raw_extracted_json"], dict) else {}
        ),
        notice_ocr_excerpt=row["notice_ocr_excerpt"] or "",
        prior_matters=[dict(r) for r in prior_matters_rows],
        sibling_notices=[dict(r) for r in sibling_rows],
        documents=[dict(r) for r in doc_rows],
        cross_registration_context=cross_block,
        supporting_documents=supporting_docs,
        pending_requirements=pending_reqs,
        format_profile=format_profile,
    )


def _load_prompt_template() -> tuple[str, str]:
    """Return (system_prompt, user_template) parsed from the active prompt."""
    text_md = (PROMPTS_DIR / f"{PROMPT_VERSION}.md").read_text(encoding="utf-8")
    parts = text_md.split("## User prompt template", 1)
    sys_section = parts[0]
    user_section = parts[1] if len(parts) > 1 else ""
    # Pull the first fenced code block from each half.
    sys_prompt = _extract_fenced(sys_section)
    user_template = _extract_fenced(user_section)
    if not sys_prompt or not user_template:
        raise LLMError(f"{PROMPT_VERSION}.md is missing the expected fenced blocks")
    return sys_prompt, user_template


def _extract_fenced(s: str) -> str:
    start = s.find("```")
    if start == -1:
        return ""
    # Skip the opening fence + optional language tag line.
    nl = s.find("\n", start)
    if nl == -1:
        return ""
    end = s.find("```", nl + 1)
    if end == -1:
        return ""
    return s[nl + 1 : end].strip()


def _render_user_prompt(template: str, di: DraftingInput) -> str:
    state_qualifier = f" ({di.registration_state_name})" if di.registration_state_name else ""
    fy_or_ay = (
        f"FY {di.financial_year}"
        if di.financial_year
        else (f"AY {di.assessment_year}" if di.assessment_year else "—")
    )
    def _prior_line(m: dict[str, Any]) -> str:
        period = m.get("financial_year") or m.get("assessment_year") or "—"
        status = m.get("status") or "open"
        return f"- matter {m['matter_id']} · {period} · status {status}"

    prior_block = _format_list(
        di.prior_matters, _prior_line, empty="(none — first matter on this registration)"
    )

    def _sibling_line(n: dict[str, Any]) -> str:
        return f"- {n['document_type']} due {n.get('due_date')} · {n.get('lifecycle_status')}"

    sibling_block = _format_list(
        di.sibling_notices,
        _sibling_line,
        empty="(no other open notices on this registration)",
    )
    def _doc_line(d: dict[str, Any]) -> str:
        kind = d.get("document_type") or "unspecified"
        stage = d.get("lifecycle_stage") or "received"
        excerpt = (d.get("excerpt") or "").strip()[:280]
        return f"- [{kind}] {d['filename']} (stage: {stage})\n  excerpt: {excerpt}"

    docs_block = _format_list(di.documents, _doc_line, empty="(no documents attached yet)")
    cross_block = _format_list(
        di.cross_registration_context,
        lambda n: (
            f"- {n.get('state_name') or 'other registration'} · "
            f"{n['document_type']} · due {n.get('due_date')}"
        ),
        empty="(none — partner did not attach cross-registration context)",
    )

    ocr_block = (
        di.notice_ocr_excerpt.strip()
        if di.notice_ocr_excerpt
        else (
            "(notice was manually entered; no OCR text available — rely on "
            "the parsed fields + structured JSON above for the para-wise reply)"
        )
    )

    supporting_evidence_block = _render_supporting_evidence(
        di.supporting_documents, di.pending_requirements
    )

    return (
        template
        .replace("{client.legal_name}", di.client_legal_name)
        .replace("{client.pan}", di.client_pan)
        .replace("{client.entity_type}", di.client_entity_type or "—")
        .replace("{registration_type}", di.registration_type)
        .replace("{registration.identifier_value}", di.registration_identifier)
        .replace("{state_qualifier}", state_qualifier)
        .replace("{law}", di.law)
        .replace("{fy_or_ay}", fy_or_ay)
        .replace("{notice.document_type}", di.notice.get("document_type") or "—")
        .replace("{notice.din_or_rfn}", di.notice.get("din_or_rfn") or "—")
        .replace("{notice.notice_number}", di.notice.get("notice_number") or "—")
        .replace("{notice.issue_date}", di.notice.get("issue_date") or "—")
        .replace("{notice.due_date}", di.notice.get("due_date") or "—")
        .replace("{notice.authority}", di.notice.get("authority") or "—")
        .replace("{notice.issue}", di.notice.get("issue") or "—")
        .replace(
            "{notice.demand_amount}",
            (
                f"₹{di.notice['demand_amount']:.2f}"
                if di.notice.get("demand_amount") is not None
                else "—"
            ),
        )
        .replace(
            "{regime_guidance_block}",
            render_regime_guidance(di.law, di.notice.get("document_type")),
        )
        .replace("{format_profile_block}", render_format_profile(di.format_profile))
        .replace("{notice_ocr_excerpt}", ocr_block)
        .replace("{supporting_evidence_block}", supporting_evidence_block)
        .replace(
            "{raw_extracted_json}",
            json.dumps(di.raw_extracted_json, indent=2, ensure_ascii=False),
        )
        .replace("{prior_matters_block}", prior_block)
        .replace("{sibling_notices_block}", sibling_block)
        .replace("{documents_block}", docs_block)
        .replace("{cross_registration_block}", cross_block)
        .replace("{tone}", di.tone)
        .replace("{partner_instructions}", di.partner_instructions or "(none)")
    )


def _format_list(items: list[dict[str, Any]], fmt, *, empty: str) -> str:
    if not items:
        return empty
    return "\n".join(fmt(i) for i in items)


def _render_supporting_evidence(
    docs: list[SupportingDoc],
    pending: list[PendingRequirement],
) -> str:
    """Render the SUPPORTING EVIDENCE block for the user prompt.

    Three sub-sections: attached (with excerpts), pending (so the drafter
    knows to emit [DOCUMENT REQUESTED]), and a "none" footer when triage
    was never run / has no items. Caps total chars at
    SUPPORTING_EVIDENCE_MAX_CHARS so a 20-doc dump doesn't blow input budget.
    """
    if not docs and not pending:
        return (
            "(no triage checklist for this notice — either triage hasn't "
            "been run yet or the matter has no document requirements. "
            "Fall back to the DOCUMENTS ATTACHED block below.)"
        )

    parts: list[str] = []
    if docs:
        parts.append("ATTACHED (use these as primary evidence):")
        budget = SUPPORTING_EVIDENCE_MAX_CHARS
        for idx, d in enumerate(docs, start=1):
            header = (
                f"\n[{idx}] {d.requirement_label}\n"
                f"    rationale: {d.requirement_rationale}\n"
                f"    document : {d.filename}"
                + (f" ({d.document_type})" if d.document_type else "")
                + "\n    excerpt  :\n"
            )
            parts.append(header)
            excerpt = (d.excerpt or "").strip()
            if not excerpt:
                parts.append("        (no extracted text available for this document)")
                continue
            if budget <= 0:
                parts.append(
                    "        (excerpt omitted — total supporting-evidence budget "
                    "exceeded; reference by filename only)"
                )
                continue
            chunk = excerpt[:budget]
            parts.append("        " + chunk.replace("\n", "\n        "))
            budget -= len(chunk)

    if pending:
        parts.append(
            "\nPENDING — not attached (write [DOCUMENT REQUESTED] "
            "for facts that would have come from these):"
        )
        for p in pending:
            tag = "REQUIRED" if p.is_required else "optional"
            parts.append(f"  - [{tag}] {p.label} — {p.rationale}")

    return "\n".join(parts)


def _validate(payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        raise JsonSchemaValidationError("drafting response must be a JSON object")
    sections = payload.get("sections")
    if not isinstance(sections, list) or not sections:
        raise JsonSchemaValidationError("drafting response must include sections[]")
    seen_nums: set[int] = set()
    for s in sections:
        if not isinstance(s, dict):
            raise JsonSchemaValidationError("each section must be an object")
        if not isinstance(s.get("num"), int):
            raise JsonSchemaValidationError("each section.num must be an int")
        if s["num"] in seen_nums:
            raise JsonSchemaValidationError(f"duplicate section.num {s['num']}")
        seen_nums.add(s["num"])
        if not isinstance(s.get("title"), str) or not s["title"].strip():
            raise JsonSchemaValidationError("each section.title must be a non-empty string")
        if not isinstance(s.get("body_html"), str):
            raise JsonSchemaValidationError("each section.body_html must be a string")
    if not isinstance(payload.get("internal_partner_note", ""), str):
        raise JsonSchemaValidationError("internal_partner_note must be a string")


async def _generate_with(
    provider: LLMProvider, system: str, user: str
) -> tuple[dict[str, Any], Any]:
    response = await provider.generate_text(
        system=system,
        user=user,
        max_output_tokens=DRAFTING_MAX_OUTPUT_TOKENS,
        temperature=0.1,
    )
    raw = response.content.strip()
    # Strip ```json fences if present.
    if raw.startswith("```"):
        first_nl = raw.find("\n")
        raw = raw[first_nl + 1 :] if first_nl != -1 else raw
        if raw.endswith("```"):
            raw = raw[:-3]
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        # The most common cause is hitting max_output_tokens mid-string. The
        # response ends without closing quotes / brackets, so json.loads
        # raises "Unterminated string". Surface a partner-actionable hint.
        out_tokens = getattr(response, "output_tokens", None)
        if out_tokens is not None and out_tokens >= DRAFTING_MAX_OUTPUT_TOKENS - 100:
            raise JsonSchemaValidationError(
                f"drafter hit output token cap ({out_tokens} / "
                f"{DRAFTING_MAX_OUTPUT_TOKENS}) and returned truncated JSON. "
                "Bump DRAFTING_MAX_OUTPUT_TOKENS in apps/api/.env (try 24000)."
            ) from e
        raise JsonSchemaValidationError(f"drafter returned non-JSON: {e}") from e
    _validate(payload)
    return payload, response


async def generate_draft(di: DraftingInput) -> GeneratedDraft:
    """Run the drafting agent. Tries primary then secondary on transient errors."""
    system, user_template = _load_prompt_template()
    user = _render_user_prompt(user_template, di)

    last_err: Exception | None = None
    # A rate limit on either provider is the most actionable failure for the
    # partner ("wait N seconds") so it wins over e.g. a secondary's bad JSON.
    last_rate_limit: LLMRateLimitError | None = None
    for provider in (
        get_llm_for_agent("drafting"),
        get_secondary_llm_for_agent("drafting"),
    ):
        if provider is None:
            continue
        try:
            payload, response = await _generate_with(provider, system, user)
            sections = [
                DraftSection(num=s["num"], title=s["title"], body_html=s["body_html"])
                for s in payload["sections"]
            ]
            return GeneratedDraft(
                sections=sections,
                internal_partner_note=str(payload.get("internal_partner_note", "")),
                client_summary=str(payload.get("client_summary", "")),
                prompt_version=PROMPT_VERSION,
                model=response.model,
                provider_name=response.provider_name,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
            )
        except LLMTransientError as e:
            logger.warning("drafting transient error from %s: %s", provider.name, e)
            last_err = e
            if isinstance(e, LLMRateLimitError):
                last_rate_limit = e
            continue
        except (LLMError, JsonSchemaValidationError) as e:
            # Bad JSON / schema from one provider shouldn't stop us trying
            # the secondary — same policy as the triage agent.
            logger.exception("drafting failed via %s: %s", provider.name, e)
            last_err = e
            continue
    final_err = last_rate_limit or last_err
    raise LLMError(f"drafting failed: {final_err}") from final_err
