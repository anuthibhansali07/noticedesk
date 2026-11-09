"""Document upload + inbox listing endpoints."""

from __future__ import annotations

import hashlib
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, File, HTTPException, UploadFile, status
from sqlalchemy import text

from app.core.config import get_settings
from app.core.errors import NotFoundError, ValidationError
from app.middleware.tenant_context import CurrentContext
from app.models.inbox import (
    InboxItem,
    InboxList,
    OcrTextResponse,
    UploadResponse,
)
from app.services import audit
from app.services.storage import get_storage
from app.workflows import get_dispatcher

router = APIRouter()

ALLOWED_MIME_TYPES: frozenset[str] = frozenset(
    {"application/pdf", "image/jpeg", "image/jpg", "image/png"}
)


@router.post(
    "/documents/upload",
    response_model=UploadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_document(
    ctx: CurrentContext,
    file: Annotated[UploadFile, File(...)],
    ingest_channel: str = "web_upload",
) -> UploadResponse:
    if ingest_channel not in {"web_upload", "mobile_capture"}:
        raise ValidationError(
            "ingest_channel for uploads must be web_upload or mobile_capture, "
            f"got {ingest_channel!r}"
        )

    mime = (file.content_type or "").lower()
    if mime not in ALLOWED_MIME_TYPES:
        raise ValidationError(
            f"unsupported mime type {mime!r}; allowed: {sorted(ALLOWED_MIME_TYPES)}"
        )

    settings = get_settings()
    payload = await file.read()
    if not payload:
        raise ValidationError("uploaded file is empty")
    if len(payload) > settings.max_upload_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"file exceeds max upload size of {settings.max_upload_bytes} bytes",
        )

    file_hash = hashlib.sha256(payload).hexdigest()
    inbox_id = uuid4()
    filename = file.filename or "upload.bin"
    s3_key = f"tenants/{ctx.claims.tenant_id}/inbox/{inbox_id}/{_safe_filename(filename)}"

    await get_storage().put(s3_key, payload, content_type=mime)

    await ctx.session.execute(
        text(
            """
            INSERT INTO documents_inbox (
                inbox_id, tenant_id, uploaded_by_user_id, original_filename,
                file_hash, file_size_bytes, mime_type, s3_key, ingest_channel
            ) VALUES (
                :id, :tid, :uid, :name, :hash, :size, :mime, :key, :chan
            )
            """
        ),
        {
            "id": str(inbox_id),
            "tid": ctx.claims.tenant_id,
            "uid": ctx.claims.user_id,
            "name": filename,
            "hash": file_hash,
            "size": len(payload),
            "mime": mime,
            "key": s3_key,
            "chan": ingest_channel,
        },
    )
    await audit.emit(
        ctx.session,
        tenant_id=ctx.claims.tenant_id,
        user_id=ctx.claims.user_id,
        action_type="document.uploaded",
        entity_type="documents_inbox",
        entity_id=inbox_id,
        after_state={
            "filename": filename,
            "size_bytes": len(payload),
            "mime_type": mime,
            "ingest_channel": ingest_channel,
            "file_hash": file_hash,
        },
    )
    await ctx.session.commit()

    await get_dispatcher().submit_ocr(inbox_id, UUID(ctx.claims.tenant_id))

    return UploadResponse(
        inbox_id=inbox_id,
        ocr_status="pending",
        file_hash=file_hash,
        file_size_bytes=len(payload),
    )


@router.get("/documents/inbox", response_model=InboxList)
async def list_inbox(ctx: CurrentContext, limit: int = 50, offset: int = 0) -> InboxList:
    if limit < 1 or limit > 200:
        raise ValidationError("limit must be between 1 and 200")
    rows = (
        await ctx.session.execute(
            text(
                """
                SELECT
                  di.inbox_id, di.original_filename, di.file_size_bytes, di.page_count,
                  di.mime_type, di.ocr_status, di.ocr_provider_used, di.ocr_error,
                  di.ingest_channel, di.parse_status, di.routing_status,
                  di.routing_anomaly_details, di.parsed_to_notice_id,
                  c.legal_name AS matched_client_name,
                  CASE
                    WHEN r.registration_type = 'GST' THEN
                      COALESCE(r.state_name, r.state_code) || ' GST'
                    WHEN r.registration_type = 'IT' THEN 'Income Tax'
                    ELSE NULL
                  END AS matched_registration_label,
                  n.document_type,
                  n.parse_confidence,
                  di.uploaded_at
                FROM documents_inbox di
                LEFT JOIN notices n ON n.notice_id = di.parsed_to_notice_id
                LEFT JOIN clients c ON c.client_id = n.client_id
                LEFT JOIN client_registrations r ON r.registration_id = n.registration_id
                ORDER BY di.uploaded_at DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            {"limit": limit, "offset": offset},
        )
    ).all()
    # "Awaiting review" used to be COUNT(*) of every row, so rejected and
    # already-routed documents inflated the inbox heading and sidebar badge.
    counts = (
        await ctx.session.execute(
            text(
                """
                SELECT COUNT(*),
                       COUNT(*) FILTER (
                           WHERE routing_status NOT IN ('routed', 'rejected')
                       )
                FROM documents_inbox
                """
            )
        )
    ).one()
    total_row, awaiting_row = counts[0], counts[1]
    items = [
        InboxItem(
            inbox_id=r[0],
            original_filename=r[1],
            file_size_bytes=r[2],
            page_count=r[3],
            mime_type=r[4],
            ocr_status=r[5],
            ocr_provider_used=r[6],
            ocr_error=r[7],
            ingest_channel=r[8],
            parse_status=r[9],
            routing_status=r[10],
            routing_anomaly_details=r[11],
            parsed_to_notice_id=r[12],
            matched_client_name=r[13],
            matched_registration_label=r[14],
            document_type=r[15],
            parse_confidence=float(r[16]) if r[16] is not None else None,
            uploaded_at=r[17],
        )
        for r in rows
    ]
    return InboxList(items=items, total=int(total_row), awaiting_review=int(awaiting_row))


@router.get("/documents/inbox/{inbox_id}/ocr", response_model=OcrTextResponse)
async def get_ocr_text(ctx: CurrentContext, inbox_id: UUID) -> OcrTextResponse:
    row = (
        await ctx.session.execute(
            text(
                """
                SELECT ocr_status, ocr_text, ocr_provider_used, page_count, ocr_error
                FROM documents_inbox
                WHERE inbox_id = :id
                """
            ),
            {"id": str(inbox_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError("document not found")
    return OcrTextResponse(
        inbox_id=inbox_id,
        ocr_status=row[0],
        ocr_text=row[1],
        ocr_provider_used=row[2],
        page_count=row[3],
        ocr_error=row[4],
    )


@router.post("/documents/inbox/{inbox_id}/retry-ocr")
async def retry_ocr(ctx: CurrentContext, inbox_id: UUID) -> dict[str, str]:
    """Re-run OCR (then parse + route) on a document whose OCR failed.

    A failed row used to be a dead end: the provider is often only briefly
    unavailable (Gemini 503 "high demand"), but the partner had to upload
    the file again to get another attempt.
    """
    row = (
        await ctx.session.execute(
            text("SELECT ocr_status FROM documents_inbox WHERE inbox_id = :id"),
            {"id": str(inbox_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError("document not found")
    if row[0] != "failed":
        raise ValidationError(f"OCR can only be retried after it failed (status: {row[0]})")

    await ctx.session.execute(
        text(
            """
            UPDATE documents_inbox
            SET ocr_status = 'pending', ocr_error = NULL
            WHERE inbox_id = :id
            """
        ),
        {"id": str(inbox_id)},
    )
    await audit.emit(
        ctx.session,
        tenant_id=ctx.claims.tenant_id,
        user_id=ctx.claims.user_id,
        action_type="ocr.retried",
        entity_type="documents_inbox",
        entity_id=inbox_id,
        after_state={"ocr_status": "pending"},
    )
    await ctx.session.commit()
    await get_dispatcher().submit_ocr(inbox_id, UUID(ctx.claims.tenant_id))
    return {"inbox_id": str(inbox_id), "ocr_status": "pending"}


def _safe_filename(filename: str) -> str:
    # Basic path-safety: strip directory components and keep a small set of
    # filesystem-friendly characters. Storage keys must be predictable.
    name = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return "".join(c if c.isalnum() or c in "._-+ " else "_" for c in name) or "upload.bin"
