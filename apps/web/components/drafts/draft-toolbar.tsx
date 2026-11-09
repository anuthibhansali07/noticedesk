"use client";

import { Download, GitCompare, RefreshCw } from "lucide-react";

import { formatShortLabel, toneLabel } from "@/components/drafts/draft-options";
import { Button } from "@/components/ui/button";
import type { DraftDetail, DraftSummary } from "@/lib/api";
import { cn } from "@/lib/cn";

interface Props {
  current: DraftDetail;
  versions: DraftSummary[];
  onPickVersion: (draftId: string) => void;
  onCompare: () => void;
  onRegenerate: () => void;
  onExport: (mode: "filing" | "client" | "internal") => void;
  busy?: boolean;
}

export function DraftToolbar({
  current,
  versions,
  onPickVersion,
  onCompare,
  onRegenerate,
  onExport,
  busy,
}: Props) {
  const c = current.citation_summary ?? {};
  const verified = (c["verified"] as number) ?? 0;
  const partial = (c["partial"] as number) ?? 0;
  const stripped = (c["stripped"] as number) ?? 0;

  return (
    <header className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-line bg-paper px-5 py-3">
      <div className="flex items-center gap-3">
        <select
          value={current.draft_id}
          onChange={(e) => onPickVersion(e.target.value)}
          className="rounded-sm border border-slate-line bg-white px-2.5 py-1 font-mono text-[11.5px]"
        >
          {versions.map((v) => (
            <option key={v.draft_id} value={v.draft_id}>
              {versionLabel(v)}
              {v.generated_at ? ` · ${shortTs(v.generated_at)}` : ""}
            </option>
          ))}
        </select>
        <Chip tone="bg-paper-tint text-navy-deep">
          {[toneLabel(current.tone), formatShortLabel(current.format_profile)]
            .filter(Boolean)
            .join(" · ") || "—"}
        </Chip>
        <Chip tone="bg-success-bg text-success">{verified} verified</Chip>
        <Chip tone="bg-warn-bg text-[#8B5C1F]">{partial} partial</Chip>
        <Chip tone="bg-alarm-bg text-alarm">{stripped} stripped</Chip>
      </div>
      <div className="flex items-center gap-2">
        <Button
          variant="ghost"
          size="sm"
          onClick={onCompare}
          disabled={versions.length < 2}
        >
          <GitCompare className="mr-1.5 h-3.5 w-3.5" aria-hidden /> Compare versions
        </Button>
        <Button variant="ghost" size="sm" onClick={onRegenerate} disabled={busy}>
          <RefreshCw className="mr-1.5 h-3.5 w-3.5" aria-hidden /> Regenerate with options…
        </Button>
        <ExportMenu onExport={onExport} />
      </div>
    </header>
  );
}

function Chip({ children, tone }: { children: React.ReactNode; tone: string }) {
  return (
    <span
      className={cn(
        "rounded-full px-2.5 py-0.5 text-[10.5px] font-bold uppercase tracking-wide",
        tone,
      )}
    >
      {children}
    </span>
  );
}

function ExportMenu({ onExport }: { onExport: (mode: "filing" | "client" | "internal") => void }) {
  return (
    <div className="relative">
      <details className="relative">
        <summary className="inline-flex cursor-pointer list-none items-center gap-1.5 rounded-md border border-slate-line bg-white px-3.5 py-1.5 text-[12px] font-semibold text-ink hover:border-slate">
          <Download className="h-3.5 w-3.5" aria-hidden /> Export to Word
        </summary>
        <div className="absolute right-0 top-full z-30 mt-1 w-[220px] rounded-md border border-slate-line bg-white py-1 shadow-card-lg">
          <button
            type="button"
            onClick={() => onExport("filing")}
            className="block w-full px-3.5 py-1.5 text-left text-[13px] hover:bg-paper"
          >
            <span className="font-semibold">Filing version</span>
            <span className="block text-[11px] text-slate">
              Without internal note or client summary
            </span>
          </button>
          <button
            type="button"
            onClick={() => onExport("client")}
            className="block w-full px-3.5 py-1.5 text-left text-[13px] hover:bg-paper"
          >
            <span className="font-semibold">Client review version</span>
            <span className="block text-[11px] text-slate">
              Includes the lay-language summary
            </span>
          </button>
          <button
            type="button"
            onClick={() => onExport("internal")}
            className="block w-full px-3.5 py-1.5 text-left text-[13px] hover:bg-paper"
          >
            <span className="font-semibold">Internal review version</span>
            <span className="block text-[11px] text-slate">
              Full draft incl. partner note
            </span>
          </button>
        </div>
      </details>
    </div>
  );
}

// "Draft v2 (Assertive · Tabular)" so iterations are distinguishable in the
// version dropdown.
function versionLabel(v: DraftSummary): string {
  const tags = [toneLabel(v.tone), formatShortLabel(v.format_profile)].filter(Boolean);
  return `Draft v${v.version}${tags.length ? ` (${tags.join(" · ")})` : ""}`;
}

function shortTs(iso: string): string {
  try {
    return new Date(iso).toLocaleString("en-IN", {
      day: "2-digit",
      month: "short",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}
