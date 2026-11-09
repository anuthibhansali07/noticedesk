"""Matter-attached document endpoints.

The Sprint 2 inbox upload (/v1/documents/upload) creates a
``documents_inbox`` row and runs the parse-and-route pipeline; that's for
the *original* notice PDF. Sprint 5 needs a parallel surface for the
*supporting* documents partners attach to a matter (books, invoices, prior
correspondence) — those land directly in ``documents`` with a lifecycle
stage and never go through routing.

Endpoints:
- POST   /v1/matters/{id}/documents              — upload a supporting doc
- GET    /v1/matters/{id}/documents              — list documents on matter
- PATCH  /v1/documents/{id}                      — lifecycle_stage / document_type
- DELETE /v1/documents/{id}                      — remove (hard delete, partner only)
"""

from __future__ import annotations

import hashlib
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, File, HTTPException, Response, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.core.config import get_settings
from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.middleware.tenant_context import CurrentContext
from app.services import audit
from app.services.ocr import OCRError, get_primary_provider
from app.services.storage import get_storage

logger = get_logger(__name__)

router = APIRouter()


# Mime types where running OCR makes sense (image/PDF). Text files are
# decoded directly. docx / xlsx land without extracted_text for now —
# extraction for office formats is a separate problem from OCR.
_OCR_MIME = frozenset({"application/pdf", "image/jpeg", "image/png"})
_PLAINTEXT_MIME = frozenset({"text/plain", "text/csv"})


_ALLOWED_MIME = frozenset(
    {
        "application/pdf",
        "image/jpeg",
        "image/png",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "text/plain",
        "text/csv",
    }
)

_ALLOWED_LIFECYCLE = (
    "requested",
    "received",
    "validated",
    "used_in_draft",
    "approved",
    "submitted",
    "acknowledged",
    "referenced_in_appeal",
)


def _safe_filename(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name)[:120] or "upload.bin"


async def _extract_text(
    payload: bytes, mime: str, *, document_id: UUID
) -> str | None:
    """Best-effort text extraction at upload time.

    OCR for PDF/image; UTF-8 decode for text/csv; nothing for office docs
    (those need a different extractor and are not in scope). OCR failures
    are non-fatal — the upload still succeeds, the partner just doesn't get
    the document fed into draft generation. A warning is logged so the
    operator can see the silent degradation.
    """
    if mime in _PLAINTEXT_MIME:
        try:
            return payload.decode("utf-8", errors="replace")
        except Exception:
            return None
    if mime not in _OCR_MIME:
        return None
    try:
        provider = get_primary_provider()
        extracted = await provider.extract(payload, mime)
        return extracted.text or None
    except OCRError as e:
        logger.warning(
            "matter_document_ocr_failed",
            document_id=str(document_id),
            mime=mime,
            error=str(e),
        )
        return None
    except Exception:
        logger.exception(
            "matter_document_ocr_unexpected_error",
            document_id=str(document_id),
            mime=mime,
        )
        return None


@router.post(
    "/matters/{matter_id}/documents",
    status_code=status.HTTP_201_CREATED,
)
async def upload_matter_document(
    ctx: CurrentContext,
    matter_id: UUID,
    file: Annotated[UploadFile, File(...)],
    document_type: str = "supporting",
) -> dict[str, Any]:
    settings = get_settings()
    mime = (file.content_type or "").lower()
    if mime not in _ALLOWED_MIME:
        raise ValidationError(
            f"unsupported mime type {mime!r}; allowed: {sorted(_ALLOWED_MIME)}"
        )

    # Verify the matter belongs to this tenant before we burn an S3 round-trip.
    matter_row = (
        await ctx.session.execute(
            text("SELECT matter_id FROM matters WHERE matter_id = :mid"),
            {"mid": str(matter_id)},
        )
    ).first()
    if matter_row is None:
        raise NotFoundError("matter not found")

    payload = await file.read()
    if not payload:
        raise ValidationError("uploaded file is empty")
    if len(payload) > settings.max_upload_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"file exceeds max upload size of {settings.max_upload_bytes} bytes",
        )

    file_hash = hashlib.sha256(payload).hexdigest()
    document_id = uuid4()
    filename = _safe_filename(file.filename or "upload.bin")
    s3_key = (
        f"tenants/{ctx.claims.tenant_id}/matters/{matter_id}/"
        f"{document_id}/{filename}"
    )

    storage = get_storage()
    await storage.put(s3_key, payload, content_type=mime)

    extracted_text = await _extract_text(payload, mime, document_id=document_id)

    await ctx.session.execute(
        text(
            """
            INSERT INTO documents (
                document_id, tenant_id, matter_id, document_type,
                filename, file_hash, s3_key, mime_type, size_bytes,
                lifecycle_stage, uploaded_by_user_id, uploaded_at,
                extracted_text
            ) VALUES (
                :id, :tid, :mid, :dtype,
                :name, :hash, :key, :mime, :size,
                'received', :uid, NOW(),
                :etext
            )
            """
        ),
        {
            "id": str(document_id),
            "tid": ctx.claims.tenant_id,
            "mid": str(matter_id),
            "dtype": document_type,
            "name": file.filename or filename,
            "hash": file_hash,
            "key": s3_key,
            "mime": mime,
            "size": len(payload),
            "uid": ctx.claims.user_id,
            "etext": extracted_text,
        },
    )

    await audit.emit(
        ctx.session,
        tenant_id=ctx.claims.tenant_id,
        user_id=ctx.claims.user_id,
        action_type="document.attached",
        entity_type="documents",
        entity_id=document_id,
        after_state={
            "matter_id": str(matter_id),
            "filename": file.filename,
            "size_bytes": len(payload),
            "mime_type": mime,
            "document_type": document_type,
        },
        risk_tier=1,
    )
    await ctx.session.commit()
    return {
        "document_id": str(document_id),
        "filename": file.filename,
        "size_bytes": len(payload),
        "lifecycle_stage": "received",
    }


@router.get("/matters/{matter_id}/documents")
async def list_matter_documents(
    ctx: CurrentContext, matter_id: UUID
) -> dict[str, Any]:
    rows = (
        await ctx.session.execute(
            text(
                """
                SELECT d.document_id, d.filename, d.document_type, d.mime_type,
                       d.size_bytes, d.lifecycle_stage, d.uploaded_at,
                       u.name AS uploaded_by_name
                FROM documents d
                LEFT JOIN users u ON u.user_id = d.uploaded_by_user_id
                WHERE d.matter_id = :mid
                ORDER BY d.uploaded_at DESC
                """
            ),
            {"mid": str(matter_id)},
        )
    ).mappings().all()
    return {
        "documents": [
            {
                "document_id": str(r["document_id"]),
                "filename": r["filename"],
                "document_type": r["document_type"],
                "mime_type": r["mime_type"],
                "size_bytes": r["size_bytes"],
                "lifecycle_stage": r["lifecycle_stage"],
                "uploaded_at": r["uploaded_at"].isoformat() if r["uploaded_at"] else None,
                "uploaded_by_name": r["uploaded_by_name"],
            }
            for r in rows
        ],
        "total": len(rows),
    }


class UpdateDocumentRequest(BaseModel):
    document_type: str | None = None
    lifecycle_stage: str | None = Field(default=None)


@router.patch("/documents/{document_id}")
async def update_document(
    ctx: CurrentContext, document_id: UUID, body: UpdateDocumentRequest
) -> dict[str, Any]:
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise ValidationError("no fields to update")
    if "lifecycle_stage" in fields and fields["lifecycle_stage"] not in _ALLOWED_LIFECYCLE:
        raise ValidationError(
            f"lifecycle_stage must be one of {_ALLOWED_LIFECYCLE}"
        )

    before = (
        await ctx.session.execute(
            text(
                "SELECT document_type, lifecycle_stage FROM documents "
                "WHERE document_id = :did"
            ),
            {"did": str(document_id)},
        )
    ).mappings().first()
    if before is None:
        raise NotFoundError("document not found")

    set_clauses = ", ".join(f"{k} = :{k}" for k in fields)
    params: dict[str, Any] = {"did": str(document_id), **fields}
    await ctx.session.execute(
        text(f"UPDATE documents SET {set_clauses} WHERE document_id = :did"),
        params,
    )

    await audit.emit(
        ctx.session,
        tenant_id=ctx.claims.tenant_id,
        user_id=ctx.claims.user_id,
        action_type="document.updated",
        entity_type="documents",
        entity_id=document_id,
        before_state={k: before[k] for k in fields},
        after_state=fields,
        risk_tier=1,
    )
    await ctx.session.commit()
    return {"document_id": str(document_id), "updated_fields": list(fields)}


# ---------------------------------------------------------------------------
# Preview: metadata + extracted text, and the raw file
# ---------------------------------------------------------------------------

# Mime types the browser can render inline in the preview drawer. Anything
# else (docx / xlsx) is served as an attachment download.
_INLINE_PREVIEW_MIME = frozenset(
    {"application/pdf", "image/jpeg", "image/png", "text/plain", "text/csv"}
)
# Characters of OCR / extracted text returned for the "Extracted text" tab.
_PREVIEW_TEXT_CHARS = 20_000


@router.get("/documents/{document_id}")
async def get_document(ctx: CurrentContext, document_id: UUID) -> dict[str, Any]:
    """Metadata + extracted-text excerpt for the checklist preview drawer."""
    row = (
        await ctx.session.execute(
            text(
                """
                SELECT d.document_id, d.matter_id, d.filename, d.document_type,
                       d.mime_type, d.size_bytes, d.lifecycle_stage,
                       d.uploaded_at, d.file_hash,
                       LEFT(COALESCE(d.extracted_text, ''), :chars) AS extracted_text,
                       LENGTH(COALESCE(d.extracted_text, '')) AS extracted_text_length,
                       u.name AS uploaded_by_name
                FROM documents d
                LEFT JOIN users u ON u.user_id = d.uploaded_by_user_id
                WHERE d.document_id = :did
                """
            ),
            {"did": str(document_id), "chars": _PREVIEW_TEXT_CHARS},
        )
    ).mappings().first()
    if row is None:
        raise NotFoundError("document not found")
    mime = (row["mime_type"] or "").lower()
    return {
        "document_id": str(row["document_id"]),
        "matter_id": str(row["matter_id"]) if row["matter_id"] else None,
        "filename": row["filename"],
        "document_type": row["document_type"],
        "mime_type": row["mime_type"],
        "size_bytes": row["size_bytes"],
        "lifecycle_stage": row["lifecycle_stage"],
        "uploaded_at": row["uploaded_at"].isoformat() if row["uploaded_at"] else None,
        "uploaded_by_name": row["uploaded_by_name"],
        "file_hash": row["file_hash"],
        "previewable": mime in _INLINE_PREVIEW_MIME,
        "extracted_text": row["extracted_text"] or None,
        "extracted_text_truncated": (row["extracted_text_length"] or 0) > _PREVIEW_TEXT_CHARS,
    }


@router.get("/documents/{document_id}/file")
async def get_document_file(
    ctx: CurrentContext, document_id: UUID, download: bool = False
) -> Response:
    """Stream the stored file. ``inline`` for previewable types unless
    ``?download=1``; tenant scoping comes from RLS on the documents row."""
    row = (
        await ctx.session.execute(
            text(
                "SELECT filename, mime_type, s3_key FROM documents "
                "WHERE document_id = :did"
            ),
            {"did": str(document_id)},
        )
    ).mappings().first()
    if row is None or not row["s3_key"]:
        raise NotFoundError("document not found")

    try:
        payload = await get_storage().get(row["s3_key"])
    except Exception as e:  # noqa: BLE001 — storage backends raise varied errors
        logger.warning("document_file_missing", document_id=str(document_id), error=str(e))
        raise NotFoundError("stored file not found for this document") from e

    mime = (row["mime_type"] or "application/octet-stream").lower()
    disposition = (
        "inline" if (mime in _INLINE_PREVIEW_MIME and not download) else "attachment"
    )
    filename = _safe_filename(row["filename"] or "document")
    return Response(
        content=payload,
        media_type=mime,
        headers={
            "Content-Disposition": f'{disposition}; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )
