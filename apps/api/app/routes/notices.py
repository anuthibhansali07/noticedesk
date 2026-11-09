"""Notice list endpoints + dashboard aggregates + matter view + lifecycle.

The dashboard surface needs three reads:

- GET /v1/notices?status=&law=&client_id=&registration_id=&from=&to=&page=&page_size=
    Filtered + paginated list, sorted by due_date ASC. Joins clients and
    client_registrations so the row can render the full identity context
    without N+1 follow-ups.
- GET /v1/dashboard/status_counts
    Single row of counts keyed by lifecycle_status. Drives the 8 status
    tabs at the top of the dashboard.
- GET /v1/dashboard/today
    Notices due in the next 7 days inclusive. Brief calls this out
    explicitly even though the prototype doesn't bind it to UI yet.

Slice 6 adds the matter-view surface:

- POST   /v1/notices                  — manual notice creation (partner enters
                                         a notice without uploading a PDF).
- GET    /v1/notices/{id}             — full matter context for the page.
- PATCH  /v1/notices/{id}             — partial field edit, audit-logged.
- PATCH  /v1/notices/{id}/lifecycle   — manual lifecycle transition. Reason
                                         required for closed / on_hold /
                                         reply_submitted per the brief.
- GET    /v1/notices/{id}/timeline    — audit-log events for the Timeline tab.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.core.errors import NotFoundError, ValidationError
from app.middleware.tenant_context import CurrentContext
from app.services import audit

# Annotated query-param aliases — fixes ruff B008 (no Query() in arg defaults).
StatusFilter = Annotated[str | None, Query(pattern="^[a-z_]+$")]
LawFilter = Annotated[Literal["GST", "IT"] | None, Query()]
ClientFilter = Annotated[UUID | None, Query()]
RegistrationFilter = Annotated[UUID | None, Query()]
StateCodeFilter = Annotated[str | None, Query(pattern=r"^[0-9]{2}$")]
FromDateFilter = Annotated[date | None, Query(alias="from")]
ToDateFilter = Annotated[date | None, Query(alias="to")]
PageNumber = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=200)]

router = APIRouter()


_LIFECYCLE_KEYS = (
    "issued",
    "in_progress",
    "due",
    "due_date_over",
    "reply_submitted",
    "acknowledged",
    "order_received",
    "appeal_filed",
    "closed",
    "on_hold",
)


@router.get("/notices")
async def list_notices(
    ctx: CurrentContext,
    status: StatusFilter = None,
    law: LawFilter = None,
    client_id: ClientFilter = None,
    registration_id: RegistrationFilter = None,
    state_code: StateCodeFilter = None,
    from_date: FromDateFilter = None,
    to_date: ToDateFilter = None,
    page: PageNumber = 1,
    page_size: PageSize = 50,
) -> dict[str, Any]:
    """List notices matching the filters, joined with client + registration."""
    where: list[str] = ["c.deleted_at IS NULL"]
    params: dict[str, Any] = {}
    if status is not None:
        where.append("n.lifecycle_status = :status")
        params["status"] = status
    if law is not None:
        where.append("n.law = :law")
        params["law"] = law
    if client_id is not None:
        where.append("n.client_id = :cid")
        params["cid"] = str(client_id)
    if registration_id is not None:
        where.append("n.registration_id = :rid")
        params["rid"] = str(registration_id)
    if state_code is not None:
        where.append("r.state_code = :sc")
        params["sc"] = state_code
    if from_date is not None:
        where.append("n.due_date >= :from_date")
        params["from_date"] = from_date
    if to_date is not None:
        where.append("n.due_date <= :to_date")
        params["to_date"] = to_date

    where_clause = " AND ".join(where) if where else "TRUE"
    params["limit"] = page_size
    params["offset"] = (page - 1) * page_size

    rows = (
        await ctx.session.execute(
            text(
                f"""
                SELECT
                    n.notice_id, n.law, n.document_type, n.due_date,
                    n.financial_year, n.assessment_year, n.lifecycle_status,
                    n.ingest_channel, n.din_or_rfn, n.raw_extracted_json,
                    c.client_id, c.legal_name AS client_legal_name, c.pan AS client_pan,
                    r.registration_id, r.registration_type,
                    r.identifier_value AS registration_identifier,
                    r.state_code  AS registration_state_code,
                    r.state_name  AS registration_state_name
                FROM notices n
                JOIN clients c ON c.client_id = n.client_id
                JOIN client_registrations r ON r.registration_id = n.registration_id
                WHERE {where_clause}
                ORDER BY n.due_date ASC NULLS LAST, n.notice_id ASC
                LIMIT :limit OFFSET :offset
                """
            ),
            params,
        )
    ).mappings().all()

    total_row = (
        await ctx.session.execute(
            text(
                f"""
                SELECT COUNT(*)::int AS total
                FROM notices n
                JOIN clients c ON c.client_id = n.client_id
                JOIN client_registrations r ON r.registration_id = n.registration_id
                WHERE {where_clause}
                """
            ),
            {k: v for k, v in params.items() if k not in ("limit", "offset")},
        )
    ).mappings().first()

    return {
        "notices": [
            {
                "notice_id": str(r["notice_id"]),
                "law": r["law"],
                "document_type": r["document_type"],
                "due_date": r["due_date"].isoformat() if r["due_date"] else None,
                "financial_year": r["financial_year"],
                "assessment_year": r["assessment_year"],
                "lifecycle_status": r["lifecycle_status"],
                "ingest_channel": r["ingest_channel"],
                "din_or_rfn": r["din_or_rfn"],
                "issue": (
                    (r["raw_extracted_json"] or {}).get("issue")
                    if isinstance(r["raw_extracted_json"], dict)
                    else None
                ),
                "assigned_to": (
                    (r["raw_extracted_json"] or {}).get("assigned_to")
                    if isinstance(r["raw_extracted_json"], dict)
                    else None
                ),
                "client_id": str(r["client_id"]),
                "client_legal_name": r["client_legal_name"],
                "client_pan": r["client_pan"],
                "registration_id": str(r["registration_id"]),
                "registration_type": r["registration_type"],
                "registration_identifier": r["registration_identifier"],
                "registration_state_code": r["registration_state_code"],
                "registration_state_name": r["registration_state_name"],
            }
            for r in rows
        ],
        "total": total_row["total"] if total_row else 0,
        "page": page,
        "page_size": page_size,
    }


@router.get("/dashboard/status_counts")
async def dashboard_status_counts(ctx: CurrentContext) -> dict[str, int]:
    """Return counts of notices per lifecycle_status. All 10 keys are
    always present (zero-filled) so the UI doesn't have to defend itself.
    """
    rows = (
        await ctx.session.execute(
            text(
                """
                SELECT n.lifecycle_status, COUNT(*)::int AS count
                FROM notices n
                JOIN clients c ON c.client_id = n.client_id
                WHERE c.deleted_at IS NULL
                GROUP BY n.lifecycle_status
                """
            )
        )
    ).all()
    counts: dict[str, int] = {k: 0 for k in _LIFECYCLE_KEYS}
    for row in rows:
        counts[row[0]] = row[1]
    return counts


@router.get("/dashboard/today")
async def dashboard_today(ctx: CurrentContext) -> dict[str, Any]:
    """Notices due in the next 7 days inclusive of today, plus anything
    already past due that is still open (issued / in_progress / due /
    due_date_over). Used by partners' morning standup view.
    """
    today = date.today()
    horizon = today + timedelta(days=7)
    rows = (
        await ctx.session.execute(
            text(
                """
                SELECT
                    n.notice_id, n.law, n.document_type, n.due_date,
                    n.lifecycle_status, n.raw_extracted_json,
                    c.client_id, c.legal_name AS client_legal_name,
                    r.identifier_value AS reg_identifier
                FROM notices n
                JOIN clients c ON c.client_id = n.client_id
                JOIN client_registrations r ON r.registration_id = n.registration_id
                WHERE c.deleted_at IS NULL
                  AND n.lifecycle_status IN ('issued','in_progress','due','due_date_over')
                  AND n.due_date IS NOT NULL
                  AND n.due_date <= :horizon
                ORDER BY n.due_date ASC
                """
            ),
            {"horizon": horizon},
        )
    ).mappings().all()

    return {
        "today": today.isoformat(),
        "horizon": horizon.isoformat(),
        "notices": [
            {
                "notice_id": str(r["notice_id"]),
                "law": r["law"],
                "document_type": r["document_type"],
                "due_date": r["due_date"].isoformat(),
                "lifecycle_status": r["lifecycle_status"],
                "client_id": str(r["client_id"]),
                "client_legal_name": r["client_legal_name"],
                "registration_identifier": r["reg_identifier"],
                "issue": (
                    (r["raw_extracted_json"] or {}).get("issue")
                    if isinstance(r["raw_extracted_json"], dict)
                    else None
                ),
            }
            for r in rows
        ],
        "total": len(rows),
    }


# ============================================================================
# Slice 6 — matter view + manual notice creation + lifecycle transitions
# ============================================================================


# Lifecycle transitions that warrant a partner-supplied reason. The brief
# calls out three explicitly; anything else can transition silently.
_REASON_REQUIRED: frozenset[str] = frozenset({"closed", "on_hold", "reply_submitted"})

# Tighter risk tiers for the audit log: closing a matter is more destructive
# than moving it forward.
_LIFECYCLE_RISK: dict[str, int] = {
    "closed": 2,
    "on_hold": 2,
}

_ALL_LIFECYCLE: frozenset[str] = frozenset(_LIFECYCLE_KEYS)


class CreateNoticeRequest(BaseModel):
    """Manual notice creation — partner enters a notice without uploading a PDF.

    The matter is auto-found-or-created by (client_id, registration_id, law,
    fy/ay) so partners don't have to think about matter rows. ingest_channel
    defaults to 'web_upload' (the closest existing CHECK value for "typed by
    hand"); we'll add a 'manual_entry' enum in a later migration if the
    distinction matters for reporting.
    """

    client_id: UUID
    registration_id: UUID
    law: Literal["GST", "IT"]
    document_type: str = Field(..., min_length=1, max_length=64)
    due_date: date | None = None
    issue_date: date | None = None
    hearing_date: date | None = None
    financial_year: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}$")
    assessment_year: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}$")
    authority: str | None = None
    din_or_rfn: str | None = None
    notice_number: str | None = None
    issue: str | None = None
    assigned_to: str | None = None
    ingest_channel: str = Field(default="web_upload")


@router.post("/notices", status_code=201)
async def create_notice(ctx: CurrentContext, body: CreateNoticeRequest) -> dict[str, Any]:
    # Verify the client + registration belong to this tenant (RLS will hide
    # mismatches; we surface a 404 instead of silently failing the insert).
    reg = (
        await ctx.session.execute(
            text(
                "SELECT client_id, registration_type FROM client_registrations "
                "WHERE registration_id = :rid"
            ),
            {"rid": str(body.registration_id)},
        )
    ).mappings().first()
    if reg is None:
        raise NotFoundError("registration not found")
    if reg["client_id"] != body.client_id:
        raise ValidationError("registration does not belong to the given client")
    if reg["registration_type"] != body.law:
        raise ValidationError(
            f"law '{body.law}' does not match registration_type '{reg['registration_type']}'"
        )

    # Find or create the matter for this (client, reg, fy/ay).
    period_field = "financial_year" if body.law == "GST" else "assessment_year"
    period_value = body.financial_year if body.law == "GST" else body.assessment_year
    matter_row = (
        await ctx.session.execute(
            text(
                f"""
                SELECT matter_id FROM matters
                WHERE client_id = :cid
                  AND registration_id = :rid
                  AND law = :law
                  AND {period_field} IS NOT DISTINCT FROM :period
                LIMIT 1
                """
            ),
            {
                "cid": str(body.client_id),
                "rid": str(body.registration_id),
                "law": body.law,
                "period": period_value,
            },
        )
    ).first()
    if matter_row is None:
        matter_id = (
            await ctx.session.execute(
                text(
                    f"""
                    INSERT INTO matters (
                        tenant_id, client_id, registration_id, law, {period_field}
                    ) VALUES (
                        :tid, :cid, :rid, :law, :period
                    ) RETURNING matter_id
                    """
                ),
                {
                    "tid": ctx.claims.tenant_id,
                    "cid": str(body.client_id),
                    "rid": str(body.registration_id),
                    "law": body.law,
                    "period": period_value,
                },
            )
        ).scalar_one()
    else:
        matter_id = matter_row[0]

    raw_json = {
        "issue": body.issue,
        "assigned_to": body.assigned_to,
        "source": "manual_entry",
    }
    notice_id = (
        await ctx.session.execute(
            text(
                """
                INSERT INTO notices (
                    tenant_id, matter_id, client_id, registration_id, law,
                    document_type, notice_number, din_or_rfn,
                    issue_date, due_date, hearing_date, authority,
                    financial_year, assessment_year,
                    lifecycle_status, ingest_channel, raw_extracted_json
                ) VALUES (
                    :tid, :mid, :cid, :rid, :law,
                    :dtype, :nnum, :din,
                    :idate, :ddate, :hdate, :auth,
                    :fy, :ay,
                    'issued', :ichan, CAST(:raw AS JSONB)
                ) RETURNING notice_id
                """
            ),
            {
                "tid": ctx.claims.tenant_id,
                "mid": str(matter_id),
                "cid": str(body.client_id),
                "rid": str(body.registration_id),
                "law": body.law,
                "dtype": body.document_type,
                "nnum": body.notice_number,
                "din": body.din_or_rfn,
                "idate": body.issue_date,
                "ddate": body.due_date,
                "hdate": body.hearing_date,
                "auth": body.authority,
                "fy": body.financial_year,
                "ay": body.assessment_year,
                "ichan": body.ingest_channel,
                "raw": json.dumps(raw_json),
            },
        )
    ).scalar_one()

    await audit.emit(
        ctx.session,
        tenant_id=ctx.claims.tenant_id,
        user_id=ctx.claims.user_id,
        action_type="notice.created",
        entity_type="notices",
        entity_id=notice_id,
        after_state={
            "document_type": body.document_type,
            "law": body.law,
            "due_date": body.due_date.isoformat() if body.due_date else None,
            "source": "manual_entry",
        },
        risk_tier=1,
    )
    await ctx.session.commit()
    return {
        "notice_id": str(notice_id),
        "matter_id": str(matter_id),
    }


@router.get("/notices/{notice_id}")
async def get_notice(ctx: CurrentContext, notice_id: UUID) -> dict[str, Any]:
    """Full matter-view payload: notice + client + registration + matter."""
    row = (
        await ctx.session.execute(
            text(
                """
                SELECT
                    n.notice_id, n.matter_id, n.law, n.document_type,
                    n.notice_number, n.din_or_rfn,
                    n.issue_date, n.due_date, n.receipt_date, n.hearing_date,
                    n.authority, n.financial_year, n.assessment_year,
                    n.issues, n.documents_required,
                    n.lifecycle_status, n.ingest_channel, n.demand_amount,
                    n.source_inbox_id, n.pan_gstin_reconciliation_status,
                    n.parse_confidence, n.verification_status,
                    n.raw_extracted_json, n.manual_corrections_json,
                    n.created_at, n.updated_at,
                    c.client_id, c.legal_name AS client_legal_name,
                    c.pan AS client_pan, c.entity_type AS client_entity_type,
                    c.industry AS client_industry,
                    r.registration_id, r.registration_type,
                    r.identifier_value AS registration_identifier,
                    r.state_code  AS registration_state_code,
                    r.state_name  AS registration_state_name,
                    r.jurisdiction_office AS registration_jurisdiction
                FROM notices n
                JOIN clients c ON c.client_id = n.client_id
                JOIN client_registrations r ON r.registration_id = n.registration_id
                WHERE n.notice_id = :nid AND c.deleted_at IS NULL
                """
            ),
            {"nid": str(notice_id)},
        )
    ).mappings().first()
    if row is None:
        raise NotFoundError("notice not found")

    raw = row["raw_extracted_json"] if isinstance(row["raw_extracted_json"], dict) else {}

    return {
        "notice": {
            "notice_id": str(row["notice_id"]),
            "matter_id": str(row["matter_id"]),
            "law": row["law"],
            "document_type": row["document_type"],
            "notice_number": row["notice_number"],
            "din_or_rfn": row["din_or_rfn"],
            "issue_date": row["issue_date"].isoformat() if row["issue_date"] else None,
            "receipt_date": row["receipt_date"].isoformat() if row["receipt_date"] else None,
            "due_date": row["due_date"].isoformat() if row["due_date"] else None,
            "hearing_date": row["hearing_date"].isoformat() if row["hearing_date"] else None,
            "authority": row["authority"],
            "financial_year": row["financial_year"],
            "assessment_year": row["assessment_year"],
            "issues": row["issues"],
            "documents_required": row["documents_required"],
            "lifecycle_status": row["lifecycle_status"],
            "ingest_channel": row["ingest_channel"],
            "demand_amount": (
                float(row["demand_amount"]) if row["demand_amount"] is not None else None
            ),
            "source_inbox_id": (
                str(row["source_inbox_id"]) if row["source_inbox_id"] else None
            ),
            "pan_gstin_reconciliation_status": row["pan_gstin_reconciliation_status"],
            "parse_confidence": (
                float(row["parse_confidence"]) if row["parse_confidence"] is not None else None
            ),
            "verification_status": row["verification_status"],
            # Surface issue + assigned_to flat for the UI (they live in raw_json
            # for the seed-generated notices; manual entries put them there too).
            "issue": raw.get("issue"),
            "assigned_to": raw.get("assigned_to"),
            "raw_extracted_json": row["raw_extracted_json"],
            "manual_corrections_json": row["manual_corrections_json"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        },
        "client": {
            "client_id": str(row["client_id"]),
            "legal_name": row["client_legal_name"],
            "pan": row["client_pan"],
            "entity_type": row["client_entity_type"],
            "industry": row["client_industry"],
        },
        "registration": {
            "registration_id": str(row["registration_id"]),
            "registration_type": row["registration_type"],
            "identifier_value": row["registration_identifier"],
            "state_code": row["registration_state_code"],
            "state_name": row["registration_state_name"],
            "jurisdiction_office": row["registration_jurisdiction"],
        },
    }


class UpdateNoticeRequest(BaseModel):
    document_type: str | None = None
    notice_number: str | None = None
    din_or_rfn: str | None = None
    due_date: date | None = None
    issue_date: date | None = None
    hearing_date: date | None = None
    authority: str | None = None
    demand_amount: float | None = None


@router.patch("/notices/{notice_id}")
async def update_notice(
    ctx: CurrentContext, notice_id: UUID, body: UpdateNoticeRequest
) -> dict[str, Any]:
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise ValidationError("no fields to update")

    before = (
        await ctx.session.execute(
            text(
                "SELECT " + ", ".join(fields) + " FROM notices WHERE notice_id = :nid"
            ),
            {"nid": str(notice_id)},
        )
    ).mappings().first()
    if before is None:
        raise NotFoundError("notice not found")

    set_clauses = ", ".join(f"{k} = :{k}" for k in fields)
    params: dict[str, Any] = {"nid": str(notice_id), **fields}
    await ctx.session.execute(
        text(
            f"UPDATE notices SET {set_clauses}, updated_at = NOW() WHERE notice_id = :nid"
        ),
        params,
    )

    await audit.emit(
        ctx.session,
        tenant_id=ctx.claims.tenant_id,
        user_id=ctx.claims.user_id,
        action_type="notice.updated",
        entity_type="notices",
        entity_id=notice_id,
        before_state={
            k: (before[k].isoformat() if hasattr(before[k], "isoformat") else before[k])
            for k in fields
        },
        after_state={
            k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in fields.items()
        },
        risk_tier=1,
    )
    await ctx.session.commit()
    return {"notice_id": str(notice_id), "updated_fields": list(fields)}


class LifecycleTransitionRequest(BaseModel):
    lifecycle_status: str
    reason: str | None = Field(default=None, max_length=2000)


@router.patch("/notices/{notice_id}/lifecycle")
async def transition_lifecycle(
    ctx: CurrentContext, notice_id: UUID, body: LifecycleTransitionRequest
) -> dict[str, Any]:
    """Move a notice to a new lifecycle state. Phase 1 has no automatic
    transitions — the partner drives every move manually. We validate the
    target state and require a reason for the three "terminal-ish" moves.
    """
    if body.lifecycle_status not in _ALL_LIFECYCLE:
        raise ValidationError(
            f"invalid lifecycle_status '{body.lifecycle_status}'",
            details={"allowed": sorted(_ALL_LIFECYCLE)},
        )
    if body.lifecycle_status in _REASON_REQUIRED and not (body.reason and body.reason.strip()):
        raise ValidationError(
            f"reason is required when transitioning to '{body.lifecycle_status}'"
        )

    before = (
        await ctx.session.execute(
            text("SELECT lifecycle_status FROM notices WHERE notice_id = :nid"),
            {"nid": str(notice_id)},
        )
    ).first()
    if before is None:
        raise NotFoundError("notice not found")

    previous = before[0]
    if previous == body.lifecycle_status:
        # No-op — return success without an audit row so re-clicks don't pollute
        # the timeline.
        return {"notice_id": str(notice_id), "lifecycle_status": previous, "changed": False}

    await ctx.session.execute(
        text(
            "UPDATE notices SET lifecycle_status = :s, updated_at = NOW() "
            "WHERE notice_id = :nid"
        ),
        {"s": body.lifecycle_status, "nid": str(notice_id)},
    )

    await audit.emit(
        ctx.session,
        tenant_id=ctx.claims.tenant_id,
        user_id=ctx.claims.user_id,
        action_type="notice.lifecycle_changed",
        entity_type="notices",
        entity_id=notice_id,
        before_state={"lifecycle_status": previous},
        after_state={
            "lifecycle_status": body.lifecycle_status,
            "reason": body.reason,
        },
        risk_tier=_LIFECYCLE_RISK.get(body.lifecycle_status, 1),
    )
    await ctx.session.commit()
    return {
        "notice_id": str(notice_id),
        "lifecycle_status": body.lifecycle_status,
        "changed": True,
    }


@router.get("/notices/{notice_id}/timeline")
async def notice_timeline(ctx: CurrentContext, notice_id: UUID) -> dict[str, Any]:
    """Audit-log events for this notice, newest first. Joins the user so the
    UI can render 'Rohan Mehta moved this to Reply Submitted' instead of a
    bare UUID.
    """
    rows = (
        await ctx.session.execute(
            text(
                """
                SELECT a.log_id AS audit_id, a.timestamp, a.action_type,
                       a.before_state, a.after_state, a.risk_tier,
                       u.name AS user_name, u.role AS user_role
                FROM audit_logs a
                LEFT JOIN users u ON u.user_id = a.user_id
                WHERE a.entity_type = 'notices' AND a.entity_id = :nid
                ORDER BY a.timestamp DESC
                """
            ),
            {"nid": str(notice_id)},
        )
    ).mappings().all()

    return {
        "events": [
            {
                "audit_id": str(r["audit_id"]),
                "timestamp": r["timestamp"].isoformat() if r["timestamp"] else None,
                "action_type": r["action_type"],
                "before_state": r["before_state"],
                "after_state": r["after_state"],
                "risk_tier": r["risk_tier"],
                "user_name": r["user_name"],
                "user_role": r["user_role"],
            }
            for r in rows
        ],
        "total": len(rows),
    }
