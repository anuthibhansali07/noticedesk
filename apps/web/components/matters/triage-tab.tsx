"use client";

import { CheckCircle2, Eye, FileText, Loader2, Upload, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { DocumentPreviewDrawer } from "@/components/documents/document-preview-drawer";
import { Button } from "@/components/ui/button";
import { RateLimitNotice } from "@/components/ui/rate-limit-notice";
import { useToast } from "@/components/ui/toast";
import {
  attachDocumentToChecklistItem,
  fetchTriage,
  markChecklistItemNotApplicable,
  runTriage,
  uploadMatterDocument,
  type ChecklistItem,
  type TriagePayload,
} from "@/lib/api";
import { cn } from "@/lib/cn";
import { isRateLimited, useRateLimitRetry } from "@/lib/hooks/use-rate-limit-retry";

interface Props {
  noticeId: string;
  matterId: string;
  onGoToDraft: () => void;
}

// Friendly labels for the doc_type enum the triage agent emits. Anything
// not in this map renders as "Document".
const DOC_TYPE_LABELS: Record<string, string> = {
  gstr_3b: "GSTR-3B",
  gstr_1: "GSTR-1",
  gstr_2a_2b: "GSTR-2A / 2B",
  gstr_9: "GSTR-9",
  gstr_9c: "GSTR-9C",
  itc_ledger: "ITC ledger",
  invoice: "Invoice",
  e_way_bill: "E-way bill",
  bank_statement: "Bank statement",
  reconciliation: "Reconciliation",
  itr: "ITR",
  form_26as: "Form 26AS",
  ais_tis: "AIS / TIS",
  contract: "Contract",
  ledger_extract: "Ledger extract",
  board_resolution: "Board resolution",
  reply_to_prior_notice: "Reply to prior notice",
  other: "Document",
};

export function TriageTab({ noticeId, matterId, onGoToDraft }: Props) {
  const { toast } = useToast();
  const [triage, setTriage] = useState<TriagePayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [busyItem, setBusyItem] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [previewItem, setPreviewItem] = useState<ChecklistItem | null>(null);
  const rateLimit = useRateLimitRetry();

  const load = useCallback(async () => {
    try {
      const t = await fetchTriage(noticeId);
      setTriage(t);
      setError(null);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "failed to load triage");
    } finally {
      setLoading(false);
    }
  }, [noticeId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function handleRun(attempt = 0) {
    rateLimit.cancel();
    setRunning(true);
    setError(null);
    try {
      const next = await runTriage(noticeId);
      setTriage(next);
      const es = next.client_email_status;
      const emailBit =
        es?.status === "sent"
          ? ` · checklist emailed to ${es.to}`
          : es?.status === "skipped"
            ? " · client email not on file"
            : es?.status === "failed"
              ? ` · checklist email failed (${es.reason ?? "unknown"})`
              : "";
      toast(
        `Triage complete · ${next.checklist.length} checklist items${emailBit}`,
        "success",
      );
    } catch (e: unknown) {
      if (isRateLimited(e) && attempt === 0) {
        // Provider throttled us: count down and retry once automatically.
        rateLimit.schedule(e.retryAfterSeconds, () => void handleRun(attempt + 1));
      } else {
        setError(e instanceof Error ? e.message : "triage failed");
      }
    } finally {
      setRunning(false);
    }
  }

  const rateLimitNotice =
    rateLimit.countdown !== null ? (
      <RateLimitNotice
        countdown={rateLimit.countdown}
        what="triage"
        onRetryNow={rateLimit.retryNow}
        onCancel={() => {
          rateLimit.cancel();
          setError("Triage cancelled after the AI provider rate limit. Try again when ready.");
        }}
      />
    ) : null;

  if (loading) {
    return (
      <p className="py-12 text-center font-serif italic text-slate">
        Loading triage…
      </p>
    );
  }

  if (!triage || triage.status === "not_started") {
    return (
      <TriageEmpty
        running={running}
        error={error}
        notice={rateLimitNotice}
        onRun={() => void handleRun()}
      />
    );
  }

  const pendingRequired = triage.checklist.filter(
    (c) => c.is_required && c.status === "pending",
  );

  return (
    <div className="space-y-6">
      {/* Summary card */}
      <div className="overflow-hidden rounded-md border border-slate-line bg-white px-7 py-6">
        <div className="mb-3 flex items-start justify-between gap-4">
          <div>
            <p className="mb-1 text-[10px] font-bold uppercase tracking-[0.14em] text-gold-dark">
              Triage summary
            </p>
            <h2 className="font-serif text-[20px] font-semibold text-navy-deep">
              What this notice is about
            </h2>
          </div>
          <button
            type="button"
            onClick={() => void handleRun()}
            disabled={running || rateLimit.countdown !== null}
            className="text-[11.5px] font-semibold uppercase tracking-[0.06em] text-slate hover:text-ink disabled:opacity-50"
          >
            {running ? "Regenerating…" : "Regenerate"}
          </button>
        </div>
        <div className="whitespace-pre-line font-serif text-[14.5px] leading-relaxed text-ink">
          {triage.summary}
        </div>
        {triage.model ? (
          <p className="mt-4 text-[10.5px] uppercase tracking-[0.06em] text-slate-pale">
            {triage.model} · {triage.prompt_version} ·{" "}
            {triage.generated_at
              ? new Date(triage.generated_at).toLocaleString()
              : "—"}
          </p>
        ) : null}
      </div>

      {/* Checklist */}
      <div className="overflow-hidden rounded-md border border-slate-line bg-white">
        <div className="border-b border-slate-line px-7 py-4">
          <p className="mb-1 text-[10px] font-bold uppercase tracking-[0.14em] text-gold-dark">
            Document checklist
          </p>
          <h3 className="font-serif text-[17px] font-semibold text-navy-deep">
            Evidence to gather before drafting
          </h3>
          <p className="mt-1 font-serif text-[13px] italic text-slate">
            Attach each document the triage agent has identified — the drafter
            will use them as primary evidence instead of writing
            <code className="mx-1 rounded bg-paper-tint px-1.5 py-0.5 not-italic text-[11.5px]">
              [ASSUMED]
            </code>
            markers.
          </p>
        </div>

        <ul className="divide-y divide-slate-line">
          {triage.checklist.map((item) => (
            <ChecklistRow
              key={item.requirement_id}
              item={item}
              matterId={matterId}
              noticeId={noticeId}
              busy={busyItem === item.requirement_id}
              onBusy={(b) => setBusyItem(b ? item.requirement_id : null)}
              onChange={setTriage}
              onError={(m) => toast(m, "error")}
              onPreview={() => setPreviewItem(item)}
            />
          ))}
        </ul>
      </div>

      {/* Footer CTA — soft block */}
      <div className="rounded-md border border-slate-line bg-white px-7 py-5">
        {pendingRequired.length > 0 ? (
          <p className="mb-3 rounded border border-gold/40 bg-gold/10 px-3 py-2 text-[12.5px] text-ink">
            <strong>{pendingRequired.length}</strong>{" "}
            required document{pendingRequired.length === 1 ? "" : "s"} still
            pending. You can still generate a first-cut draft — the drafter
            will mark the missing facts as <code>[DOCUMENT REQUESTED]</code>.
          </p>
        ) : (
          <p className="mb-3 rounded border border-success/30 bg-success-bg px-3 py-2 text-[12.5px] text-success">
            All required documents resolved. The drafter will use them as
            primary evidence.
          </p>
        )}
        <Button variant="gold" size="lg" onClick={onGoToDraft}>
          Continue to draft generation →
        </Button>
      </div>

      {rateLimitNotice}

      {error ? (
        <p className="rounded border border-alarm/30 bg-alarm-bg px-3 py-2 text-sm text-alarm">
          {error}
        </p>
      ) : null}

      <DocumentPreviewDrawer
        documentId={previewItem?.document_id ?? null}
        requirementLabel={previewItem?.label}
        requirementRationale={previewItem?.rationale}
        onClose={() => setPreviewItem(null)}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------

interface EmptyProps {
  running: boolean;
  error: string | null;
  notice?: React.ReactNode;
  onRun: () => void;
}

function TriageEmpty({ running, error, notice, onRun }: EmptyProps) {
  if (running) {
    return (
      <div className="rounded-md border border-slate-line bg-white px-8 py-10">
        <p className="mb-1 text-[10px] font-bold uppercase tracking-[0.14em] text-gold-dark">
          Running triage
        </p>
        <h2 className="mb-2 font-serif text-[24px] font-semibold text-navy-deep">
          Reading the notice and building the checklist
        </h2>
        <p className="mb-6 max-w-[640px] font-serif text-[14px] italic text-slate">
          The triage agent is identifying what the officer is alleging and the
          5-12 documents you&rsquo;ll want in hand before drafting. Typically
          completes in 15-30 seconds.
        </p>
        <div className="flex items-center gap-2 text-[13px] text-slate">
          <Loader2 className="h-4 w-4 animate-spin" />
          Working…
        </div>
      </div>
    );
  }

  return (
    <div className="rounded-md border border-slate-line bg-white px-8 py-10">
      <p className="mb-1 text-[10px] font-bold uppercase tracking-[0.14em] text-gold-dark">
        Begin triage
      </p>
      <h2 className="mb-2 font-serif text-[24px] font-semibold text-navy-deep">
        Build the document checklist for this notice
      </h2>
      <p className="mb-6 max-w-[640px] font-serif text-[14px] italic text-slate">
        Before drafting, the triage agent reads the notice and produces (a) a
        plain-English summary of what&rsquo;s alleged and (b) a checklist of
        evidentiary documents you&rsquo;ll need. Once you attach those
        documents, the drafter cites them directly instead of writing
        <code className="mx-1 rounded bg-paper-tint px-1.5 py-0.5 text-[11.5px]">
          [ASSUMED]
        </code>
        markers.
      </p>
      {notice ? <div className="mb-4">{notice}</div> : null}
      {error ? (
        <p className="mb-4 rounded border border-alarm/30 bg-alarm-bg px-3 py-2 text-sm text-alarm">
          {error}
        </p>
      ) : null}
      <Button variant="gold" size="lg" onClick={onRun} disabled={notice != null}>
        Begin triage
      </Button>
    </div>
  );
}

// ---------------------------------------------------------------------------

interface RowProps {
  item: ChecklistItem;
  matterId: string;
  noticeId: string;
  busy: boolean;
  onBusy: (b: boolean) => void;
  onChange: (next: TriagePayload) => void;
  onError: (m: string) => void;
  onPreview: () => void;
}

function ChecklistRow({
  item,
  matterId,
  noticeId,
  busy,
  onBusy,
  onChange,
  onError,
  onPreview,
}: RowProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const docTypeLabel =
    (item.doc_type && DOC_TYPE_LABELS[item.doc_type]) ?? "Document";

  async function handleFileChosen(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    onBusy(true);
    try {
      const uploaded = await uploadMatterDocument(
        matterId,
        file,
        item.doc_type ?? "supporting",
      );
      const next = await attachDocumentToChecklistItem(
        noticeId,
        item.requirement_id,
        uploaded.document_id,
      );
      onChange(next);
    } catch (err: unknown) {
      onError(err instanceof Error ? err.message : "upload failed");
    } finally {
      onBusy(false);
      // Reset so re-uploading the same file fires the change event.
      if (inputRef.current) inputRef.current.value = "";
    }
  }

  async function handleMarkNa() {
    // Simple prompt for v1; a proper modal lands when the partner feedback
    // calls for it. Empty reason is accepted server-side.
    const reason = window.prompt(
      `Mark "${item.label}" as not applicable. Optional: brief reason for the audit log.`,
      "",
    );
    if (reason === null) return; // user cancelled
    onBusy(true);
    try {
      const next = await markChecklistItemNotApplicable(
        noticeId,
        item.requirement_id,
        reason,
      );
      onChange(next);
    } catch (err: unknown) {
      onError(err instanceof Error ? err.message : "mark N/A failed");
    } finally {
      onBusy(false);
    }
  }

  return (
    <li
      className={cn(
        "flex items-start gap-4 px-7 py-4",
        item.status === "uploaded" && "bg-success-bg/40",
        item.status === "not_applicable" && "bg-paper-tint/60",
      )}
    >
      <StatusIcon status={item.status} />

      <div className="min-w-0 flex-1">
        <div className="mb-1 flex flex-wrap items-center gap-2">
          <span className="font-serif text-[14.5px] font-semibold text-navy-deep">
            {item.label}
          </span>
          <span className="rounded-sm bg-paper-tint px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-[0.06em] text-slate">
            {docTypeLabel}
          </span>
          {item.is_required ? (
            <span className="rounded-sm bg-alarm/10 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-[0.06em] text-alarm">
              Required
            </span>
          ) : (
            <span className="rounded-sm bg-slate-100 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-[0.06em] text-slate">
              Optional
            </span>
          )}
        </div>
        <p className="mb-2 text-[12.5px] leading-relaxed text-ink-soft">
          {item.rationale}
        </p>
        {item.status === "uploaded" && item.document_filename ? (
          <button
            type="button"
            onClick={onPreview}
            disabled={!item.document_id}
            title="Preview the attached document"
            className="flex items-center gap-1.5 text-left text-[12px] text-success hover:underline disabled:no-underline"
          >
            <FileText className="h-3.5 w-3.5 shrink-0" />
            <span className="font-mono">{item.document_filename}</span>
          </button>
        ) : null}
        {item.status === "not_applicable" ? (
          <p className="text-[12px] italic text-slate">
            Marked not applicable
            {item.marked_na_reason ? ` — ${item.marked_na_reason}` : ""}
          </p>
        ) : null}
      </div>

      <div className="flex shrink-0 items-center gap-2">
        <input
          ref={inputRef}
          type="file"
          className="sr-only"
          onChange={handleFileChosen}
          accept=".pdf,.png,.jpg,.jpeg,.docx,.xlsx,.csv"
          disabled={busy}
        />
        {item.status !== "uploaded" ? (
          <Button
            size="sm"
            variant="ghost"
            onClick={() => inputRef.current?.click()}
            disabled={busy}
          >
            <Upload className="mr-1 h-3.5 w-3.5" />
            {item.status === "not_applicable" ? "Upload anyway" : "Upload"}
          </Button>
        ) : (
          <>
            <Button
              size="sm"
              variant="ghost"
              onClick={onPreview}
              disabled={busy || !item.document_id}
            >
              <Eye className="mr-1 h-3.5 w-3.5" />
              Preview
            </Button>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => inputRef.current?.click()}
              disabled={busy}
            >
              Replace
            </Button>
          </>
        )}
        {item.status !== "not_applicable" ? (
          <button
            type="button"
            onClick={handleMarkNa}
            disabled={busy}
            className="text-[11.5px] font-medium text-slate hover:text-alarm disabled:opacity-50"
          >
            Mark N/A
          </button>
        ) : null}
      </div>
    </li>
  );
}

function StatusIcon({ status }: { status: ChecklistItem["status"] }) {
  if (status === "uploaded") {
    return (
      <CheckCircle2 className="mt-0.5 h-5 w-5 shrink-0 text-success" />
    );
  }
  if (status === "not_applicable") {
    return <X className="mt-0.5 h-5 w-5 shrink-0 text-slate-pale" />;
  }
  return (
    <span className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full border border-slate-line">
      <span className="h-2 w-2 rounded-full bg-slate-pale" />
    </span>
  );
}
