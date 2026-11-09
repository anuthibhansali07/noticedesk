"""Triage endpoints.

The new flow inserts a triage stage between parsing and drafting:

- POST /v1/notices/{notice_id}/triage                    — run the triage agent
- GET  /v1/notices/{notice_id}/triage                    — fetch summary + checklist
- POST /v1/notices/{notice_id}/checklist/{rid}/attach    — link a document to a checklist item
- POST /v1/notices/{notice_id}/checklist/{rid}/mark-na   — partner marks item not-applicable

Triage is manually triggered by the partner — there is no auto-fire after
parse. The status flow is generating → ready_to_gather → ready_to_draft →
drafted, but the API never blocks drafting on the status (soft block policy):
the UI surfaces a banner when required items are unresolved, the partner can
still hit Generate Draft if they want a first-cut.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.agents.notice_triage import generate_triage, load_triage_input
from app.core.errors import NotFoundError, llm_error_to_app_error
from app.core.logging import get_logger
from app.middleware.tenant_context import CurrentContext
from app.services import audit
from app.services.llm import LLMError
from app.workflows.email_sends import send_checklist_to_client

logger = get_logger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# POST /v1/notices/{notice_id}/triage
# ---------------------------------------------------------------------------


@router.post("/notices/{notice_id}/triage", status_code=201)
async def run_triage(ctx: CurrentContext, notice_id: UUID) -> dict[str, Any]:
    """Run the triage agent for a notice. Idempotent: re-running replaces the
    summary + replaces any *unresolved* checklist items, but keeps items the
    partner has already attached a document to or marked N/A on so they
    don't lose their work."""

    # 1. Confirm the notice exists in this tenant + grab its matter for the
    #    audit log and the response payload.
    notice_row = (
        await ctx.session.execute(
            text("SELECT notice_id, matter_id FROM notices WHERE notice_id = :nid"),
            {"nid": str(notice_id)},
        )
    ).first()
    if notice_row is None:
        raise NotFoundError("notice not found")

    # 2. Load context + run agent.
    ti = await load_triage_input(ctx.session, notice_id=notice_id)

    try:
        triage = await generate_triage(ti)
    except LLMError as e:
        logger.exception("triage generation failed: %s", e)
        raise llm_error_to_app_error(e, action="triage") from e

    # 3. Persist. We keep the previous summary's resolved checklist items so
    #    a re-run doesn't wipe partner work.
    await ctx.session.execute(
        text(
            """
            INSERT INTO notice_triage (
                notice_id, tenant_id, summary, status,
                prompt_version, model, provider_name,
                input_tokens, output_tokens, generated_at
            ) VALUES (
                :nid, :tid, :summary, 'ready_to_gather',
                :prompt_version, :model, :provider,
                :in_tokens, :out_tokens, NOW()
            )
            ON CONFLICT (notice_id) DO UPDATE SET
                summary        = EXCLUDED.summary,
                status         = 'ready_to_gather',
                prompt_version = EXCLUDED.prompt_version,
                model          = EXCLUDED.model,
                provider_name  = EXCLUDED.provider_name,
                input_tokens   = EXCLUDED.input_tokens,
                output_tokens  = EXCLUDED.output_tokens,
                generated_at   = NOW()
            """
        ),
        {
            "nid": str(notice_id),
            "tid": ctx.claims.tenant_id,
            "summary": triage.summary,
            "prompt_version": triage.prompt_version,
            "model": triage.model,
            "provider": triage.provider_name,
            "in_tokens": triage.input_tokens,
            "out_tokens": triage.output_tokens,
        },
    )

    # 4. Replace unresolved checklist items, preserve resolved ones.
    await ctx.session.execute(
        text(
            """
            DELETE FROM notice_document_requirements
            WHERE notice_id = :nid AND status = 'pending'
            """
        ),
        {"nid": str(notice_id)},
    )
    for item in triage.checklist:
        await ctx.session.execute(
            text(
                """
                INSERT INTO notice_document_requirements (
                    notice_id, tenant_id, label, rationale, doc_type,
                    is_required, status, position
                ) VALUES (
                    :nid, :tid, :label, :rationale, :doc_type,
                    :required, 'pending', :pos
                )
                """
            ),
            {
                "nid": str(notice_id),
                "tid": ctx.claims.tenant_id,
                "label": item.label,
                "rationale": item.rationale,
                "doc_type": item.doc_type,
                "required": item.is_required,
                "pos": item.position,
            },
        )

    await audit.emit(
        ctx.session,
        tenant_id=ctx.claims.tenant_id,
        user_id=ctx.claims.user_id,
        action_type="triage.generated",
        entity_type="notice",
        entity_id=notice_id,
        after_state={
            "checklist_items": len(triage.checklist),
            "prompt_version": triage.prompt_version,
            "model": triage.model,
        },
    )
    await ctx.session.commit()

    # Fire the document-checklist email to the client. Best-effort: a
    # missing client email or SMTP error must NOT block the triage response.
    email_status = await send_checklist_to_client(
        ctx.session,
        tenant_id=ctx.claims.tenant_id,
        user_id=ctx.claims.user_id,
        notice_id=notice_id,
    )
    await ctx.session.commit()

    payload = await _fetch_triage_payload(ctx, notice_id)
    payload["client_email_status"] = email_status
    return payload


# ---------------------------------------------------------------------------
# GET /v1/notices/{notice_id}/triage
# ---------------------------------------------------------------------------


@router.get("/notices/{notice_id}/triage")
async def get_triage(ctx: CurrentContext, notice_id: UUID) -> dict[str, Any]:
    return await _fetch_triage_payload(ctx, notice_id)


# ---------------------------------------------------------------------------
# POST /v1/notices/{notice_id}/checklist/{requirement_id}/attach
# ---------------------------------------------------------------------------


class AttachDocumentRequest(BaseModel):
    document_id: UUID


@router.post("/notices/{notice_id}/checklist/{requirement_id}/attach")
async def attach_document_to_requirement(
    ctx: CurrentContext,
    notice_id: UUID,
    requirement_id: UUID,
    body: AttachDocumentRequest,
) -> dict[str, Any]:
    # Confirm the requirement belongs to this notice (and tenant via RLS).
    req = (
        await ctx.session.execute(
            text(
                """
                SELECT requirement_id, notice_id, status
                FROM notice_document_requirements
                WHERE requirement_id = :rid AND notice_id = :nid
                """
            ),
            {"rid": str(requirement_id), "nid": str(notice_id)},
        )
    ).first()
    if req is None:
        raise NotFoundError("checklist item not found")

    # Confirm the document exists in this tenant's matters.
    doc = (
        await ctx.session.execute(
            text("SELECT document_id FROM documents WHERE document_id = :did"),
            {"did": str(body.document_id)},
        )
    ).first()
    if doc is None:
        raise NotFoundError("document not found")

    await ctx.session.execute(
        text(
            """
            UPDATE notice_document_requirements
            SET document_id = :did,
                status      = 'uploaded',
                updated_at  = NOW()
            WHERE requirement_id = :rid
            """
        ),
        {"did": str(body.document_id), "rid": str(requirement_id)},
    )

    await _refresh_triage_status(ctx, notice_id)
    await audit.emit(
        ctx.session,
        tenant_id=ctx.claims.tenant_id,
        user_id=ctx.claims.user_id,
        action_type="triage.requirement_attached",
        entity_type="notice_document_requirement",
        entity_id=requirement_id,
        after_state={"document_id": str(body.document_id)},
    )
    await ctx.session.commit()
    return await _fetch_triage_payload(ctx, notice_id)


# ---------------------------------------------------------------------------
# POST /v1/notices/{notice_id}/checklist/{requirement_id}/mark-na
# ---------------------------------------------------------------------------


class MarkNotApplicableRequest(BaseModel):
    reason: str = Field(default="", max_length=500)


@router.post("/notices/{notice_id}/checklist/{requirement_id}/mark-na")
async def mark_requirement_not_applicable(
    ctx: CurrentContext,
    notice_id: UUID,
    requirement_id: UUID,
    body: MarkNotApplicableRequest,
) -> dict[str, Any]:
    req = (
        await ctx.session.execute(
            text(
                """
                SELECT requirement_id FROM notice_document_requirements
                WHERE requirement_id = :rid AND notice_id = :nid
                """
            ),
            {"rid": str(requirement_id), "nid": str(notice_id)},
        )
    ).first()
    if req is None:
        raise NotFoundError("checklist item not found")

    await ctx.session.execute(
        text(
            """
            UPDATE notice_document_requirements
            SET status           = 'not_applicable',
                marked_na_reason = :reason,
                document_id      = NULL,
                updated_at       = NOW()
            WHERE requirement_id = :rid
            """
        ),
        {"reason": body.reason or None, "rid": str(requirement_id)},
    )

    await _refresh_triage_status(ctx, notice_id)
    await audit.emit(
        ctx.session,
        tenant_id=ctx.claims.tenant_id,
        user_id=ctx.claims.user_id,
        action_type="triage.requirement_marked_na",
        entity_type="notice_document_requirement",
        entity_id=requirement_id,
        after_state={"reason": body.reason or None},
    )
    await ctx.session.commit()
    return await _fetch_triage_payload(ctx, notice_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _fetch_triage_payload(
    ctx: CurrentContext, notice_id: UUID
) -> dict[str, Any]:
    await ctx.session.execute(
        text("SELECT set_config('app.current_tenant', :tid, true)"),
        {"tid": str(ctx.claims.tenant_id)},
    )
    triage_row = (
        await ctx.session.execute(
            text(
                """
                SELECT notice_id, summary, status, prompt_version,
                       model, provider_name, input_tokens, output_tokens,
                       generated_at
                FROM notice_triage
                WHERE notice_id = :nid
                """
            ),
            {"nid": str(notice_id)},
        )
    ).mappings().first()

    if triage_row is None:
        # No triage yet — return a "not started" shape so the UI can render
        # the "Begin triage" empty state without a 404.
        return {
            "notice_id": str(notice_id),
            "status": "not_started",
            "summary": None,
            "checklist": [],
            "generated_at": None,
            "prompt_version": None,
            "model": None,
            "provider_name": None,
        }

    item_rows = (
        await ctx.session.execute(
            text(
                """
                SELECT r.requirement_id, r.label, r.rationale, r.doc_type,
                       r.is_required, r.status, r.position,
                       r.marked_na_reason, r.document_id,
                       d.filename AS document_filename
                FROM notice_document_requirements r
                LEFT JOIN documents d ON d.document_id = r.document_id
                WHERE r.notice_id = :nid
                ORDER BY r.position ASC
                """
            ),
            {"nid": str(notice_id)},
        )
    ).mappings().all()

    return {
        "notice_id": str(triage_row["notice_id"]),
        "status": triage_row["status"],
        "summary": triage_row["summary"],
        "generated_at": (
            triage_row["generated_at"].isoformat()
            if triage_row["generated_at"]
            else None
        ),
        "prompt_version": triage_row["prompt_version"],
        "model": triage_row["model"],
        "provider_name": triage_row["provider_name"],
        "checklist": [
            {
                "requirement_id": str(r["requirement_id"]),
                "label": r["label"],
                "rationale": r["rationale"],
                "doc_type": r["doc_type"],
                "is_required": r["is_required"],
                "status": r["status"],
                "position": r["position"],
                "marked_na_reason": r["marked_na_reason"],
                "document_id": (
                    str(r["document_id"]) if r["document_id"] else None
                ),
                "document_filename": r["document_filename"],
            }
            for r in item_rows
        ],
    }


async def _refresh_triage_status(ctx: CurrentContext, notice_id: UUID) -> None:
    """Move triage to ready_to_draft when every required item is resolved.

    Advisory only — the API never blocks drafting on this status (soft block).
    """
    unresolved = (
        await ctx.session.execute(
            text(
                """
                SELECT COUNT(*) AS n
                FROM notice_document_requirements
                WHERE notice_id = :nid
                  AND is_required = TRUE
                  AND status = 'pending'
                """
            ),
            {"nid": str(notice_id)},
        )
    ).first()
    new_status = "ready_to_draft" if (unresolved and unresolved[0] == 0) else "ready_to_gather"
    await ctx.session.execute(
        text(
            """
            UPDATE notice_triage
            SET status = :s
            WHERE notice_id = :nid AND status <> 'drafted'
            """
        ),
        {"s": new_status, "nid": str(notice_id)},
    )


