// Sprint 2: shared inbox types. Mirror the documents_inbox table and the
// Pydantic models in apps/api/app/models/inbox.py.

export type OcrStatus = "pending" | "in_progress" | "completed" | "failed";

export type ParseStatus =
  | "pending"
  | "in_progress"
  | "completed"
  | "failed"
  | "needs_review";

export type RoutingStatus =
  | "pending"
  | "routed"
  | "client_not_found"
  | "new_gst_registration_detected"
  | "pan_gstin_mismatch"
  | "no_identifier_found"
  | "manual_assignment"
  | "rejected";

export type IngestChannel =
  | "web_upload"
  | "mobile_capture"
  | "email"
  | "gst_portal_gsp"
  | "it_portal_aa"
  | "whatsapp";

export interface InboxItem {
  inbox_id: string;
  original_filename: string;
  file_size_bytes: number;
  page_count: number | null;
  mime_type: string | null;
  ocr_status: OcrStatus;
  ocr_provider_used: string | null;
  ocr_error: string | null;
  ingest_channel: IngestChannel;
  parse_status: ParseStatus;
  routing_status: RoutingStatus;
  routing_anomaly_details: Record<string, unknown> | null;
  parsed_to_notice_id: string | null;
  matched_client_name: string | null;
  matched_registration_label: string | null;
  document_type: string | null;
  parse_confidence: number | null;
  uploaded_at: string;
}

export interface InboxList {
  items: InboxItem[];
  // Every inbox row (paging).
  total: number;
  // Not yet routed and not rejected: what "N awaiting review" badges show.
  awaiting_review: number;
}

export interface UploadResponse {
  inbox_id: string;
  ocr_status: OcrStatus;
  file_hash: string;
  file_size_bytes: number;
}

export interface OcrTextResponse {
  inbox_id: string;
  ocr_status: OcrStatus;
  ocr_text: string | null;
  ocr_provider_used: string | null;
  page_count: number | null;
  ocr_error: string | null;
}

export const INGEST_CHANNEL_LABELS: Record<IngestChannel, string> = {
  web_upload: "Upload",
  mobile_capture: "Mobile",
  email: "Email",
  gst_portal_gsp: "GST portal",
  it_portal_aa: "IT portal",
  whatsapp: "WhatsApp",
};

export const OCR_STATUS_LABELS: Record<OcrStatus, string> = {
  pending: "Uploading",
  in_progress: "OCR in progress",
  completed: "Ready for review",
  failed: "OCR failed",
};

export const ROUTING_STATUS_LABELS: Record<RoutingStatus, string> = {
  pending: "Routing",
  routed: "Routed",
  client_not_found: "Client not found",
  new_gst_registration_detected: "New GST registration",
  pan_gstin_mismatch: "PAN / GSTIN mismatch",
  no_identifier_found: "No identifier",
  manual_assignment: "Manual assignment needed",
  rejected: "Rejected",
};
