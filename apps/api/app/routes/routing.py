"""Sprint 3 routing-recovery endpoints.

Surfaces the anomaly-resolution flows the inbox UI invokes:

- POST /v1/inbox/{inbox_id}/route_manually  — partner-chosen client + reg.
- POST /v1/inbox/{inbox_id}/reject           — toss a doc with a reason.
- POST /v1/clients                           — add a client (anomaly fix).
- POST /v1/clients/{client_id}/registrations — add a registration.
- GET  /v1/inbox/{inbox_id}/parsed           — view parsed JSON (debug + UI).
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, status
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.agents.notice_routing import _coerce_date, route_notice
from app.core.errors import (
    AppError,
    NotFoundError,
    PanGstinReconciliationError,
    ValidationError,
)
from app.middleware.tenant_context import CurrentContext
from app.models.routing import (
    AddClientRequest,
    AddRegistrationRequest,
    ManualRouteRequest,
    RejectInboxRequest,
    RoutingDecisionView,
)
from app.services import audit
from app.services.identity import (
    extract_pan_from_gstin,
    validate_gstin_format,
    validate_pan_format,
)

router = APIRouter()


# ----- View parsed JSON ------------------------------------------------------


@router.get("/inbox/{inbox_id}/parsed")
async def view_parsed(ctx: CurrentContext, inbox_id: UUID) -> dict[str, Any]:
    row = (
        await ctx.session.execute(
            text(
                """
                SELECT parse_status, routing_status, raw_parsed_json,
                       routing_anomaly_details, parsed_to_notice_id
                FROM documents_inbox WHERE inbox_id = :id
                """
            ),
            {"id": str(inbox_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError("inbox row not found")
    return {
        "inbox_id": str(inbox_id),
        "parse_status": row[0],
        "routing_status": row[1],
        "parsed": row[2],
        "anomaly_details": row[3],
        "parsed_to_notice_id": str(row[4]) if row[4] else None,
    }


# ----- Manual override -------------------------------------------------------


@router.post("/inbox/{inbox_id}/route_manually", response_model=RoutingDecisionView)
async def route_manually(
    ctx: CurrentContext, inbox_id: UUID, body: ManualRouteRequest
) -> RoutingDecisionView:
    """Force-route a document to a partner-chosen client + registration.

    Verifies the registration belongs to the client. If the registration's
    embedded PAN doesn't match the client's PAN we don't fight the trigger,
    we just record the override and refuse to claim it's reconciled.
    """
    inbox = await _fetch_inbox(ctx.session, inbox_id)
    if inbox is None:
        raise NotFoundError("inbox row not found")

    client_pan = await _fetch_client_pan(ctx.session, body.client_id)
    if client_pan is None:
        raise NotFoundError("client not found")
    reg = await _fetch_registration(ctx.session, body.registration_id)
    if reg is None:
        raise NotFoundError("registration not found")
    if reg["client_id"] != body.client_id:
        raise ValidationError("registration does not belong to the given client")

    reconciliation = _reconciliation_status_for_override(inbox.get("raw_parsed_json"), client_pan)

    parsed = inbox.get("raw_parsed_json") or {}
    matter_id = await _match_or_create_matter(
        ctx.session,
        tenant_id=ctx.claims.tenant_id,
        client_id=body.client_id,
        registration_id=body.registration_id,
        law=reg["registration_type"],
        financial_year=parsed.get("financial_year"),
        assessment_year=parsed.get("assessment_year"),
    )
    notice_id = await _create_notice_override(
        ctx.session,
        tenant_id=ctx.claims.tenant_id,
        matter_id=matter_id,
        client_id=body.client_id,
        registration_id=body.registration_id,
        law=reg["registration_type"],
        parsed=parsed,
        source_inbox_id=inbox_id,
        ingest_channel=inbox["ingest_channel"],
        reconciliation_status=reconciliation,
    )
    await ctx.session.execute(
        text(
            """
            UPDATE documents_inbox
            SET routing_status      = 'routed',
                parsed_to_notice_id = :nid,
                parse_status        = 'completed'
            WHERE inbox_id = :id
            """
        ),
        {"id": str(inbox_id), "nid": str(notice_id)},
    )
    await audit.emit(
        ctx.session,
        tenant_id=ctx.claims.tenant_id,
        user_id=ctx.claims.user_id,
        action_type="notice.routed.manual_override",
        entity_type="documents_inbox",
        entity_id=inbox_id,
        after_state={
            "client_id": str(body.client_id),
            "registration_id": str(body.registration_id),
            "created_notice_id": str(notice_id),
            "created_matter_id": str(matter_id),
            "override_reason": body.override_reason,
            "reconciliation_status": reconciliation,
        },
        risk_tier=2,
    )
    await ctx.session.commit()

    return RoutingDecisionView(
        inbox_id=inbox_id,
        routing_status="routed",
        matched_client_id=body.client_id,
        matched_registration_id=body.registration_id,
        created_matter_id=matter_id,
        created_notice_id=notice_id,
        anomaly_details={"override_reason": body.override_reason, "reconciliation": reconciliation},
    )


# ----- Reject ----------------------------------------------------------------


@router.post("/inbox/{inbox_id}/reject")
async def reject_inbox(
    ctx: CurrentContext, inbox_id: UUID, body: RejectInboxRequest
) -> dict[str, str]:
    inbox = await _fetch_inbox(ctx.session, inbox_id)
    if inbox is None:
        raise NotFoundError("inbox row not found")
    if inbox["routing_status"] == "routed":
        raise ValidationError(
            "this document is already routed to a matter and cannot be rejected"
        )

    # Earlier code set routing_status back to 'manual_assignment' — the status
    # an unroutable row already has — so the inbox row looked unchanged after
    # Reject. 'rejected' arrives in migration 0024. The previous anomaly
    # details are kept (merged) so the reason it could not be routed survives.
    try:
        await ctx.session.execute(
            text(
                """
                UPDATE documents_inbox
                SET routing_status          = 'rejected',
                    parse_status            = 'completed',
                    routing_anomaly_details = COALESCE(routing_anomaly_details, '{}'::JSONB)
                                              || CAST(:details AS JSONB),
                    updated_at              = NOW()
                WHERE inbox_id = :id
                """
            ),
            {
                "id": str(inbox_id),
                "details": json.dumps(
                    {
                        "rejected": True,
                        "reason": body.reason,
                        "rejected_by_user_id": str(ctx.claims.user_id),
                        "previous_routing_status": inbox["routing_status"],
                    }
                ),
            },
        )
    except IntegrityError as e:
        await ctx.session.rollback()
        if "routing_status_check" in str(e):
            raise AppError(
                "Rejecting needs database migration "
                "0024_inbox_rejected_status.sql, which has not been applied."
            ) from e
        raise
    await audit.emit(
        ctx.session,
        tenant_id=ctx.claims.tenant_id,
        user_id=ctx.claims.user_id,
        action_type="document.rejected",
        entity_type="documents_inbox",
        entity_id=inbox_id,
        after_state={"reason": body.reason},
        risk_tier=2,
    )
    await ctx.session.commit()
    return {"status": "rejected"}


# ----- Add client (anomaly fix for 'client_not_found') -----------------------


@router.post("/clients", status_code=status.HTTP_201_CREATED)
async def add_client(
    ctx: CurrentContext, body: AddClientRequest
) -> dict[str, Any]:
    if not validate_pan_format(body.pan):
        raise ValidationError("invalid PAN format")

    # Idempotent on (tenant_id, pan): if a partner double-clicks Add Client
    # or retries after a transient failure, we don't want to crash on the
    # UNIQUE constraint — we treat the existing client as the canonical one
    # and continue with the re-route. The status code stays 201 for the
    # "fresh insert" path and 200 for the "already existed" path.
    existing = (
        await ctx.session.execute(
            text(
                "SELECT client_id FROM clients "
                "WHERE tenant_id = :tid AND pan = :pan"
            ),
            {"tid": ctx.claims.tenant_id, "pan": body.pan},
        )
    ).first()

    created = False
    if existing is not None:
        client_id: UUID = existing[0]
    else:
        try:
            inserted = (
                await ctx.session.execute(
                    text(
                        """
                        INSERT INTO clients (
                            tenant_id, pan, legal_name, trade_name, entity_type,
                            cin, date_of_incorporation_or_birth, industry
                        ) VALUES (
                            :tid, :pan, :legal_name, :trade_name, :entity_type,
                            :cin, CAST(:dob AS DATE), :industry
                        ) RETURNING client_id
                        """
                    ),
                    {
                        "tid": ctx.claims.tenant_id,
                        "pan": body.pan,
                        "legal_name": body.legal_name,
                        "trade_name": body.trade_name,
                        "entity_type": body.entity_type,
                        "cin": body.cin,
                        "dob": body.date_of_incorporation_or_birth.isoformat()
                            if body.date_of_incorporation_or_birth else None,
                        "industry": body.industry,
                    },
                )
            ).first()
        except Exception as e:  # noqa: BLE001
            raise ValidationError(f"could not create client: {e}") from e

        client_id = inserted[0]
        created = True

        # Every client gets exactly one IT registration (the PAN itself).
        # Idempotent: if a previous attempt got this far before failing,
        # the partial-unique index from migration 0003 would catch a
        # duplicate insert; we skip if it's already there.
        existing_it = (
            await ctx.session.execute(
                text(
                    "SELECT 1 FROM client_registrations "
                    "WHERE tenant_id = :tid AND client_id = :cid "
                    "  AND registration_type = 'IT' LIMIT 1"
                ),
                {"tid": ctx.claims.tenant_id, "cid": str(client_id)},
            )
        ).first()
        if existing_it is None:
            await ctx.session.execute(
                text(
                    """
                    INSERT INTO client_registrations (
                        tenant_id, client_id, registration_type, identifier_value,
                        registration_status
                    ) VALUES (
                        :tid, :cid, 'IT', :pan, 'active'
                    )
                    """
                ),
                {"tid": ctx.claims.tenant_id, "cid": str(client_id), "pan": body.pan},
            )

        await audit.emit(
            ctx.session,
            tenant_id=ctx.claims.tenant_id,
            user_id=ctx.claims.user_id,
            action_type="client.created",
            entity_type="clients",
            entity_id=client_id,
            after_state={
                "pan": body.pan,
                "legal_name": body.legal_name,
                "via_inbox": str(body.auto_route_inbox_id) if body.auto_route_inbox_id else None,
            },
        )

    # Re-route on the SAME transaction so a re-route failure rolls back the
    # client+registration inserts too. Without this, the partner sees an
    # error and retries, and the second attempt hits the UNIQUE constraint
    # because the first attempt committed the client row.
    re_routed: dict[str, Any] | None = None
    if body.auto_route_inbox_id is not None:
        re_routed = await _rerun_routing_no_commit(ctx, body.auto_route_inbox_id)

    await ctx.session.commit()
    return {"client_id": str(client_id), "created": created, "rerouted": re_routed}


# ----- Add registration ------------------------------------------------------


@router.post("/clients/{client_id}/registrations", status_code=status.HTTP_201_CREATED)
async def add_registration(
    ctx: CurrentContext, client_id: UUID, body: AddRegistrationRequest
) -> dict[str, Any]:
    if body.client_id != client_id:
        raise ValidationError("body.client_id must match path client_id")

    client_pan = await _fetch_client_pan(ctx.session, client_id)
    if client_pan is None:
        raise NotFoundError("client not found")

    if body.registration_type == "IT":
        if not validate_pan_format(body.identifier_value):
            raise ValidationError("IT registration identifier must be a valid PAN")
        if body.identifier_value != client_pan:
            raise PanGstinReconciliationError(
                "IT registration identifier must equal the client's PAN"
            )
    else:  # GST
        if not validate_gstin_format(body.identifier_value):
            raise ValidationError("GST registration identifier must be a valid GSTIN")
        if extract_pan_from_gstin(body.identifier_value) != client_pan:
            raise PanGstinReconciliationError(
                "GSTIN positions 3-12 must equal the client's PAN"
            )
        if not body.state_code:
            # Derive from GSTIN if not provided.
            body = body.model_copy(update={"state_code": body.identifier_value[0:2]})

    # Idempotent on (tenant, identifier_value): a partner double-click or a
    # retry after a transient failure returns the existing registration.
    existing = (
        await ctx.session.execute(
            text(
                "SELECT registration_id FROM client_registrations "
                "WHERE tenant_id = :tid AND identifier_value = :iv"
            ),
            {"tid": ctx.claims.tenant_id, "iv": body.identifier_value},
        )
    ).first()

    created = False
    if existing is not None:
        reg_id: UUID = existing[0]
    else:
        try:
            inserted = (
                await ctx.session.execute(
                    text(
                        """
                        INSERT INTO client_registrations (
                            tenant_id, client_id, registration_type, identifier_value,
                            state_code, state_name, jurisdiction_office, registration_status
                        ) VALUES (
                            :tid, :cid, :rtype, :iv, :state_code, :state_name, :jo, 'active'
                        ) RETURNING registration_id
                        """
                    ),
                    {
                        "tid": ctx.claims.tenant_id,
                        "cid": str(client_id),
                        "rtype": body.registration_type,
                        "iv": body.identifier_value,
                        "state_code": body.state_code,
                        "state_name": body.state_name,
                        "jo": body.jurisdiction_office,
                    },
                )
            ).first()
        except Exception as e:  # noqa: BLE001
            raise ValidationError(f"could not create registration: {e}") from e

        reg_id = inserted[0]
        created = True

        await audit.emit(
            ctx.session,
            tenant_id=ctx.claims.tenant_id,
            user_id=ctx.claims.user_id,
            action_type="registration.created",
            entity_type="client_registrations",
            entity_id=reg_id,
            after_state={
                "client_id": str(client_id),
                "registration_type": body.registration_type,
                "identifier_value": body.identifier_value,
                "state_code": body.state_code,
                "via_inbox": str(body.auto_route_inbox_id) if body.auto_route_inbox_id else None,
            },
        )

    # Re-route on the same transaction. A re-route failure rolls the
    # registration creation back too, so the partner's retry sees a clean
    # slate.
    re_routed: dict[str, Any] | None = None
    if body.auto_route_inbox_id is not None:
        re_routed = await _rerun_routing_no_commit(ctx, body.auto_route_inbox_id)

    await ctx.session.commit()
    return {"registration_id": str(reg_id), "created": created, "rerouted": re_routed}


# ----- Helpers ---------------------------------------------------------------


async def _fetch_inbox(session, inbox_id: UUID) -> dict[str, Any] | None:
    row = (
        await session.execute(
            text(
                """
                SELECT ingest_channel, raw_parsed_json, routing_status
                FROM documents_inbox WHERE inbox_id = :id
                """
            ),
            {"id": str(inbox_id)},
        )
    ).first()
    if row is None:
        return None
    return {"ingest_channel": row[0], "raw_parsed_json": row[1], "routing_status": row[2]}


async def _fetch_client_pan(session, client_id: UUID) -> str | None:
    row = (
        await session.execute(
            text("SELECT pan FROM clients WHERE client_id = :id"),
            {"id": str(client_id)},
        )
    ).first()
    return row[0] if row else None


async def _fetch_registration(session, registration_id: UUID) -> dict[str, Any] | None:
    row = (
        await session.execute(
            text(
                "SELECT client_id, registration_type, identifier_value "
                "FROM client_registrations WHERE registration_id = :id"
            ),
            {"id": str(registration_id)},
        )
    ).first()
    if row is None:
        return None
    return {"client_id": row[0], "registration_type": row[1], "identifier_value": row[2]}


def _reconciliation_status_for_override(parsed: Any, client_pan: str) -> str:
    """If the parsed PAN/GSTIN agrees with the chosen client's PAN, mark
    'reconciled'; otherwise the partner is overriding so we record
    'mismatch_blocked'.
    """
    if not isinstance(parsed, dict):
        return "no_identifier"
    pans = [p.get("value") for p in (parsed.get("pans_extracted") or []) if isinstance(p, dict)]
    gstins = [g.get("value") for g in (parsed.get("gstins_extracted") or []) if isinstance(g, dict)]
    if pans and pans[0] == client_pan:
        return "reconciled"
    if (
        gstins
        and validate_gstin_format(gstins[0])
        and extract_pan_from_gstin(gstins[0]) == client_pan
    ):
        return "reconciled"
    if not pans and not gstins:
        return "no_identifier"
    return "mismatch_blocked"


async def _match_or_create_matter(
    session,
    *,
    tenant_id,
    client_id,
    registration_id,
    law,
    financial_year,
    assessment_year,
):
    from app.agents.notice_routing import _match_or_create_matter as impl  # local import
    return await impl(
        session,
        tenant_id=tenant_id,
        client_id=client_id,
        registration_id=registration_id,
        law=law,
        financial_year=financial_year,
        assessment_year=assessment_year,
    )


async def _create_notice_override(
    session,
    *,
    tenant_id,
    matter_id,
    client_id,
    registration_id,
    law,
    parsed,
    source_inbox_id,
    ingest_channel,
    reconciliation_status,
) -> UUID:
    parsed = parsed or {}
    # asyncpg needs datetime.date for the CAST(... AS DATE) params; ISO
    # strings straight from the parser failed with DataError ("'str' object
    # has no attribute 'toordinal'") and the Assign manually dialog showed
    # "database error".
    inserted = (
        await session.execute(
            text(
                """
                INSERT INTO notices (
                    tenant_id, matter_id, client_id, registration_id, law,
                    document_type, notice_number, din_or_rfn,
                    issue_date, receipt_date, due_date, authority,
                    financial_year, assessment_year,
                    issues, documents_required, hearing_date,
                    demand_amount, ingest_channel, source_inbox_id,
                    parse_confidence, pan_gstin_reconciliation_status,
                    raw_extracted_json
                ) VALUES (
                    :tid, :mid, :cid, :rid, :law,
                    :doc_type, :notice_number, :din,
                    CAST(:issue_date AS DATE), CAST(:receipt_date AS DATE),
                    CAST(:due_date AS DATE), :authority,
                    :fy, :ay,
                    CAST(:issues AS JSONB), CAST(:docs AS JSONB),
                    CAST(:hearing_date AS DATE),
                    :demand_amount, :ingest_channel, :inbox_id,
                    :confidence, :reconciliation,
                    CAST(:raw AS JSONB)
                ) RETURNING notice_id
                """
            ),
            {
                "tid": str(tenant_id),
                "mid": str(matter_id),
                "cid": str(client_id),
                "rid": str(registration_id),
                "law": law,
                "doc_type": parsed.get("document_type"),
                "notice_number": parsed.get("notice_number"),
                "din": parsed.get("din_or_rfn"),
                "issue_date": _coerce_date(parsed.get("issue_date")),
                "receipt_date": _coerce_date(parsed.get("receipt_date")),
                "due_date": _coerce_date(parsed.get("due_date")),
                "authority": parsed.get("authority"),
                "fy": parsed.get("financial_year"),
                "ay": parsed.get("assessment_year"),
                "issues": json.dumps(parsed.get("issues") or []),
                "docs": json.dumps(parsed.get("documents_required") or []),
                "hearing_date": _coerce_date(parsed.get("hearing_date")),
                "demand_amount": parsed.get("demand_amount"),
                "ingest_channel": ingest_channel,
                "inbox_id": str(source_inbox_id),
                "confidence": parsed.get("parse_confidence"),
                "reconciliation": reconciliation_status,
                "raw": json.dumps(parsed),
            },
        )
    ).first()
    return inserted[0]


async def _rerun_routing_no_commit(
    ctx: CurrentContext, inbox_id: UUID
) -> dict[str, Any] | None:
    """Re-run routing on the caller's session WITHOUT committing.

    The caller commits once, after the anomaly fix and the re-route are
    both proven to work. This is what makes add_client + add_registration
    atomic — a re-route failure rolls the partner's fix back too.
    """
    inbox = await _fetch_inbox(ctx.session, inbox_id)
    if inbox is None or not isinstance(inbox.get("raw_parsed_json"), dict):
        return None
    await ctx.session.execute(
        text(
            """
            UPDATE documents_inbox
            SET routing_status = 'pending',
                routing_anomaly_details = NULL
            WHERE inbox_id = :id
            """
        ),
        {"id": str(inbox_id)},
    )
    decision = await route_notice(
        ctx.session,
        tenant_id=ctx.claims.tenant_id,
        inbox_id=inbox_id,
        parsed=inbox["raw_parsed_json"],
        ingest_channel=inbox["ingest_channel"],
    )
    return decision.to_jsonable()
