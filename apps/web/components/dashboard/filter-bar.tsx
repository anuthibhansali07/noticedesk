"use client";

import { useEffect, useMemo, useRef, useState } from "react";

import type { ClientSummary } from "@/lib/api";
import { cn } from "@/lib/cn";
import { stateNameFor } from "@/lib/states";

interface Props {
  clients: ClientSummary[];
  law: "GST" | "IT" | "";
  clientId: string;
  stateCode: string;
  onLawChange: (v: "GST" | "IT" | "") => void;
  onClientChange: (v: string) => void;
  onStateChange: (v: string) => void;
  resultCount: number;
}

// Filter pills: Partner / Law / Client / State.
//
// Phase 1 wires three of them (law / client / state) against the seed.
// Partner is a UI-only stub because the seed parks assignee names in
// raw_extracted_json rather than as a real users FK — once we tie staff
// to user rows, the same dropdown shell wires up cleanly.
export function FilterBar({
  clients,
  law,
  clientId,
  stateCode,
  onLawChange,
  onClientChange,
  onStateChange,
  resultCount,
}: Props) {
  const stateCodes = useMemo(() => {
    const seen = new Set<string>();
    for (const c of clients) {
      for (const sc of c.gst_state_codes) {
        if (sc) seen.add(sc);
      }
    }
    return Array.from(seen).sort();
  }, [clients]);

  return (
    <div className="mb-4 flex flex-wrap items-center gap-2.5">
      <FilterPill
        label="All partners"
        active={false}
        disabled
        options={[]}
        onSelect={() => {}}
      />
      <FilterPill
        label={law === "" ? "Both laws" : law === "GST" ? "GST only" : "Income Tax only"}
        active={law !== ""}
        options={[
          { value: "", label: "Both laws" },
          { value: "GST", label: "GST only" },
          { value: "IT", label: "Income Tax only" },
        ]}
        onSelect={(v) => onLawChange(v as "GST" | "IT" | "")}
      />
      <FilterPill
        label={
          clientId
            ? (clients.find((c) => c.client_id === clientId)?.legal_name ?? "Client")
            : "All clients"
        }
        active={clientId !== ""}
        options={[
          { value: "", label: "All clients" },
          ...clients.map((c) => ({ value: c.client_id, label: c.legal_name })),
        ]}
        onSelect={onClientChange}
      />
      <FilterPill
        label={stateCode ? stateLabel(stateCode) : "All states"}
        active={stateCode !== ""}
        options={[
          { value: "", label: "All states" },
          ...stateCodes.map((sc) => ({ value: sc, label: stateLabel(sc) })),
        ]}
        onSelect={onStateChange}
      />
      <p className="ml-auto text-[12px] font-medium text-slate">
        {resultCount} {resultCount === 1 ? "notice" : "notices"} · sorted by due date
      </p>
    </div>
  );
}

interface PillOption {
  value: string;
  label: string;
}

function FilterPill({
  label,
  active,
  options,
  onSelect,
  disabled,
}: {
  label: string;
  active: boolean;
  options: PillOption[];
  onSelect: (v: string) => void;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        disabled={disabled}
        onClick={() => setOpen((o) => !o)}
        className={cn(
          "inline-flex items-center gap-1.5 rounded-sm border px-3 py-1.5 text-[12px] font-medium transition-colors",
          active
            ? "border-navy bg-navy text-paper"
            : "border-slate-line bg-white text-ink-soft hover:border-slate",
          disabled && "cursor-not-allowed opacity-50",
        )}
      >
        <span>{label}</span>
        <svg className="h-2.5 w-2.5 opacity-60" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}>
          <polyline points="6 9 12 15 18 9" />
        </svg>
      </button>
      {open && options.length > 0 ? (
        <div className="absolute left-0 top-full z-40 mt-1 max-h-[280px] w-[240px] overflow-y-auto rounded-md border border-slate-line bg-white py-1 shadow-card-lg">
          {options.map((o) => (
            <button
              key={o.value || "all"}
              type="button"
              onClick={() => {
                onSelect(o.value);
                setOpen(false);
              }}
              className="block w-full px-3.5 py-1.5 text-left text-[13px] hover:bg-paper"
            >
              {o.label}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

// "Tamil Nadu (33)" rather than a bare "State 33"; unknown codes keep the
// old form so nothing disappears from the list.
function stateLabel(code: string): string {
  const name = stateNameFor(code);
  return name ? `${name} (${code})` : `State ${code}`;
}
