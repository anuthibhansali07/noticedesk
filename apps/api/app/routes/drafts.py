"""Draft + citation endpoints.

The matter-view "Draft reply" tab and the version-compare modal both go
through here.

- POST   /v1/notices/{notice_id}/draft        — generate a new draft version
- GET    /v1/matters/{matter_id}/drafts       — list versions for a matter
- GET    /v1/drafts/{id}                      — full draft (sections + map + citations)
- PATCH  /v1/drafts/{id}/section              — inline edit a section's body_html;
                                                 creates a NEW version of the draft
- GET    /v1/drafts/compare?a=...&b=...       — section-diff between two versions
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.core.config import get_settings
from app.core.errors import NotFoundError, ValidationError, llm_error_to_app_error
from app.core.logging import get_logger
from app.middleware.tenant_context import CurrentContext
from app.services import audit
from app.services.docx_export import CoverSheetData, render_draft_docx
from app.services.llm import LLMError
from app.workflows.drafting import (
    GenerateDraftJob,
    drafts_optional_columns,
    run_generate_draft,
)

logger = get_logger(__name__)

router = APIRouter()


class GenerateDraftRequest(BaseModel):
    tone: Literal["formal", "assertive", "conciliatory"] = "formal"
    partner_instructions: str = Field(default="", max_length=4000)
    include_cross_registration: bool = False
    format_profile: Literal[
        "full_submission", "tabular_reconciliation", "adjournment"
    ] = "full_submission"


@router.post("/notices/{notice_id}/draft", status_code=201)
async def generate_draft_for_notice(
    ctx: CurrentContext, notice_id: UUID, body: GenerateDraftRequest
) -> dict[str, Any]:
    """Trigger the drafting workflow synchronously. Returns the new draft_id.

    Phase 1 runs inline (a few seconds for the stub, ~60s for real Anthropic);
    Phase 2 moves this onto the Temporal queue with a progress endpoint the
    UI polls. For now the partner waits.
    """
    row = (
        await ctx.session.execute(
            text("SELECT matter_id FROM notices WHERE notice_id = :nid"),
            {"nid": str(notice_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError("notice not found")
    matter_id = row[0]
    # Commit our read transaction before handing off — the workflow opens
    # its own session via session_for_tenant().
    await ctx.session.commit()

    job = GenerateDraftJob(
        tenant_id=UUID(ctx.claims.tenant_id),
        user_id=UUID(ctx.claims.user_id),
        matter_id=matter_id,
        notice_id=notice_id,
        tone=body.tone,
        partner_instructions=body.partner_instructions,
        include_cross_registration=body.include_cross_registration,
        format_profile=body.format_profile,
    )
    try:
        result = await run_generate_draft(job)
    except LLMError as e:
        # Previously bubbled out as a bare 500 ("internal server error") on a
        # Gemini 429. Log the provider detail; send the partner a clean
        # 429 / 502 instead.
        logger.exception("draft generation failed: %s", e)
        raise llm_error_to_app_error(e, action="draft") from e
    except ValueError as e:
        raise NotFoundError(str(e)) from e
    return {
        "draft_id": str(result.draft_id),
        "version": result.version,
        "citation_summary": result.citation_summary,
        "sections_kept": result.sections_kept,
    }


@router.get("/matters/{matter_id}/drafts")
async def list_drafts_for_matter(
    ctx: CurrentContext, matter_id: UUID
) -> dict[str, Any]:
    """List every draft version on a matter for the version chip + diff UI."""
    opt = await drafts_optional_columns(ctx.session)
    opt_select = "".join(f", d.{c}" for c in opt)
    rows = (
        await ctx.session.execute(
            text(
                f"""
                SELECT d.draft_id, d.version, d.status, d.tone{opt_select},
                       d.model_used, d.prompt_version,
                       d.citation_summary, d.generated_at,
                       u.name AS generated_by_name
                FROM drafts d
                LEFT JOIN users u ON u.user_id = d.generated_by_user_id
                WHERE d.matter_id = :mid
                ORDER BY d.version DESC
                """
            ),
            {"mid": str(matter_id)},
        )
    ).mappings().all()
    return {
        "drafts": [
            {
                "draft_id": str(r["draft_id"]),
                "version": r["version"],
                "status": r["status"],
                "tone": r["tone"],
                "format_profile": r.get("format_profile"),
                "notice_id": str(r["notice_id"]) if r.get("notice_id") else None,
                "model_used": r["model_used"],
                "prompt_version": r["prompt_version"],
                "citation_summary": r["citation_summary"],
                "generated_at": (
                    r["generated_at"].isoformat() if r["generated_at"] else None
                ),
                "generated_by_name": r["generated_by_name"],
            }
            for r in rows
        ],
        "total": len(rows),
    }


@router.get("/drafts/{draft_id}")
async def get_draft(ctx: CurrentContext, draft_id: UUID) -> dict[str, Any]:
    opt = await drafts_optional_columns(ctx.session)
    opt_select = "".join(f", d.{c}" for c in opt)
    row = (
        await ctx.session.execute(
            text(
                f"""
                SELECT d.draft_id, d.matter_id, d.version, d.status, d.tone{opt_select},
                       d.model_used, d.prompt_version, d.sections,
                       d.citation_summary, d.paragraph_to_source_map,
                       d.internal_partner_note, d.edits_log, d.generated_at,
                       u.name AS generated_by_name
                FROM drafts d
                LEFT JOIN users u ON u.user_id = d.generated_by_user_id
                WHERE d.draft_id = :did
                """
            ),
            {"did": str(draft_id)},
        )
    ).mappings().first()
    if row is None:
        raise NotFoundError("draft not found")

    citations = (
        await ctx.session.execute(
            text(
                """
                SELECT citation_id, case_name, citation_string,
                       paragraph_referenced, proposition_for_which_cited,
                       status, source_url, verification_tier,
                       verified_paragraph_text, proposition_match_confidence,
                       verified_at, action_taken
                FROM citations
                WHERE draft_id = :did
                ORDER BY status, case_name
                """
            ),
            {"did": str(draft_id)},
        )
    ).mappings().all()

    return {
        "draft_id": str(row["draft_id"]),
        "matter_id": str(row["matter_id"]),
        "version": row["version"],
        "status": row["status"],
        "tone": row["tone"],
        "format_profile": row.get("format_profile"),
        "notice_id": str(row["notice_id"]) if row.get("notice_id") else None,
        "model_used": row["model_used"],
        "prompt_version": row["prompt_version"],
        "sections": row["sections"] or [],
        "citation_summary": row["citation_summary"] or {},
        "paragraph_to_source_map": row["paragraph_to_source_map"] or [],
        "internal_partner_note": row["internal_partner_note"],
        "edits_log": row["edits_log"] or [],
        "generated_at": row["generated_at"].isoformat() if row["generated_at"] else None,
        "generated_by_name": row["generated_by_name"],
        "citations": [
            {
                "citation_id": str(c["citation_id"]),
                "case_name": c["case_name"],
                "citation_string": c["citation_string"],
                "paragraph_referenced": c["paragraph_referenced"],
                "proposition_for_which_cited": c["proposition_for_which_cited"],
                "status": c["status"],
                "source_url": c["source_url"],
                "verification_tier": c["verification_tier"],
                "verified_paragraph_text": c["verified_paragraph_text"],
                "proposition_match_confidence": (
                    float(c["proposition_match_confidence"])
                    if c["proposition_match_confidence"] is not None
                    else None
                ),
                "verified_at": (
                    c["verified_at"].isoformat() if c["verified_at"] else None
                ),
                "action_taken": c["action_taken"],
            }
            for c in citations
        ],
    }


class EditSectionRequest(BaseModel):
    section_num: int
    body_html: str = Field(..., max_length=200_000)
    internal_partner_note: str | None = None


@router.patch("/drafts/{draft_id}/section")
async def edit_section(
    ctx: CurrentContext, draft_id: UUID, body: EditSectionRequest
) -> dict[str, Any]:
    """Inline edit of one section's body_html. Creates a NEW draft version
    rather than mutating the existing row so the version history stays
    traceable.

    The new version copies citations, paragraph_to_source_map, internal_note,
    and every other section verbatim; only the targeted section's body_html
    changes. ``edits_log`` on the new version captures who changed what.
    """
    opt = await drafts_optional_columns(ctx.session)
    opt_select = "".join(f", {c}" for c in opt)
    opt_values = "".join(f", :{c}" for c in opt)
    src = (
        await ctx.session.execute(
            text(
                f"""
                SELECT matter_id, sections, citation_summary{opt_select},
                       paragraph_to_source_map, internal_partner_note,
                       tone, model_used, prompt_version, edits_log
                FROM drafts WHERE draft_id = :did
                """
            ),
            {"did": str(draft_id)},
        )
    ).mappings().first()
    if src is None:
        raise NotFoundError("draft not found")

    sections = src["sections"] or []
    target = next(
        (s for s in sections if int(s.get("num", 0)) == body.section_num), None
    )
    if target is None:
        raise ValidationError(f"section {body.section_num} not in this draft")

    before_body = target.get("body_html", "")
    if before_body == body.body_html and (
        body.internal_partner_note is None
        or body.internal_partner_note == src["internal_partner_note"]
    ):
        return {"draft_id": str(draft_id), "changed": False}

    new_sections = [
        {**s, "body_html": body.body_html} if int(s.get("num", 0)) == body.section_num else s
        for s in sections
    ]
    new_ipn = (
        body.internal_partner_note
        if body.internal_partner_note is not None
        else src["internal_partner_note"]
    )

    edits_log = list(src["edits_log"] or [])
    edits_log.append(
        {
            "at": "NOW",  # replaced server-side by NOW(); see JSONB write below
            "user_id": ctx.claims.user_id,
            "section_num": body.section_num,
            "before_length": len(before_body),
            "after_length": len(body.body_html),
            "type": "section_edit",
        }
    )

    next_version = (
        await ctx.session.execute(
            text(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM drafts "
                "WHERE matter_id = :mid"
            ),
            {"mid": str(src["matter_id"])},
        )
    ).scalar_one()

    new_id = uuid4()
    await ctx.session.execute(
        text(
            f"""
            INSERT INTO drafts (
                draft_id, tenant_id, matter_id, version, status,
                model_used, prompt_version, tone, sections, citation_summary,
                paragraph_to_source_map, internal_partner_note, edits_log,
                content, generated_at, generated_by_user_id{opt_select}
            ) VALUES (
                :did, :tid, :mid, :ver, 'draft',
                :model, :pv, :tone,
                CAST(:sections AS JSONB),
                CAST(:csum AS JSONB),
                CAST(:pmap AS JSONB),
                :ipn, CAST(:elog AS JSONB),
                CAST(:sections AS JSONB), NOW(), :uid{opt_values}
            )
            """
        ),
        {
            # Carry the notice link + format profile onto the edited version.
            **{c: (str(src[c]) if src[c] is not None else None) for c in opt},
            "did": str(new_id),
            "tid": ctx.claims.tenant_id,
            "mid": str(src["matter_id"]),
            "ver": next_version,
            "model": src["model_used"],
            "pv": src["prompt_version"],
            "tone": src["tone"],
            "sections": json.dumps(new_sections),
            "csum": json.dumps(src["citation_summary"] or {}),
            "pmap": json.dumps(src["paragraph_to_source_map"] or []),
            "ipn": new_ipn,
            "elog": json.dumps(edits_log),
            "uid": ctx.claims.user_id,
        },
    )

    # Copy the citation rows over so the new version doesn't lose them.
    await ctx.session.execute(
        text(
            """
            INSERT INTO citations (
                tenant_id, draft_id, case_name, citation_string,
                paragraph_referenced, proposition_for_which_cited, status,
                source_url, verification_tier, verified_paragraph_text,
                proposition_match_confidence, verified_at, action_taken
            )
            SELECT tenant_id, :new_id, case_name, citation_string,
                   paragraph_referenced, proposition_for_which_cited, status,
                   source_url, verification_tier, verified_paragraph_text,
                   proposition_match_confidence, verified_at, action_taken
            FROM citations WHERE draft_id = :src_id
            """
        ),
        {"new_id": str(new_id), "src_id": str(draft_id)},
    )

    await audit.emit(
        ctx.session,
        tenant_id=ctx.claims.tenant_id,
        user_id=ctx.claims.user_id,
        action_type="draft.section_edited",
        entity_type="drafts",
        entity_id=new_id,
        before_state={
            "source_draft_id": str(draft_id),
            "section_num": body.section_num,
            "before_length": len(before_body),
        },
        after_state={
            "new_draft_id": str(new_id),
            "version": next_version,
            "after_length": len(body.body_html),
        },
        risk_tier=1,
    )

    await ctx.session.commit()
    return {
        "draft_id": str(new_id),
        "version": next_version,
        "source_draft_id": str(draft_id),
        "changed": True,
    }


ExportModeQ = Annotated[
    Literal["filing", "client", "internal"],
    Query(description="filing excludes Sec 13+14; client excludes Sec 13; internal includes all"),
]


@router.get("/drafts/{draft_id}/export")
async def export_draft(
    ctx: CurrentContext, draft_id: UUID, mode: ExportModeQ = "filing"
) -> Response:
    """Return the draft rendered as .docx (Times New Roman A4, no hyperlinks).

    Audit-logs draft.exported with mode + version so partners can see who
    pulled which version for filing.
    """
    # The cover sheet must describe the notice THIS draft answers. Earlier
    # code took the matter's earliest-due open notice, so on a matter with
    # both a s.142(1) and a s.143(2) notice the s.143(2) reply was exported
    # with the s.142(1) type + due date. Prefer drafts.notice_id, then the
    # notice id the workflow records in paragraph_to_source_map (covers a DB
    # without migration 0023 and drafts made before it), and only then the
    # old heuristic.
    opt = await drafts_optional_columns(ctx.session)
    source_map_notice = (
        "(SELECT (e->'source'->>'notice_id')::uuid "
        "FROM jsonb_array_elements(CASE WHEN jsonb_typeof(d.paragraph_to_source_map) = 'array' "
        "THEN d.paragraph_to_source_map ELSE '[]'::jsonb END) e "
        "WHERE e->'source'->>'notice_id' "
        "~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' "
        "LIMIT 1)"
    )
    draft_notice = (
        f"COALESCE(d.notice_id, {source_map_notice})"
        if "notice_id" in opt
        else source_map_notice
    )
    notice_match = (
        "(dn.notice_id IS NOT NULL AND nn.notice_id = dn.notice_id) OR "
        "(dn.notice_id IS NULL AND nn.matter_id = m.matter_id "
        "AND nn.lifecycle_status <> 'closed')"
    )
    row = (
        await ctx.session.execute(
            text(
                f"""
                SELECT d.draft_id, d.version, d.sections, d.internal_partner_note,
                       d.matter_id,
                       c.legal_name AS client_legal_name, c.pan AS client_pan,
                       r.registration_type, r.identifier_value, r.state_name,
                       n.document_type, n.authority, n.due_date,
                       COALESCE(n.financial_year, m.financial_year) AS financial_year,
                       COALESCE(n.assessment_year, m.assessment_year) AS assessment_year,
                       t.legal_name AS firm_name
                FROM drafts d
                JOIN matters m ON m.matter_id = d.matter_id
                JOIN clients c ON c.client_id = m.client_id
                JOIN client_registrations r ON r.registration_id = m.registration_id
                JOIN tenants t ON t.tenant_id = d.tenant_id
                CROSS JOIN LATERAL (SELECT {draft_notice} AS notice_id) dn
                LEFT JOIN LATERAL (
                    SELECT nn.document_type, nn.authority, nn.due_date,
                           nn.financial_year, nn.assessment_year
                    FROM notices nn
                    WHERE {notice_match}
                    ORDER BY nn.due_date ASC NULLS LAST
                    LIMIT 1
                ) n ON TRUE
                WHERE d.draft_id = :did
                """
            ),
            {"did": str(draft_id)},
        )
    ).mappings().first()
    if row is None:
        raise NotFoundError("draft not found")

    reg_label = "GSTIN" if row["registration_type"] == "GST" else "Registration"
    reg_id = row["identifier_value"]
    if row["registration_type"] == "GST" and row["state_name"]:
        reg_id = f"{row['identifier_value']} · {row['state_name']}"

    period = (
        f"FY {row['financial_year']}"
        if row["financial_year"]
        else (f"AY {row['assessment_year']}" if row["assessment_year"] else "—")
    )
    # Demo override: production tenants get their real Clerk-mapped name,
    # but for partner-facing pitches the operator can swap in a real firm
    # name via env without seeding a new tenant. Falls through to the DB
    # value when not set.
    firm_name = (
        get_settings().demo_firm_name_override
        or row["firm_name"]
    )
    cover = CoverSheetData(
        firm_name=firm_name,
        client_legal_name=row["client_legal_name"],
        client_pan=row["client_pan"],
        registration_label=reg_label,
        registration_identifier=reg_id,
        notice_type=row["document_type"],
        fy_or_ay=period,
        authority=row["authority"],
        due_date=row["due_date"].isoformat() if row["due_date"] else None,
    )

    blob = render_draft_docx(
        mode=mode,
        cover=cover,
        sections=row["sections"] or [],
        internal_partner_note=row["internal_partner_note"],
    )

    await audit.emit(
        ctx.session,
        tenant_id=ctx.claims.tenant_id,
        user_id=ctx.claims.user_id,
        action_type="draft.exported",
        entity_type="drafts",
        entity_id=draft_id,
        after_state={"mode": mode, "version": row["version"], "bytes": len(blob)},
        risk_tier=1,
    )
    await ctx.session.commit()

    safe_client = (
        "".join(ch if ch.isalnum() else "_" for ch in row["client_legal_name"])[:40]
        or "client"
    )
    filename = (
        f"{safe_client}_v{row['version']}_{mode}.docx"
    )
    return Response(
        content=blob,
        media_type=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
