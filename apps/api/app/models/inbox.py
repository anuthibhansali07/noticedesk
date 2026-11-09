"""Pydantic models for the documents_inbox API surface."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

OcrStatus = Literal["pending", "in_progress", "completed", "failed"]
ParseStatus = Literal["pending", "in_progress", "completed", "failed", "needs_review"]
RoutingStatus = Literal[
    "pending",
    "routed",
    "client_not_found",
    "new_gst_registration_detected",
    "pan_gstin_mismatch",
    "no_identifier_found",
    "manual_assignment",
    "rejected",
]
IngestChannel = Literal[
    "web_upload",
    "mobile_capture",
    "email",
    "gst_portal_gsp",
    "it_portal_aa",
    "whatsapp",
]


class InboxItem(BaseModel):
    inbox_id: UUID
    original_filename: str
    file_size_bytes: int
    page_count: int | None = None
    mime_type: str | None = None
    ocr_status: OcrStatus
    ocr_provider_used: str | None = None
    ocr_error: str | None = None
    ingest_channel: IngestChannel
    parse_status: ParseStatus
    routing_status: RoutingStatus
    routing_anomaly_details: dict[str, object] | None = None
    parsed_to_notice_id: UUID | None = None
    matched_client_name: str | None = None
    matched_registration_label: str | None = None
    document_type: str | None = None
    parse_confidence: float | None = None
    uploaded_at: datetime


class InboxList(BaseModel):
    items: list[InboxItem]
    # Every inbox row (paging). Use awaiting_review for "N awaiting" badges.
    total: int
    # Rows that still need a partner: not yet routed to a matter and not
    # rejected.
    awaiting_review: int = 0


class UploadResponse(BaseModel):
    inbox_id: UUID
    ocr_status: OcrStatus = "pending"
    file_hash: str
    file_size_bytes: int


class OcrTextResponse(BaseModel):
    inbox_id: UUID
    ocr_status: OcrStatus
    ocr_text: str | None
    ocr_provider_used: str | None
    page_count: int | None
    ocr_error: str | None


class EmailAttachment(BaseModel):
    filename: str
    mime_type: str
    content_b64: str = Field(..., description="Base64-encoded attachment bytes")


class EmailInboundPayload(BaseModel):
    sender: str
    recipient: str
    subject: str | None = None
    received_at: datetime | None = None
    attachments: list[EmailAttachment] = Field(default_factory=list)


class EmailInboundResponse(BaseModel):
    accepted: int
    skipped: int
    inbox_ids: list[UUID]
