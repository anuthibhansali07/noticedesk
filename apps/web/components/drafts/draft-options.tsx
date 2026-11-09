"use client";

// Tone + reply-format pickers shared by the first-generation screen and the
// "Regenerate with options" dialog, so v2 / v3 can change either.

import type { DraftFormatProfile, DraftTone } from "@/lib/api";
import { cn } from "@/lib/cn";

export interface DraftOptions {
  tone: DraftTone;
  formatProfile: DraftFormatProfile;
  instructions: string;
  includeCross: boolean;
}

export const DEFAULT_DRAFT_OPTIONS: DraftOptions = {
  tone: "formal",
  formatProfile: "full_submission",
  instructions: "",
  includeCross: false,
};

export const TONE_OPTIONS: Array<{ value: DraftTone; label: string; desc: string }> = [
  {
    value: "formal",
    label: "Formal",
    desc: "Third-person, measured, courteous — the default for first-round replies.",
  },
  {
    value: "assertive",
    label: "Assertive",
    desc: "Direct rebuttal where law + facts support it; still courteous.",
  },
  {
    value: "conciliatory",
    label: "Conciliatory",
    desc: "Emphasise willingness to comply; request indulgence on procedure.",
  },
];

export const FORMAT_OPTIONS: Array<{
  value: DraftFormatProfile;
  label: string;
  short: string;
  desc: string;
}> = [
  {
    value: "full_submission",
    label: "Comprehensive submissions",
    short: "Full",
    desc: "Full 15-section statutory rebuttal — for scrutiny / SCN replies.",
  },
  {
    value: "tabular_reconciliation",
    label: "Tabular reconciliation + covering note",
    short: "Tabular",
    desc: "Streamlined reply for simple mismatch notices (s.142(1), ASMT-10).",
  },
  {
    value: "adjournment",
    label: "Adjournment / time extension",
    short: "Adjournment",
    desc: "Formal petition requesting 15-30 days more; no merits argued.",
  },
];

export function toneLabel(tone: string | null | undefined): string | null {
  return TONE_OPTIONS.find((o) => o.value === tone)?.label ?? null;
}

export function formatShortLabel(profile: string | null | undefined): string | null {
  return FORMAT_OPTIONS.find((o) => o.value === profile)?.short ?? null;
}

interface FieldsProps {
  value: DraftOptions;
  onChange: (next: DraftOptions) => void;
  instructionsLabel?: string;
  instructionsPlaceholder?: string;
}

export function DraftOptionsFields({
  value,
  onChange,
  instructionsLabel = "Partner instructions (optional)",
  instructionsPlaceholder = "e.g. Lean on the limitation point. Officer is open to reconciliation-based closure.",
}: FieldsProps) {
  const set = (patch: Partial<DraftOptions>) => onChange({ ...value, ...patch });

  return (
    <>
      <fieldset className="mb-6">
        <legend className="mb-2 text-[11px] font-semibold uppercase tracking-[0.08em] text-slate">
          Tone
        </legend>
        <div className="grid grid-cols-1 gap-2 md:grid-cols-3">
          {TONE_OPTIONS.map((o) => (
            <OptionCard
              key={o.value}
              name="tone"
              selected={value.tone === o.value}
              onSelect={() => set({ tone: o.value })}
              label={o.label}
              desc={o.desc}
            />
          ))}
        </div>
      </fieldset>

      <fieldset className="mb-6">
        <legend className="mb-2 text-[11px] font-semibold uppercase tracking-[0.08em] text-slate">
          Reply format
        </legend>
        <div className="grid grid-cols-1 gap-2 md:grid-cols-3">
          {FORMAT_OPTIONS.map((o) => (
            <OptionCard
              key={o.value}
              name="format_profile"
              selected={value.formatProfile === o.value}
              onSelect={() => set({ formatProfile: o.value })}
              label={o.label}
              desc={o.desc}
            />
          ))}
        </div>
      </fieldset>

      <label className="mb-2 block text-[11px] font-semibold uppercase tracking-[0.08em] text-slate">
        {instructionsLabel}
      </label>
      <textarea
        value={value.instructions}
        onChange={(e) => set({ instructions: e.target.value })}
        rows={3}
        maxLength={4000}
        placeholder={instructionsPlaceholder}
        className="mb-4 w-full rounded-sm border border-slate-line bg-white px-3.5 py-2 text-[13px] outline-none focus:border-gold"
      />

      <label className="mb-6 flex items-start gap-2.5 text-[12.5px] text-ink-soft">
        <input
          type="checkbox"
          checked={value.includeCross}
          onChange={(e) => set({ includeCross: e.target.checked })}
          className="mt-0.5"
        />
        <span>
          Include cross-registration context (only check if facts from a
          sibling state / law are genuinely relevant — they land in their own
          Section 09 and never in the factual matrix).
        </span>
      </label>
    </>
  );
}

function OptionCard({
  name,
  selected,
  onSelect,
  label,
  desc,
}: {
  name: string;
  selected: boolean;
  onSelect: () => void;
  label: string;
  desc: string;
}) {
  return (
    <label
      className={cn(
        "cursor-pointer rounded-sm border bg-white px-3.5 py-3 transition-colors",
        selected ? "border-gold ring-1 ring-gold" : "border-slate-line hover:border-slate",
      )}
    >
      <input type="radio" name={name} checked={selected} onChange={onSelect} className="sr-only" />
      <p className="font-serif text-[14px] font-semibold text-navy-deep">{label}</p>
      <p className="mt-1 text-[11.5px] leading-relaxed text-slate">{desc}</p>
    </label>
  );
}
