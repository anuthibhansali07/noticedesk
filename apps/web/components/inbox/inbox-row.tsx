"use client";

import type { InboxItem } from "@noticedesk/shared/inbox";
import { FileText, RotateCw } from "lucide-react";
import { useState } from "react";

import { AnomalyActions } from "@/components/inbox/anomaly-actions";
import { Button } from "@/components/ui/button";
import { retryOcr } from "@/lib/api";
import { formatBytes, formatDateTime } from "@/lib/format";

import { ChannelChip } from "./channel-chip";
import { RoutingChip } from "./routing-chip";
import { OcrStatusChip } from "./status-chip";

interface InboxRowProps {
  item: InboxItem;
  onView: (item: InboxItem) => void;
  onResolved: () => void;
}

export function InboxRow({ item, onView, onResolved }: InboxRowProps) {
  const ocrReady = item.ocr_status === "completed";
  const ocrFailed = item.ocr_status === "failed";
  const [retrying, setRetrying] = useState(false);
  const [retryError, setRetryError] = useState<string | null>(null);

  async function handleRetryOcr() {
    setRetrying(true);
    setRetryError(null);
    try {
      await retryOcr(item.inbox_id);
      onResolved();
    } catch (e) {
      setRetryError(e instanceof Error ? e.message : "could not retry OCR");
    } finally {
      setRetrying(false);
    }
  }
  const rejected = item.routing_status === "rejected";
  const rejectionReason = rejected
    ? String(item.routing_anomaly_details?.reason ?? "").trim()
    : "";
  const hasAnomaly =
    item.routing_status !== "routed" &&
    item.routing_status !== "pending" &&
    !rejected &&
    item.parse_status === "completed";

  return (
    <li className="flex flex-col gap-3 rounded-lg border border-slate-200 bg-white p-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex min-w-0 items-start gap-3">
          <div className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-slate-100 text-slate-600">
            <FileText className="h-5 w-5" aria-hidden />
          </div>
          <div className="min-w-0">
            <p className="truncate font-medium text-navy">
              {item.original_filename}
              {item.document_type ? (
                <span className="ml-2 text-xs font-normal text-slate-500">
                  · {item.document_type}
                </span>
              ) : null}
            </p>
            <p className="mt-0.5 text-xs text-slate-500">
              {formatBytes(item.file_size_bytes)}
              {item.page_count ? ` · ${item.page_count} pages` : null}
              {" · "}
              {formatDateTime(item.uploaded_at)}
              {item.parse_confidence != null
                ? ` · ${(item.parse_confidence * 100).toFixed(0)}% confidence`
                : null}
            </p>
            {ocrFailed && item.ocr_error ? (
              <p className="mt-1 text-xs text-red-600">{item.ocr_error}</p>
            ) : null}
            {retryError ? (
              <p className="mt-1 text-xs text-red-600">{retryError}</p>
            ) : null}
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2 sm:justify-end">
          <ChannelChip channel={item.ingest_channel} />
          {/* "Ready for review" (OCR done) is misleading on a rejected row. */}
          {rejected ? null : <OcrStatusChip status={item.ocr_status} />}
          {/* Routing never starts when OCR failed — a spinning "Routing"
              chip there suggested work still in progress. */}
          {ocrFailed ? null : <RoutingChip item={item} />}
          {ocrFailed ? (
            <Button
              variant="secondary"
              size="sm"
              disabled={retrying}
              onClick={() => void handleRetryOcr()}
            >
              <RotateCw className="mr-1.5 h-3.5 w-3.5" aria-hidden />
              {retrying ? "Retrying…" : "Retry OCR"}
            </Button>
          ) : null}
          <Button
            variant="ghost"
            size="sm"
            disabled={!ocrReady}
            onClick={() => onView(item)}
          >
            View OCR text
          </Button>
        </div>
      </div>
      {hasAnomaly ? (
        <div className="border-t border-slate-100 pt-3">
          {item.routing_status === "pan_gstin_mismatch" ? (
            <div className="mb-2 rounded border border-red-200 bg-red-50 p-2 text-xs text-red-700">
              The PAN and GSTIN on this document do not reconcile. Partner
              review required — no auto-fix.
            </div>
          ) : null}
          <AnomalyActions item={item} onResolved={onResolved} />
        </div>
      ) : null}
      {rejected ? (
        <div className="border-t border-slate-100 pt-3 text-xs text-slate-600">
          Rejected — kept on file but will not be routed.
          {rejectionReason ? (
            <>
              {" "}
              Reason: <span className="italic">{rejectionReason}</span>
            </>
          ) : null}
        </div>
      ) : null}
    </li>
  );
}
