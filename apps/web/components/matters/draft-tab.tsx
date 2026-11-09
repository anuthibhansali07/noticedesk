"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { CompareVersionsModal } from "@/components/drafts/compare-versions-modal";
import { DraftEmptyState } from "@/components/drafts/draft-empty-state";
import { type DraftOptions } from "@/components/drafts/draft-options";
import { DraftPane } from "@/components/drafts/draft-pane";
import { DraftProgressCard } from "@/components/drafts/draft-progress-card";
import { DraftToolbar } from "@/components/drafts/draft-toolbar";
import { RegenerateDialog } from "@/components/drafts/regenerate-dialog";
import { VerificationPanel } from "@/components/drafts/verification-panel";
import { RateLimitNotice } from "@/components/ui/rate-limit-notice";
import { useToast } from "@/components/ui/toast";
import {
  fetchDraft,
  fetchDraftVersions,
  fetchTriage,
  generateDraft,
  type DraftDetail,
  type DraftSummary,
  type DraftTone,
  type TriagePayload,
} from "@/lib/api";
import { isRateLimited, useRateLimitRetry } from "@/lib/hooks/use-rate-limit-retry";

interface Props {
  noticeId: string;
  matterId: string;
}

export function DraftTab({ noticeId, matterId }: Props) {
  const { toast } = useToast();
  const [versions, setVersions] = useState<DraftSummary[]>([]);
  const [draft, setDraft] = useState<DraftDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [compareOpen, setCompareOpen] = useState(false);
  const [triage, setTriage] = useState<TriagePayload | null>(null);
  const [regenOpen, setRegenOpen] = useState(false);
  const rateLimit = useRateLimitRetry();

  const loadVersions = useCallback(async (): Promise<DraftSummary[]> => {
    const res = await fetchDraftVersions(matterId);
    setVersions(res.drafts);
    return res.drafts;
  }, [matterId]);

  const loadDraft = useCallback(async (draftId: string) => {
    const d = await fetchDraft(draftId);
    setDraft(d);
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [list, t] = await Promise.all([
          loadVersions(),
          fetchTriage(noticeId).catch(() => null),
        ]);
        if (cancelled) return;
        if (t) setTriage(t);
        if (list.length > 0) {
          await loadDraft(list[0]!.draft_id);
        }
      } catch (e: unknown) {
        if (!cancelled) setError(e instanceof Error ? e.message : "failed to load draft");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [loadVersions, loadDraft, noticeId]);

  async function handleGenerate(opts: DraftOptions, attempt = 0) {
    rateLimit.cancel();
    setBusy(true);
    setError(null);
    try {
      const res = await generateDraft(noticeId, {
        tone: opts.tone,
        partner_instructions: opts.instructions,
        include_cross_registration: opts.includeCross,
        format_profile: opts.formatProfile,
      });
      const list = await loadVersions();
      await loadDraft(res.draft_id);
      toast(
        `Draft v${res.version} generated · ${res.citation_summary.verified ?? 0} verified, ${res.citation_summary.stripped ?? 0} stripped`,
        "success",
      );
      void list;
    } catch (e: unknown) {
      if (isRateLimited(e) && attempt === 0) {
        // Provider throttled us: count down and retry once automatically.
        rateLimit.schedule(e.retryAfterSeconds, () => void handleGenerate(opts, attempt + 1));
      } else {
        setError(e instanceof Error ? e.message : "draft failed");
      }
    } finally {
      setBusy(false);
    }
  }

  // Seed the regenerate dialog with the current version's settings.
  const regenInitial = useMemo<DraftOptions>(
    () => ({
      tone: ((draft?.tone as DraftTone) || "formal"),
      formatProfile: draft?.format_profile ?? "full_submission",
      instructions: "",
      includeCross: false,
    }),
    [draft?.tone, draft?.format_profile],
  );

  const rateLimitNotice =
    rateLimit.countdown !== null ? (
      <RateLimitNotice
        countdown={rateLimit.countdown}
        what="draft"
        onRetryNow={rateLimit.retryNow}
        onCancel={() => {
          rateLimit.cancel();
          setError("Draft generation cancelled after the AI provider rate limit. Try again when ready.");
        }}
      />
    ) : null;

  function handleExport(mode: "filing" | "client" | "internal") {
    if (!draft) return;
    // Sprint 5 slice 7 wires the actual /v1/drafts/{id}/export endpoint.
    window.open(`/api/drafts/${draft.draft_id}/export?mode=${mode}`, "_blank");
  }

  if (loading) {
    return <p className="py-12 text-center font-serif italic text-slate">Loading draft…</p>;
  }

  const triageBanner = renderTriageBanner(triage);

  if (versions.length === 0 || !draft) {
    return (
      <div className="space-y-4">
        {triageBanner}
        <DraftEmptyState
          busy={busy}
          error={error}
          onGenerate={(opts) => handleGenerate(opts)}
          notice={rateLimitNotice}
        />
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {triageBanner}
      <div className="overflow-hidden rounded-md border border-slate-line bg-white">
      <DraftToolbar
        current={draft}
        versions={versions}
        onPickVersion={(id) => void loadDraft(id)}
        onCompare={() => setCompareOpen(true)}
        onRegenerate={() => setRegenOpen(true)}
        onExport={handleExport}
        busy={busy || rateLimit.countdown !== null}
      />
      {rateLimitNotice ? <div className="mx-6 mt-4">{rateLimitNotice}</div> : null}
      {busy ? (
        // Regenerate path: swap the split-pane for the same progress card
        // the first-generation flow shows. Title makes it clear the
        // existing draft is being replaced, not lost — the previous version
        // is still in the version dropdown when this completes.
        <div className="px-6 py-6">
          <DraftProgressCard
            busy
            eyebrow="Regenerating draft"
            title={`Replacing v${draft.version} with a fresh generation — previous version stays in history`}
          />
        </div>
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-[1.6fr_1fr]" style={{ minHeight: 600 }}>
          <DraftPane
            draftId={draft.draft_id}
            sections={draft.sections}
            internalPartnerNote={draft.internal_partner_note}
            onSaved={(newId) => {
              void (async () => {
                await loadVersions();
                await loadDraft(newId);
              })();
            }}
          />
          <VerificationPanel draft={draft} />
        </div>
      )}

      {error ? (
        <p className="mx-6 mb-4 rounded border border-alarm/30 bg-alarm-bg px-3 py-2 text-sm text-alarm">
          {error}
        </p>
      ) : null}

      <RegenerateDialog
        open={regenOpen}
        currentVersion={versions[0]?.version ?? draft.version}
        initial={regenInitial}
        onClose={() => setRegenOpen(false)}
        onSubmit={(opts) => {
          // Same handler as first-generation; the body swaps to the
          // progress card while it runs.
          setRegenOpen(false);
          void handleGenerate(opts);
        }}
      />

      <CompareVersionsModal
        open={compareOpen}
        versions={versions}
        currentDraftId={draft.draft_id}
        onClose={() => setCompareOpen(false)}
      />
      </div>
    </div>
  );
}

function renderTriageBanner(triage: TriagePayload | null) {
  if (!triage) return null;
  if (triage.status === "not_started") {
    return (
      <div className="rounded-md border border-slate-line bg-paper-tint px-4 py-3 text-[12.5px] text-ink">
        <strong>Triage not started.</strong> Run triage from the Triage tab
        first to give the drafter a document checklist — partners who skip
        triage tend to see more <code className="rounded bg-white px-1.5 py-0.5 text-[11px]">[ASSUMED]</code>{" "}
        markers in the result. You can still generate now.
      </div>
    );
  }
  const pendingRequired = triage.checklist.filter(
    (c) => c.is_required && c.status === "pending",
  );
  if (pendingRequired.length === 0) return null;
  return (
    <div className="rounded-md border border-gold/40 bg-gold/10 px-4 py-3 text-[12.5px] text-ink">
      <strong>{pendingRequired.length}</strong> required document
      {pendingRequired.length === 1 ? "" : "s"} from the triage checklist are
      still pending. You can still generate a first-cut draft; the drafter
      will mark those facts as <code className="rounded bg-white px-1.5 py-0.5 text-[11px]">[DOCUMENT REQUESTED]</code>{" "}
      so they&rsquo;re easy to spot.
    </div>
  );
}
