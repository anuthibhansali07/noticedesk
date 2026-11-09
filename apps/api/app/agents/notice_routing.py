"""Notice Routing Agent — pure rule-based PAN-centric routing.

Sprint 3, the five-step procedure:

  A. Determine the canonical PAN from extracted identifiers.
  B. Resolve client by canonical PAN within the current tenant.
  C. Resolve registration (IT or GST) under that client.
  D. Create matter + notice rows, link to source documents_inbox.
  E. Emit audit_logs.action_type='notice.routed'.

Anomalies the agent surfaces (these are the user-facing recovery flows the
inbox UI handles):

  - 'pan_gstin_mismatch'         : PAN and GSTIN[2:12] disagree.
  - 'no_identifier_found'        : neither PAN nor GSTIN was extracted.
  - 'client_not_found'           : canonical PAN matches no client.
  - 'new_gst_registration_detected'
                                 : client matches by PAN but the specific
                                   GSTIN isn't on file yet.
  - 'manual_assignment'          : ambiguous resolution, partner must pick.

The agent never auto-creates a client or a GST registration. Those are
partner-confirmed flows in the inbox UI (Sprint 3 spec).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.services import audit
from app.services.identity import (
    extract_pan_from_gstin,
    validate_gstin_format,
    validate_pan_format,
)

logger = get_logger(__name__)


RoutingStatus = str  # values match documents_inbox.routing_status CHECK


@dataclass(slots=True)
class NoticeRoutingDecision:
    inbox_id: UUID
    routing_status: RoutingStatus
    canonical_pan: str | None = None
    matched_client_id: UUID | None = None
    matched_registration_id: UUID | None = None
    created_matter_id: UUID | None = None
    created_notice_id: UUID | None = None
    anomaly_details: dict[str, Any] | None = None

    def to_jsonable(self) -> dict[str, Any]:
        d = asdict(self)
        for k, v in list(d.items()):
            if isinstance(v, UUID):
                d[k] = str(v)
        if isinstance(d.get("inbox_id"), UUID):
            d["inbox_id"] = str(d["inbox_id"])
        return d


# ---- Public entry point ------------------------------------------------------


async def route_notice(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    inbox_id: UUID,
    parsed: dict[str, Any],
    ingest_channel: str,
) -> NoticeRoutingDecision:
    """Run the five-step routing procedure for one inbox row."""
    decision = NoticeRoutingDecision(inbox_id=inbox_id, routing_status="pending")

    # ---- Step A: canonical PAN + reconciliation -----------------------------
    canonical_pan, identifier_for_routing, anomaly = _resolve_canonical_pan(parsed)
    if anomaly is not None:
        decision.routing_status = anomaly["status"]
        decision.canonical_pan = canonical_pan
        decision.anomaly_details = anomaly["details"]
        await _persist_inbox_status(session, inbox_id, decision)
        await _emit_audit(session, tenant_id, inbox_id, decision, parsed, ingest_channel)
        return decision
    assert canonical_pan is not None
    decision.canonical_pan = canonical_pan

    # ---- Step B: client by canonical_pan ------------------------------------
    matched = await _find_clients_by_pan(session, tenant_id, canonical_pan)
    if not matched:
        decision.routing_status = "client_not_found"
        decision.anomaly_details = {
            "canonical_pan": canonical_pan,
            "extracted_name": parsed.get("client_name_on_document"),
            "extracted_gstins": [g["value"] for g in parsed.get("gstins_extracted") or []],
        }
        await _persist_inbox_status(session, inbox_id, decision)
        await _emit_audit(session, tenant_id, inbox_id, decision, parsed, ingest_channel)
        return decision
    if len(matched) > 1:
        # Defensive — the UNIQUE(tenant_id, pan) constraint makes this
        # impossible, but multiple rows would mean a data-corruption bug
        # we don't want to silently route past.
        decision.routing_status = "manual_assignment"
        decision.anomaly_details = {
            "canonical_pan": canonical_pan,
            "duplicate_client_ids": [str(c[0]) for c in matched],
        }
        await _persist_inbox_status(session, inbox_id, decision)
        await _emit_audit(session, tenant_id, inbox_id, decision, parsed, ingest_channel)
        return decision
    client_id = matched[0][0]
    client_legal_name = matched[0][1]
    decision.matched_client_id = client_id

    # ---- Step C: registration -----------------------------------------------
    law = parsed.get("law")
    if law not in ("GST", "IT"):
        decision.routing_status = "manual_assignment"
        decision.anomaly_details = {"reason": "parsed law is null or unknown"}
        await _persist_inbox_status(session, inbox_id, decision)
        await _emit_audit(session, tenant_id, inbox_id, decision, parsed, ingest_channel)
        return decision

    if law == "IT":
        reg = await _find_it_registration(session, tenant_id, client_id)
        if reg is None:
            # Every client has exactly one IT registration (Sprint 1 schema),
            # so this is a data anomaly worth flagging rather than auto-fixing.
            decision.routing_status = "manual_assignment"
            decision.anomaly_details = {
                "reason": "client has no IT registration; expected exactly one",
                "client_id": str(client_id),
            }
            await _persist_inbox_status(session, inbox_id, decision)
            await _emit_audit(session, tenant_id, inbox_id, decision, parsed, ingest_channel)
            return decision
        registration_id = reg
    else:
        assert identifier_for_routing is not None and validate_gstin_format(identifier_for_routing)
        reg = await _find_gst_registration(session, tenant_id, identifier_for_routing)
        if reg is None:
            decision.routing_status = "new_gst_registration_detected"
            decision.anomaly_details = {
                "canonical_pan": canonical_pan,
                "client_id": str(client_id),
                "client_legal_name": client_legal_name,
                "gstin": identifier_for_routing,
                "state_code": identifier_for_routing[0:2],
            }
            await _persist_inbox_status(session, inbox_id, decision)
            await _emit_audit(session, tenant_id, inbox_id, decision, parsed, ingest_channel)
            return decision
        registration_id = reg

    decision.matched_registration_id = registration_id

    # ---- Step D: match or create matter, create notice ----------------------
    matter_id = await _match_or_create_matter(
        session,
        tenant_id=tenant_id,
        client_id=client_id,
        registration_id=registration_id,
        law=law,
        financial_year=parsed.get("financial_year"),
        assessment_year=parsed.get("assessment_year"),
    )
    decision.created_matter_id = matter_id

    notice_id = await _create_notice(
        session,
        tenant_id=tenant_id,
        matter_id=matter_id,
        client_id=client_id,
        registration_id=registration_id,
        law=law,
        parsed=parsed,
        source_inbox_id=inbox_id,
        ingest_channel=ingest_channel,
    )
    decision.created_notice_id = notice_id
    decision.routing_status = "routed"

    await _persist_inbox_status(
        session, inbox_id, decision, parsed_to_notice_id=notice_id, parse_status="completed"
    )
    await _emit_audit(session, tenant_id, inbox_id, decision, parsed, ingest_channel)
    return decision


# ---- Step A helpers ---------------------------------------------------------


def _resolve_canonical_pan(
    parsed: dict[str, Any],
) -> tuple[str | None, str | None, dict[str, Any] | None]:
    """Return (canonical_pan, identifier_for_routing, anomaly_or_None).

    See the docstring at the top of this module for the routing semantics.
    """
    law = parsed.get("law")
    pans = parsed.get("pans_extracted") or []
    gstins = parsed.get("gstins_extracted") or []
    pan_values = [p["value"] for p in pans if isinstance(p, dict) and "value" in p]
    gstin_values = [g["value"] for g in gstins if isinstance(g, dict) and "value" in g]

    # Cross-check first: if both a PAN and a GSTIN exist, their PAN portions
    # must agree, regardless of which "law" the document claims to be.
    if pan_values and gstin_values:
        gstin_pan = extract_pan_from_gstin(gstin_values[0])
        if pan_values[0] != gstin_pan:
            return None, None, {
                "status": "pan_gstin_mismatch",
                "details": {
                    "extracted_pan": pan_values[0],
                    "extracted_gstin": gstin_values[0],
                    "gstin_pan_portion": gstin_pan,
                },
            }

    if law == "IT" and pan_values:
        pan = pan_values[0]
        if not validate_pan_format(pan):
            return None, None, {
                "status": "no_identifier_found",
                "details": {"reason": "extracted PAN failed format validation"},
            }
        return pan, pan, None

    if law == "GST" and gstin_values:
        gstin = gstin_values[0]
        if not validate_gstin_format(gstin):
            return None, None, {
                "status": "no_identifier_found",
                "details": {"reason": "extracted GSTIN failed format validation"},
            }
        return extract_pan_from_gstin(gstin), gstin, None

    # Last-resort fallbacks: if law mis-classified, still try to route off the
    # available identifier rather than blocking.
    if gstin_values:
        gstin = gstin_values[0]
        if validate_gstin_format(gstin):
            return extract_pan_from_gstin(gstin), gstin, None
    if pan_values:
        pan = pan_values[0]
        if validate_pan_format(pan):
            return pan, pan, None

    return None, None, {
        "status": "no_identifier_found",
        "details": {"reason": "no PAN or GSTIN extracted"},
    }


# ---- Step B / C / D helpers -------------------------------------------------


async def _find_clients_by_pan(
    session: AsyncSession, tenant_id: UUID, pan: str
) -> list[tuple[UUID, str]]:
    rows = (
        await session.execute(
            text(
                "SELECT client_id, legal_name FROM clients "
                "WHERE tenant_id = :tid AND pan = :pan"
            ),
            {"tid": str(tenant_id), "pan": pan},
        )
    ).all()
    return [(r[0], r[1]) for r in rows]


async def _find_it_registration(
    session: AsyncSession, tenant_id: UUID, client_id: UUID
) -> UUID | None:
    row = (
        await session.execute(
            text(
                "SELECT registration_id FROM client_registrations "
                "WHERE tenant_id = :tid AND client_id = :cid "
                "  AND registration_type = 'IT' LIMIT 1"
            ),
            {"tid": str(tenant_id), "cid": str(client_id)},
        )
    ).first()
    return row[0] if row else None


async def _find_gst_registration(
    session: AsyncSession, tenant_id: UUID, gstin: str
) -> UUID | None:
    row = (
        await session.execute(
            text(
                "SELECT registration_id FROM client_registrations "
                "WHERE tenant_id = :tid AND registration_type = 'GST' "
                "  AND identifier_value = :iv LIMIT 1"
            ),
            {"tid": str(tenant_id), "iv": gstin},
        )
    ).first()
    return row[0] if row else None


async def _match_or_create_matter(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    client_id: UUID,
    registration_id: UUID,
    law: str,
    financial_year: str | None,
    assessment_year: str | None,
) -> UUID:
    # Match on (client, registration, law, FY, AY). NULLs match NULLs via
    # IS NOT DISTINCT FROM so a Sprint-3-issued notice without an AY merges
    # cleanly with another from the same FY.
    existing = (
        await session.execute(
            text(
                """
                SELECT matter_id FROM matters
                WHERE tenant_id = :tid
                  AND client_id = :cid
                  AND registration_id = :rid
                  AND law = :law
                  AND financial_year IS NOT DISTINCT FROM :fy
                  AND assessment_year IS NOT DISTINCT FROM :ay
                LIMIT 1
                """
            ),
            {
                "tid": str(tenant_id), "cid": str(client_id),
                "rid": str(registration_id), "law": law,
                "fy": financial_year, "ay": assessment_year,
            },
        )
    ).first()
    if existing:
        return existing[0]

    inserted = (
        await session.execute(
            text(
                """
                INSERT INTO matters (
                    tenant_id, client_id, registration_id, law,
                    financial_year, assessment_year
                ) VALUES (
                    :tid, :cid, :rid, :law, :fy, :ay
                ) RETURNING matter_id
                """
            ),
            {
                "tid": str(tenant_id), "cid": str(client_id),
                "rid": str(registration_id), "law": law,
                "fy": financial_year, "ay": assessment_year,
            },
        )
    ).first()
    assert inserted is not None
    return inserted[0]


def _coerce_date(value: Any) -> date | None:
    # asyncpg's prepared-statement codec rejects strings where postgres infers
    # DATE — must be a datetime.date. Sanitiser upstream already validates
    # ISO format, so this is a last-mile encoding hop.
    # Manual assignment also passes parser output that never went through the
    # sanitiser, so an unparseable date becomes NULL instead of a 500.
    if value is None or isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


async def _create_notice(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    matter_id: UUID,
    client_id: UUID,
    registration_id: UUID,
    law: str,
    parsed: dict[str, Any],
    source_inbox_id: UUID,
    ingest_channel: str,
) -> UUID:
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
                    :confidence, 'reconciled',
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
                "raw": json.dumps(parsed),
            },
        )
    ).first()
    assert inserted is not None
    return inserted[0]


# ---- Step E + persistence ---------------------------------------------------


async def _persist_inbox_status(
    session: AsyncSession,
    inbox_id: UUID,
    decision: NoticeRoutingDecision,
    *,
    parsed_to_notice_id: UUID | None = None,
    parse_status: str | None = None,
) -> None:
    await session.execute(
        text(
            """
            UPDATE documents_inbox
            SET routing_status            = :status,
                routing_anomaly_details   = CAST(:details AS JSONB),
                parsed_to_notice_id       = COALESCE(:notice_id, parsed_to_notice_id),
                parse_status              = COALESCE(:parse_status, parse_status)
            WHERE inbox_id = :id
            """
        ),
        {
            "id": str(inbox_id),
            "status": decision.routing_status,
            "details": json.dumps(decision.anomaly_details) if decision.anomaly_details else None,
            "notice_id": str(parsed_to_notice_id) if parsed_to_notice_id else None,
            "parse_status": parse_status,
        },
    )


async def _emit_audit(
    session: AsyncSession,
    tenant_id: UUID,
    inbox_id: UUID,
    decision: NoticeRoutingDecision,
    parsed: dict[str, Any],
    ingest_channel: str,
) -> None:
    await audit.emit(
        session,
        tenant_id=tenant_id,
        action_type=f"notice.routing.{decision.routing_status}",
        entity_type="documents_inbox",
        entity_id=inbox_id,
        after_state={
            "routing_status": decision.routing_status,
            "canonical_pan": decision.canonical_pan,
            "matched_client_id": (
                str(decision.matched_client_id) if decision.matched_client_id else None
            ),
            "matched_registration_id": (
                str(decision.matched_registration_id)
                if decision.matched_registration_id else None
            ),
            "created_matter_id": (
                str(decision.created_matter_id) if decision.created_matter_id else None
            ),
            "created_notice_id": (
                str(decision.created_notice_id) if decision.created_notice_id else None
            ),
            "anomaly_details": decision.anomaly_details,
            "document_type": parsed.get("document_type"),
            "law": parsed.get("law"),
            "parse_confidence": parsed.get("parse_confidence"),
            "ingest_channel": ingest_channel,
        },
        risk_tier=(
            3 if decision.routing_status == "pan_gstin_mismatch"
            else 1
        ),
    )
