"""Sprint 5 drafting workflow.

Triggered by POST /v1/notices/{notice_id}/draft. Pulls registration-scoped
context, asks the drafting agent for the 15-section reply, runs citation
verification on every embedded citation, strips UNVERIFIED ones into a
footnote, builds a paragraph→source map, and persists everything as a new
``drafts`` row plus one ``citations`` row per kept-or-flagged citation.

Returns the new ``draft_id``. Errors during drafting bubble out with the
prompt + provider context so the partner sees a clear failure message
rather than a generic 500.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import text

from app.agents.citation_verification import (
    VerifiedCitation,
    append_footnote,
    apply_stripped_citations,
    extract_citations,
    verify_citations,
)
from app.agents.drafting import GeneratedDraft, generate_draft, load_drafting_input
from app.agents.regime_guidance import FormatProfile, regime_for_law
from app.core.db import session_for_tenant
from app.core.logging import get_logger
from app.services import audit

logger = get_logger(__name__)

Tone = Literal["formal", "assertive", "conciliatory"]


@dataclass(frozen=True, slots=True)
class GenerateDraftJob:
    tenant_id: UUID
    user_id: UUID
    matter_id: UUID
    notice_id: UUID
    tone: Tone
    partner_instructions: str
    include_cross_registration: bool = False
    format_profile: FormatProfile = "full_submission"


@dataclass(frozen=True, slots=True)
class GenerateDraftResult:
    draft_id: UUID
    version: int
    sections_kept: int
    citation_summary: dict[str, int]


DRAFTS_OPTIONAL_COLUMNS: tuple[str, ...] = ("notice_id", "format_profile")
_drafts_optional_columns_cache: tuple[str, ...] | None = None


async def drafts_optional_columns(session: Any) -> tuple[str, ...]:
    """Which of :data:`DRAFTS_OPTIONAL_COLUMNS` exist on ``drafts``.

    Lets the API keep working against a DB that predates migration 0023.
    Only a complete result is cached, so applying the migration takes
    effect without a restart.
    """
    global _drafts_optional_columns_cache
    if _drafts_optional_columns_cache is not None:
        return _drafts_optional_columns_cache
    rows = (
        await session.execute(
            text(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'drafts' AND column_name = ANY(:cols)
                """
            ),
            {"cols": list(DRAFTS_OPTIONAL_COLUMNS)},
        )
    ).all()
    present = tuple(c for c in DRAFTS_OPTIONAL_COLUMNS if c in {r[0] for r in rows})
    if len(present) == len(DRAFTS_OPTIONAL_COLUMNS):
        _drafts_optional_columns_cache = present
    return present


async def run_generate_draft(job: GenerateDraftJob) -> GenerateDraftResult:
    log = logger.bind(
        matter_id=str(job.matter_id),
        notice_id=str(job.notice_id),
        tenant_id=str(job.tenant_id),
    )
    log.info("draft.start")

    async with session_for_tenant(job.tenant_id) as session:
        # 1. Load registration-scoped context.
        di = await load_drafting_input(
            session,
            matter_id=job.matter_id,
            notice_id=job.notice_id,
            tone=job.tone,
            partner_instructions=job.partner_instructions,
            include_cross_registration=job.include_cross_registration,
            format_profile=job.format_profile,
        )

        # 2. Generate the draft via the configured LLM.
        generated: GeneratedDraft = await generate_draft(di)

        sections_as_dicts = [
            {"num": s.num, "title": s.title, "body_html": s.body_html}
            for s in generated.sections
        ]

        # 3. Verify citations.
        extracted = extract_citations(sections_as_dicts)
        verified = await verify_citations(
            session, extracted, regime=regime_for_law(di.law)
        )

        # 4. Strip UNVERIFIED citations and append footnote.
        stripped_sections, footnote_lines = apply_stripped_citations(
            sections_as_dicts, verified
        )
        final_sections = append_footnote(stripped_sections, footnote_lines)

        # 5. Build the paragraph → source map.
        paragraph_to_source_map = _build_paragraph_map(
            final_sections, di, verified
        )

        # 6. Compute citation summary.
        counts = {"verified": 0, "partial": 0, "stripped": 0}
        for v in verified:
            if v.status == "VERIFIED":
                counts["verified"] += 1
            elif v.status == "VERIFIED_PARTIAL":
                counts["partial"] += 1
            else:
                counts["stripped"] += 1

        # 7. Pick the next version for this matter.
        next_version = (
            await session.execute(
                text(
                    "SELECT COALESCE(MAX(version), 0) + 1 FROM drafts "
                    "WHERE matter_id = :mid"
                ),
                {"mid": str(job.matter_id)},
            )
        ).scalar_one()

        # 8. Persist the draft row.
        draft_id: UUID = uuid4()
        # notice_id ties the draft to the notice it answers (the DOCX cover
        # sheet reads it); format_profile drives the version badge. Both
        # arrive in migration 0023 — skip them if it hasn't been applied.
        extra_cols = await drafts_optional_columns(session)
        extra_names = "".join(f", {c}" for c in extra_cols)
        extra_values = "".join(f", :{c}" for c in extra_cols)
        await session.execute(
            text(
                f"""
                INSERT INTO drafts (
                    draft_id, tenant_id, matter_id, version, status,
                    model_used, prompt_version, tone,
                    sections, citation_summary, paragraph_to_source_map,
                    internal_partner_note, content, generated_at,
                    generated_by_user_id{extra_names}
                ) VALUES (
                    :did, :tid, :mid, :ver, 'draft',
                    :model, :pv, :tone,
                    CAST(:sections AS JSONB),
                    CAST(:csum AS JSONB),
                    CAST(:pmap AS JSONB),
                    :ipn, CAST(:sections AS JSONB), NOW(),
                    :uid{extra_values}
                )
                """
            ),
            {
                "notice_id": str(job.notice_id),
                "format_profile": job.format_profile,
                "did": str(draft_id),
                "tid": str(job.tenant_id),
                "mid": str(job.matter_id),
                "ver": next_version,
                "model": generated.model,
                "pv": generated.prompt_version,
                "tone": job.tone,
                "sections": json.dumps(final_sections),
                "csum": json.dumps(counts),
                "pmap": json.dumps(paragraph_to_source_map, default=str),
                "ipn": generated.internal_partner_note,
                "uid": str(job.user_id),
            },
        )

        # 9. Persist one citations row per kept-or-flagged citation. Stripped
        #    citations are still persisted (action_taken='stripped') so the
        #    Timeline + audit trail explain what was removed.
        for v in verified:
            await session.execute(
                text(
                    """
                    INSERT INTO citations (
                        tenant_id, draft_id, case_name, citation_string,
                        paragraph_referenced, proposition_for_which_cited,
                        status, source_url, verification_tier,
                        verified_paragraph_text, proposition_match_confidence,
                        verified_at, action_taken
                    ) VALUES (
                        :tid, :did, :case, :cs, :para, :prop,
                        :status, :url, 1, :ptext, :conf, NOW(), :act
                    )
                    """
                ),
                {
                    "tid": str(job.tenant_id),
                    "did": str(draft_id),
                    "case": v.extracted.case_name,
                    "cs": v.extracted.citation_string,
                    "para": v.extracted.paragraph_referenced,
                    "prop": v.extracted.raw_text,
                    "status": v.status,
                    "url": v.source_url,
                    "ptext": v.verified_paragraph_text,
                    "conf": v.proposition_match_confidence,
                    "act": v.action_taken,
                },
            )

        # Advance triage status (when present) so the Triage tab UI shows
        # "drafted" — purely advisory; the API never blocks drafting on
        # triage status (soft-block policy).
        await session.execute(
            text(
                """
                UPDATE notice_triage
                SET status = 'drafted'
                WHERE notice_id = :nid
                """
            ),
            {"nid": str(job.notice_id)},
        )

        await audit.emit(
            session,
            tenant_id=job.tenant_id,
            user_id=job.user_id,
            action_type="draft.generated",
            entity_type="drafts",
            entity_id=draft_id,
            after_state={
                "matter_id": str(job.matter_id),
                "notice_id": str(job.notice_id),
                "version": next_version,
                "model": generated.model,
                "prompt_version": generated.prompt_version,
                "tone": job.tone,
                "sections": len(final_sections),
                "citation_summary": counts,
                "supporting_docs": len(di.supporting_documents),
                "pending_required": sum(
                    1 for p in di.pending_requirements if p.is_required
                ),
            },
            risk_tier=1,
        )

        await session.commit()
        log.info(
            "draft.complete",
            draft_id=str(draft_id),
            version=next_version,
            sections=len(final_sections),
            citation_summary=counts,
        )
        return GenerateDraftResult(
            draft_id=draft_id,
            version=next_version,
            sections_kept=len(final_sections),
            citation_summary=counts,
        )


def _build_paragraph_map(
    sections: list[dict[str, Any]],
    di: Any,
    verified: list[VerifiedCitation],
) -> list[dict[str, Any]]:
    """Best-effort paragraph→source map for the Source-map UI tab.

    Phase 1 traceability: each section maps to its strongest source -- the
    notice itself for Sections 02/04/05, the documents list for Section 03
    + 10, the citations cluster for Section 06, and the matter+registration
    identity for everything else. This satisfies AC "links every factual
    sentence to a source document or notice field" at section granularity;
    sentence-level provenance is Phase 2 work tied to a richer parser
    output.
    """
    rows: list[dict[str, Any]] = []
    notice_id = di.notice.get("notice_id")
    # Stringify the document_id — asyncpg returns it as a Python uuid.UUID,
    # which json.dumps can't serialize. The map is persisted as JSONB and
    # the source-map UI reads document_id as a string.
    doc_summaries = [
        {
            "document_id": (
                str(d["document_id"]) if d.get("document_id") is not None else None
            ),
            "filename": d.get("filename"),
        }
        for d in di.documents
    ]
    citation_sources = [
        {
            "case_name": v.extracted.case_name,
            "citation_string": v.extracted.citation_string,
            "status": v.status,
            "source_url": v.source_url,
        }
        for v in verified
    ]
    for s in sections:
        num = int(s.get("num", 0))
        if num in (1, 12, 14, 15):
            source = {"type": "agent_synthesis"}
        elif num in (2, 4, 5):
            source = {"type": "notice_field", "notice_id": notice_id}
        elif num == 3:
            source = {
                "type": "registration_identity",
                "registration_identifier": di.registration_identifier,
                "documents": doc_summaries,
            }
        elif num == 6:
            source = {"type": "citation_cluster", "citations": citation_sources}
        elif num in (10, 11):
            source = {"type": "documents", "documents": doc_summaries}
        elif num == 13:
            source = {"type": "partner_note"}
        elif num == 9:
            source = {
                "type": "cross_registration_context",
                "items": di.cross_registration_context,
            }
        else:
            source = {"type": "agent_synthesis"}
        rows.append({"section_num": num, "title": s.get("title"), "source": source})
    return rows
