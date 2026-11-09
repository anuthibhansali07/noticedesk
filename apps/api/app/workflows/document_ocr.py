"""Document OCR pipeline.

Single source of truth for the OCR flow. Both the Temporal workflow and the
InlineDispatcher delegate here, so the logic is identical regardless of the
queue backend. Steps:

1. Set ``ocr_status='in_progress'``, mark ``ocr_started_at``.
2. Fetch the file from storage by ``s3_key``.
3. Try the primary OCR provider with up to 3 retries (exponential backoff).
4. On persistent failure, try the fallback provider once.
5. If both fail, set ``ocr_status='failed'``, record the error, alert Sentry.
6. On success, write extracted text + layout + page count + provider used,
   set ``ocr_status='completed'``.
7. Append an ``audit_logs`` row recording the outcome.

Retries are inside the activity, not the workflow, because Temporal also has
its own retry policy and we don't want both layers fighting.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from uuid import UUID

import sentry_sdk
from sqlalchemy import text

from app.core.db import session_for_tenant
from app.core.logging import get_logger
from app.services import audit
from app.services.ocr import (
    ExtractedDoc,
    OCRError,
    OCRTransientError,
    get_fallback_provider,
    get_primary_provider,
)
from app.services.storage import StorageError, get_storage

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class OcrJob:
    inbox_id: UUID
    tenant_id: UUID

    @classmethod
    def from_dict(cls, payload: dict[str, str]) -> OcrJob:
        return cls(
            inbox_id=UUID(payload["inbox_id"]),
            tenant_id=UUID(payload["tenant_id"]),
        )

    def to_dict(self) -> dict[str, str]:
        return {"inbox_id": str(self.inbox_id), "tenant_id": str(self.tenant_id)}


async def run_ocr_pipeline(job: OcrJob) -> None:
    """End-to-end OCR for one inbox row."""
    log = logger.bind(inbox_id=str(job.inbox_id), tenant_id=str(job.tenant_id))

    async with session_for_tenant(job.tenant_id) as session:
        row = await _fetch_inbox_row(session, job.inbox_id)
        if row is None:
            log.warn("inbox_row_missing")
            return
        s3_key = row["s3_key"]
        mime_type = row["mime_type"] or "application/pdf"

        await _mark_in_progress(session, job.inbox_id)
        await session.commit()

    try:
        file_bytes = await get_storage().get(s3_key)
    except StorageError as e:
        log.error("storage_get_failed", error=str(e))
        await _mark_failed(job, f"storage error: {e}")
        return

    extracted, primary_err = await _try_with_retries(
        get_primary_provider(), file_bytes, mime_type
    )
    if extracted is None:
        fallback = get_fallback_provider()
        if fallback is None:
            # Keep the provider's own reason: "primary OCR failed and no
            # fallback configured" alone hid e.g. a Gemini 503 "model is
            # experiencing high demand", which only needs a retry later.
            await _mark_failed(job, _failure_message(primary_err))
            return
        try:
            extracted = await fallback.extract(file_bytes, mime_type)
        except OCRError as e:
            log.error("fallback_failed", error=str(e))
            await _mark_failed(job, f"primary and fallback both failed: {e}")
            return

    await _mark_completed(job, extracted)


# Backoff between attempts on a transient OCR error (seconds). The old
# 1 s / 2 s schedule gave up after ~3 s, far too soon for a provider that
# reports "model is experiencing high demand"; ~22 s rides out short spikes.
OCR_RETRY_DELAYS: tuple[float, ...] = (2.0, 5.0, 15.0)


def _failure_message(err: Exception | None) -> str:
    if err is None:
        return "OCR failed and no fallback provider is configured"
    hint = (
        " — the OCR service is busy; use Retry OCR in a few minutes"
        if isinstance(err, OCRTransientError)
        else ""
    )
    return f"OCR failed: {err}{hint}"[:1000]


async def _try_with_retries(
    provider,
    file_bytes: bytes,
    mime_type: str,
    *,
    delays: tuple[float, ...] = OCR_RETRY_DELAYS,
) -> tuple[ExtractedDoc | None, Exception | None]:
    attempts = len(delays) + 1
    last_err: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await provider.extract(file_bytes, mime_type), None
        except OCRTransientError as e:
            last_err = e
            logger.warn(
                "ocr_attempt_failed",
                provider=provider.name,
                attempt=attempt,
                error=str(e),
            )
            if attempt < attempts:
                await asyncio.sleep(delays[attempt - 1])
        except OCRError as e:
            # Permanent: don't retry primary, let fallback handle it.
            logger.error("ocr_permanent_error", provider=provider.name, error=str(e))
            last_err = e
            break
    logger.warn("ocr_giving_up_on_provider", provider=provider.name, error=str(last_err))
    return None, last_err


async def _fetch_inbox_row(session, inbox_id: UUID) -> dict[str, str] | None:
    row = (
        await session.execute(
            text("SELECT s3_key, mime_type FROM documents_inbox WHERE inbox_id = :id"),
            {"id": str(inbox_id)},
        )
    ).first()
    if row is None:
        return None
    return {"s3_key": row[0], "mime_type": row[1]}


async def _mark_in_progress(session, inbox_id: UUID) -> None:
    await session.execute(
        text(
            """
            UPDATE documents_inbox
            SET ocr_status = 'in_progress',
                ocr_started_at = clock_timestamp()
            WHERE inbox_id = :id
            """
        ),
        {"id": str(inbox_id)},
    )


async def _mark_completed(job: OcrJob, extracted: ExtractedDoc) -> None:
    async with session_for_tenant(job.tenant_id) as session:
        await session.execute(
            text(
                """
                UPDATE documents_inbox
                SET ocr_status        = 'completed',
                    ocr_text          = :text,
                    ocr_layout_json   = CAST(:layout AS JSONB),
                    ocr_provider_used = :provider,
                    page_count        = :pages,
                    ocr_completed_at  = clock_timestamp(),
                    ocr_error         = NULL
                WHERE inbox_id = :id
                """
            ),
            {
                "id": str(job.inbox_id),
                "text": extracted.text,
                "layout": json.dumps(extracted.layout_json),
                "provider": extracted.provider_name,
                "pages": extracted.page_count,
            },
        )
        await audit.emit(
            session,
            tenant_id=job.tenant_id,
            action_type="ocr.completed",
            entity_type="documents_inbox",
            entity_id=job.inbox_id,
            after_state={
                "provider_used": extracted.provider_name,
                "page_count": extracted.page_count,
            },
        )
        await session.commit()


async def _mark_failed(job: OcrJob, error_message: str) -> None:
    async with session_for_tenant(job.tenant_id) as session:
        await session.execute(
            text(
                """
                UPDATE documents_inbox
                SET ocr_status       = 'failed',
                    ocr_error        = :error,
                    ocr_completed_at = clock_timestamp()
                WHERE inbox_id = :id
                """
            ),
            {"id": str(job.inbox_id), "error": error_message},
        )
        await audit.emit(
            session,
            tenant_id=job.tenant_id,
            action_type="ocr.failed",
            entity_type="documents_inbox",
            entity_id=job.inbox_id,
            after_state={"error": error_message},
            risk_tier=2,
        )
        await session.commit()
    with sentry_sdk.push_scope() as scope:
        scope.set_tag("tenant_id", str(job.tenant_id))
        scope.set_extra("inbox_id", str(job.inbox_id))
        scope.set_extra("error", error_message)
        sentry_sdk.capture_message("ocr_failed", level="error")
