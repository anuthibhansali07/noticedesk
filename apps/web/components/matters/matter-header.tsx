"use client";

import type { NoticeDetail } from "@/lib/api";
import { cn } from "@/lib/cn";
import { formatDate } from "@/lib/format";

import { LifecycleDropdown } from "./lifecycle-dropdown";

interface Props {
  data: NoticeDetail;
  onTransition: (next: string, reason: string | null) => Promise<void>;
}

function daysUntil(due: string | null): { text: string; tone: string } {
  if (!due) return { text: "—", tone: "text-slate" };
  const d = new Date(due);
  const today = new Date();
  d.setHours(0, 0, 0, 0);
  today.setHours(0, 0, 0, 0);
  const diff = Math.round((d.getTime() - today.getTime()) / (1000 * 60 * 60 * 24));
  if (diff < 0)
    return { text: `${Math.abs(diff)} d over`, tone: "text-alarm" };
  if (diff === 0) return { text: "Today", tone: "text-alarm" };
  if (diff === 1) return { text: "Tomorrow", tone: "text-gold-dark" };
  if (diff <= 14) return { text: `${diff} days`, tone: "text-gold-dark" };
  return { text: `${diff} days`, tone: "text-slate" };
}

function ingestLabel(channel: string): string {
  if (channel.endsWith("_portal_gsp") || channel.endsWith("_portal_aa")) return "Portal";
  if (channel === "email") return "Email";
  if (channel === "whatsapp") return "WhatsApp";
  if (channel === "mobile_capture") return "Mobile";
  return "Upload";
}

export function MatterHeader({ data, onTransition }: Props) {
  const { notice, client, registration } = data;
  const days = daysUntil(notice.due_date);
  const period =
    notice.law === "GST"
      ? notice.financial_year
        ? `FY ${notice.financial_year}`
        : "—"
      : notice.assessment_year
        ? `AY ${notice.assessment_year}`
        : "—";

  return (
    <header className="relative mb-5 rounded-md border border-slate-line bg-white px-7 py-6">
      {/* No overflow-hidden on the header: it clipped the lifecycle "Move to"
          popover at the header's bottom edge. The gold bar rounds its own top
          corners to match the card instead. */}
      <div className="absolute inset-x-0 top-0 h-[3px] rounded-t-md bg-gold" />
      <div className="flex items-start justify-between gap-6">
        <div className="min-w-0">
          <p className="mb-2 text-[10px] font-bold uppercase tracking-[0.14em] text-gold-dark">
            {notice.document_type ?? notice.law} · {client.legal_name}
          </p>
          <h1 className="mb-1.5 font-serif text-[26px] font-semibold leading-tight tracking-tight text-navy-deep">
            {notice.issue ?? notice.document_type ?? "Untitled notice"}
          </h1>
          <p className="font-serif text-[14px] italic text-slate">
            {registration.registration_type === "GST"
              ? `${registration.state_name ?? registration.state_code} · ${registration.identifier_value}`
              : `Income Tax · ${registration.identifier_value}`}
            {" · "}
            {period}
          </p>
        </div>
        <div className="flex shrink-0 flex-col items-end gap-2">
          <LifecycleDropdown current={notice.lifecycle_status} onTransition={onTransition} />
          <span
            className={cn(
              "rounded-full bg-paper-tint px-2.5 py-0.5 text-[10.5px] font-semibold text-slate",
            )}
          >
            via {ingestLabel(notice.ingest_channel)}
          </span>
        </div>
      </div>

      <dl className="mt-6 grid grid-cols-2 gap-x-6 gap-y-4 border-t border-slate-line pt-5 md:grid-cols-4">
        <Meta label="PAN" value={client.pan} mono />
        <Meta
          label={registration.registration_type === "GST" ? "GSTIN" : "Registration"}
          value={registration.identifier_value}
          mono
        />
        <Meta label={notice.law === "GST" ? "Financial year" : "Assessment year"} value={period} />
        <Meta label="Authority" value={notice.authority ?? "—"} />
        <Meta label="DIN / RFN" value={notice.din_or_rfn ?? "—"} mono />
        <Meta
          label="Due date"
          value={
            <span>
              {notice.due_date ? formatDate(notice.due_date) : "—"}
              {notice.due_date ? (
                <span className={cn("ml-2 font-mono text-[11.5px] font-semibold", days.tone)}>
                  {days.text}
                </span>
              ) : null}
            </span>
          }
        />
        <Meta label="Assigned to" value={notice.assigned_to ?? "—"} />
        {notice.hearing_date ? (
          <Meta label="Hearing" value={formatDate(notice.hearing_date)} />
        ) : (
          <Meta label="Demand" value={notice.demand_amount ? `₹${notice.demand_amount}` : "—"} />
        )}
      </dl>
    </header>
  );
}

function Meta({
  label,
  value,
  mono,
}: {
  label: string;
  value: React.ReactNode;
  mono?: boolean;
}) {
  return (
    <div>
      <dt className="mb-1 text-[10.5px] font-semibold uppercase tracking-[0.06em] text-slate">
        {label}
      </dt>
      <dd
        className={cn(
          "text-[13.5px] font-medium text-ink",
          mono && "font-mono text-[12.5px]",
        )}
      >
        {value}
      </dd>
    </div>
  );
}
