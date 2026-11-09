"""Compose-and-send helpers for the outbound emails NoticeDesk fires.

Today: one function — ``send_checklist_to_client`` — fired by the triage
endpoint after the summary + checklist are persisted. Future: reminder
emails (Stage 2) will land alongside it.

These helpers are best-effort and never raise — failures are logged + audited
so a stuck SMTP server or missing client email never blocks the triage
workflow itself. The triage stays usable; the partner sees a banner.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services import audit
from app.services.email import EmailError, EmailMessage, get_email_provider
from app.services.email.render import render

logger = get_logger(__name__)


async def send_checklist_to_client(
    session: AsyncSession,
    *,
    tenant_id: UUID | str,
    user_id: UUID | str | None,
    notice_id: UUID,
) -> dict[str, Any]:
    """Render and send the document-checklist email to the client.

    Returns a status dict the API surface can include in its response:
      {"status": "sent" | "skipped" | "failed", "reason": str, "to": str | None}

    Skipped (not failed) when the client has no email on file — that's the
    expected steady state for clients the firm hasn't enriched yet, not an
    error condition.
    """
    row = await _load_email_context(session, notice_id)
    if row is None:
        return {"status": "skipped", "reason": "notice or matter not found"}

    client_email: str | None = row["client_email"]
    if not client_email:
        return {
            "status": "skipped",
            "reason": "no client email on file",
            "to": None,
        }

    checklist_rows = (
        await session.execute(
            text(
                """
                SELECT label, rationale, doc_type, is_required, position
                FROM notice_document_requirements
                WHERE notice_id = :nid
                ORDER BY position ASC
                """
            ),
            {"nid": str(notice_id)},
        )
    ).mappings().all()

    if not checklist_rows:
        return {
            "status": "skipped",
            "reason": "no checklist items to send",
            "to": client_email,
        }

    settings = get_settings()
    variables = _build_variables(row, checklist_rows, firm_name=row["firm_name"])
    body_text, body_html = render("checklist", variables)

    message = EmailMessage(
        to=(client_email,),
        subject=(
            f"Documents needed for {variables['notice_type_label']} "
            f"({variables['notice_number']})"
        ),
        body_text=body_text,
        body_html=body_html or None,
        from_address=settings.email_from_address,
        from_name=row["firm_name"] or settings.email_from_name,
        tag=f"checklist:{notice_id}",
    )

    provider = get_email_provider()
    try:
        message_id = await provider.send(message)
    except EmailError as e:
        logger.warning(
            "checklist_email_failed",
            notice_id=str(notice_id),
            provider=provider.name,
            error=str(e),
        )
        await audit.emit(
            session,
            tenant_id=tenant_id,
            user_id=user_id,
            action_type="email.checklist_failed",
            entity_type="notice",
            entity_id=notice_id,
            after_state={"to": client_email, "provider": provider.name, "error": str(e)},
        )
        return {"status": "failed", "reason": str(e), "to": client_email}

    await audit.emit(
        session,
        tenant_id=tenant_id,
        user_id=user_id,
        action_type="email.checklist_sent",
        entity_type="notice",
        entity_id=notice_id,
        after_state={
            "to": client_email,
            "provider": provider.name,
            "message_id": message_id,
        },
    )
    return {
        "status": "sent",
        "to": client_email,
        "provider": provider.name,
        "message_id": message_id,
    }


# ---------------------------------------------------------------------------


async def _load_email_context(
    session: AsyncSession, notice_id: UUID
) -> dict[str, Any] | None:
    row = (
        await session.execute(
            text(
                """
                SELECT n.notice_id, n.document_type, n.notice_number,
                       n.din_or_rfn, n.issue_date, n.due_date,
                       n.authority,
                       c.legal_name AS client_legal_name,
                       c.email      AS client_email,
                       r.registration_type, r.identifier_value, r.state_name,
                       t.legal_name AS firm_name,
                       nt.summary
                FROM notices n
                JOIN matters m ON m.matter_id = n.matter_id
                JOIN clients c ON c.client_id = m.client_id
                JOIN client_registrations r ON r.registration_id = m.registration_id
                JOIN tenants t ON t.tenant_id = n.tenant_id
                LEFT JOIN notice_triage nt ON nt.notice_id = n.notice_id
                WHERE n.notice_id = :nid AND c.deleted_at IS NULL
                """
            ),
            {"nid": str(notice_id)},
        )
    ).mappings().first()
    return dict(row) if row else None


def _build_variables(
    ctx: dict[str, Any],
    checklist_rows: list[dict[str, Any]],
    *,
    firm_name: str,
) -> dict[str, Any]:
    notice_type_label = ctx["document_type"] or "the notice"
    issue_date = ctx["issue_date"].isoformat() if ctx.get("issue_date") else "—"
    due_date = ctx["due_date"].isoformat() if ctx.get("due_date") else "—"
    state_qualifier = (
        f" ({ctx['state_name']})" if ctx.get("state_name") else ""
    )
    registration_label = (
        f"{ctx['registration_type']} {ctx['identifier_value']}{state_qualifier}"
    )

    return {
        "firm_name": firm_name or "Your CA firm",
        "client_legal_name": ctx["client_legal_name"],
        "notice_type_label": notice_type_label,
        "notice_number": ctx.get("notice_number") or ctx.get("din_or_rfn") or "—",
        "authority": ctx.get("authority") or "—",
        "issue_date": issue_date,
        "due_date": due_date,
        "registration_label": registration_label,
        "summary": (ctx.get("summary") or "").strip() or "(triage summary not available)",
        "checklist_html": _checklist_html(checklist_rows),
        "checklist_text": _checklist_text(checklist_rows),
        "sent_at": datetime.now(UTC).strftime("%d %b %Y, %H:%M UTC"),
    }


def _checklist_html(rows: list[dict[str, Any]]) -> str:
    parts: list[str] = ['<ol style="margin:0;padding-left:18px;font-size:13.5px;line-height:1.6;color:#1f2937;">']
    for r in rows:
        tag = "Required" if r["is_required"] else "Optional"
        tag_bg = "#fde7e6" if r["is_required"] else "#f3f0e7"
        tag_color = "#b42318" if r["is_required"] else "#6b7280"
        label = _escape(r["label"])
        rationale = _escape(r["rationale"])
        parts.append(
            f'<li style="margin:0 0 12px;">'
            f'<span style="display:inline-block;padding:1px 8px;border-radius:3px;'
            f'background:{tag_bg};color:{tag_color};font-size:10px;font-weight:600;'
            f'letter-spacing:0.04em;text-transform:uppercase;font-family:Helvetica,Arial,sans-serif;'
            f'margin-right:6px;vertical-align:middle;">{tag}</span>'
            f'<strong>{label}</strong><br />'
            f'<span style="color:#6b7280;font-size:12.5px;">{rationale}</span>'
            f'</li>'
        )
    parts.append("</ol>")
    return "".join(parts)


def _checklist_text(rows: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for i, r in enumerate(rows, start=1):
        tag = "[REQUIRED]" if r["is_required"] else "[Optional]"
        lines.append(f"{i}. {tag} {r['label']}")
        lines.append(f"   — {r['rationale']}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
