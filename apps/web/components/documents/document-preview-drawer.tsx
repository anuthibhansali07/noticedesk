"use client";

import { Download, ExternalLink, Loader2, X } from "lucide-react";
import { useEffect, useState } from "react";

import {
  documentFileUrl,
  fetchDocument,
  type DocumentDetail,
} from "@/lib/api";
import { cn } from "@/lib/cn";

interface Props {
  documentId: string | null;
  // Checklist requirement shown alongside the file so the partner can check
  // the upload actually answers it (right AY, legible scan, matching figures).
  requirementLabel?: string | undefined;
  requirementRationale?: string | undefined;
  onClose: () => void;
}

type Tab = "preview" | "text";

export function DocumentPreviewDrawer({
  documentId,
  requirementLabel,
  requirementRationale,
  onClose,
}: Props) {
  const [doc, setDoc] = useState<DocumentDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("preview");

  useEffect(() => {
    if (!documentId) return;
    let cancelled = false;
    setDoc(null);
    setError(null);
    setTab("preview");
    fetchDocument(documentId)
      .then((d) => {
        if (cancelled) return;
        setDoc(d);
        if (!d.previewable) setTab("text");
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : "failed to load document");
      });
    return () => {
      cancelled = true;
    };
  }, [documentId]);

  useEffect(() => {
    if (!documentId) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [documentId, onClose]);

  if (!documentId) return null;
  const fileUrl = documentFileUrl(documentId);
  const mime = (doc?.mime_type ?? "").toLowerCase();

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-navy/40" onClick={onClose}>
      <aside
        role="dialog"
        aria-modal="true"
        aria-label={doc?.filename ?? "Document preview"}
        className="flex h-full w-full max-w-[920px] flex-col bg-white shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="flex items-start justify-between gap-4 border-b border-slate-line px-6 py-4">
          <div className="min-w-0">
            <p className="mb-1 text-[10px] font-bold uppercase tracking-[0.14em] text-gold-dark">
              Evidence preview
            </p>
            <h2 className="truncate font-mono text-[14px] font-semibold text-navy-deep">
              {doc?.filename ?? "Loading…"}
            </h2>
            {doc ? (
              <p className="mt-1 text-[11.5px] text-slate">
                {[
                  doc.mime_type,
                  doc.size_bytes != null ? formatBytes(doc.size_bytes) : null,
                  doc.uploaded_at ? `uploaded ${new Date(doc.uploaded_at).toLocaleString("en-IN")}` : null,
                  doc.uploaded_by_name ? `by ${doc.uploaded_by_name}` : null,
                ]
                  .filter(Boolean)
                  .join(" · ")}
              </p>
            ) : null}
          </div>
          <div className="flex shrink-0 items-center gap-3">
            <a
              href={fileUrl}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 text-[12px] font-semibold text-slate hover:text-ink"
            >
              <ExternalLink className="h-3.5 w-3.5" aria-hidden /> Open
            </a>
            <a
              href={documentFileUrl(documentId, { download: true })}
              className="inline-flex items-center gap-1 text-[12px] font-semibold text-slate hover:text-ink"
            >
              <Download className="h-3.5 w-3.5" aria-hidden /> Download
            </a>
            <button type="button" onClick={onClose} aria-label="Close" className="text-slate hover:text-navy">
              <X className="h-5 w-5" />
            </button>
          </div>
        </header>

        {requirementLabel ? (
          <div className="border-b border-slate-line bg-paper-tint px-6 py-3 text-[12.5px] text-ink">
            <span className="font-semibold">Checklist item:</span> {requirementLabel}
            {requirementRationale ? (
              <span className="block text-[12px] text-slate">{requirementRationale}</span>
            ) : null}
          </div>
        ) : null}

        <nav className="flex gap-1 border-b border-slate-line px-6">
          <TabButton active={tab === "preview"} onClick={() => setTab("preview")} disabled={!doc?.previewable}>
            Document
          </TabButton>
          <TabButton active={tab === "text"} onClick={() => setTab("text")}>
            Extracted text
          </TabButton>
        </nav>

        <div className="min-h-0 flex-1 overflow-auto bg-paper">
          {error ? (
            <p className="m-6 rounded border border-alarm/30 bg-alarm-bg px-3 py-2 text-sm text-alarm">{error}</p>
          ) : !doc ? (
            <p className="flex items-center gap-2 p-6 text-[13px] text-slate">
              <Loader2 className="h-4 w-4 animate-spin" /> Loading…
            </p>
          ) : tab === "preview" ? (
            mime.startsWith("image/") ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img src={fileUrl} alt={doc.filename} className="mx-auto max-w-full p-4" />
            ) : (
              <iframe src={fileUrl} title={doc.filename} className="h-full min-h-[70vh] w-full border-0 bg-white" />
            )
          ) : doc.extracted_text ? (
            <div className="p-6">
              <p className="mb-2 text-[11px] text-slate">
                Text the drafter reads from this document (OCR / extraction).
                {doc.extracted_text_truncated ? " Showing the first 20,000 characters." : ""}
              </p>
              <pre className="whitespace-pre-wrap rounded border border-slate-line bg-white p-4 font-mono text-[12px] leading-relaxed text-ink">
                {doc.extracted_text}
              </pre>
            </div>
          ) : (
            <p className="p-6 font-serif text-[13.5px] italic text-slate">
              No text could be extracted from this file, so the drafter cannot
              read its contents — check the scan is legible, or upload a PDF /
              text version.
            </p>
          )}
        </div>
      </aside>
    </div>
  );
}

function TabButton({
  active,
  disabled,
  onClick,
  children,
}: {
  active: boolean;
  disabled?: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={cn(
        "-mb-px border-b-2 px-3 py-2 text-[12px] font-semibold",
        active ? "border-gold text-navy-deep" : "border-transparent text-slate hover:text-ink",
        disabled && "cursor-not-allowed opacity-40",
      )}
    >
      {children}
    </button>
  );
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}
