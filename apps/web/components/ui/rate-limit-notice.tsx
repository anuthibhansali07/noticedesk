"use client";

import { Clock } from "lucide-react";

interface Props {
  countdown: number;
  what: string; // "triage" / "draft"
  onRetryNow: () => void;
  onCancel: () => void;
}

// Friendly replacement for the raw "openai call failed: Error code: 429"
// banner: explains the AI provider is throttling and counts down to an
// automatic retry.
export function RateLimitNotice({ countdown, what, onRetryNow, onCancel }: Props) {
  return (
    <div
      role="status"
      className="flex flex-wrap items-center gap-3 rounded border border-gold/40 bg-gold/10 px-3 py-2 text-[12.5px] text-ink"
    >
      <Clock className="h-4 w-4 shrink-0 text-gold-dark" aria-hidden />
      <span className="flex-1">
        AI service busy (provider rate limit). Retrying the {what} automatically in{" "}
        <strong className="font-mono">{countdown}s</strong>…
      </span>
      <button
        type="button"
        onClick={onRetryNow}
        className="text-[11.5px] font-semibold uppercase tracking-[0.06em] text-navy hover:text-ink"
      >
        Retry now
      </button>
      <button
        type="button"
        onClick={onCancel}
        className="text-[11.5px] font-semibold uppercase tracking-[0.06em] text-slate hover:text-alarm"
      >
        Cancel
      </button>
    </div>
  );
}
