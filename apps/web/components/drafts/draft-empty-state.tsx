"use client";

import { useState } from "react";

import {
  DEFAULT_DRAFT_OPTIONS,
  DraftOptionsFields,
  type DraftOptions,
} from "@/components/drafts/draft-options";
import { DraftProgressCard } from "@/components/drafts/draft-progress-card";
import { Button } from "@/components/ui/button";

interface Props {
  busy: boolean;
  error: string | null;
  onGenerate: (opts: DraftOptions) => Promise<void>;
  // Rendered above the button (e.g. the rate-limit countdown notice).
  notice?: React.ReactNode;
}

export function DraftEmptyState({ busy, error, onGenerate, notice }: Props) {
  const [opts, setOpts] = useState<DraftOptions>(DEFAULT_DRAFT_OPTIONS);

  if (busy) {
    return <DraftProgressCard busy />;
  }

  return (
    <div className="rounded-md border border-slate-line bg-white px-8 py-10">
      <p className="mb-1 text-[10px] font-bold uppercase tracking-[0.14em] text-gold-dark">
        Draft reply
      </p>
      <h2 className="mb-2 font-serif text-[24px] font-semibold text-navy-deep">
        Generate the first draft
      </h2>
      <p className="mb-6 max-w-[640px] font-serif text-[14px] italic text-slate">
        The drafting agent reads the parsed notice, this registration&rsquo;s
        prior matters, attached documents, and produces a 15-section reply
        with verified citations. Sibling registrations are deliberately
        excluded so the factual matrix stays clean.
      </p>

      <DraftOptionsFields value={opts} onChange={setOpts} />

      {notice ? <div className="mb-4">{notice}</div> : null}

      {error ? (
        <p className="mb-4 rounded border border-alarm/30 bg-alarm-bg px-3 py-2 text-sm text-alarm">
          {error}
        </p>
      ) : null}

      <Button variant="gold" size="lg" disabled={busy} onClick={() => onGenerate(opts)}>
        Generate draft
      </Button>
    </div>
  );
}
