"use client";

import type { InboxItem } from "@noticedesk/shared/inbox";
import { Plus } from "lucide-react";
import { useState } from "react";

import { InboxRow } from "@/components/inbox/inbox-row";
import { OcrSidePanel } from "@/components/inbox/ocr-side-panel";
import { UploadDropzone } from "@/components/inbox/upload-dropzone";
import { Button } from "@/components/ui/button";
import { useInboxPoll } from "@/lib/hooks/use-inbox-poll";

export default function InboxPage() {
  const { data, loading, error, refresh } = useInboxPoll();
  const [uploadOpen, setUploadOpen] = useState(false);
  const [viewing, setViewing] = useState<InboxItem | null>(null);

  const items = data?.items ?? [];
  // Rejected and already-routed rows are listed but not "awaiting review".
  const total = data?.awaiting_review ?? 0;

  return (
    <main className="mx-auto max-w-5xl px-4 py-8 sm:px-6">
      <header className="flex items-center justify-between gap-4">
        <div>
          <p className="text-xs uppercase tracking-wider text-slate-500">Inbox</p>
          <h1 className="mt-1 text-2xl font-semibold text-navy">
            {total} document{total === 1 ? "" : "s"} awaiting review
          </h1>
        </div>
        <Button onClick={() => setUploadOpen(true)}>
          <Plus className="mr-1 h-4 w-4" aria-hidden /> Upload notice
        </Button>
      </header>

      {error ? (
        <p className="mt-4 rounded border border-red-200 bg-red-50 p-3 text-sm text-red-700">
          {error}
        </p>
      ) : null}

      {loading && items.length === 0 ? (
        <p className="mt-8 text-sm text-slate-500">Loading…</p>
      ) : items.length === 0 ? (
        <div className="mt-8 rounded-lg border border-dashed border-slate-300 bg-white p-12 text-center">
          <p className="font-medium text-navy">No documents yet</p>
          <p className="mt-1 text-sm text-slate-600">
            Drop a PDF, snap a photo on mobile, or forward your firm&rsquo;s
            <code className="mx-1 rounded bg-slate-100 px-1">notices+&lt;slug&gt;@noticedesk.in</code>
            address.
          </p>
        </div>
      ) : (
        <ul className="mt-6 space-y-3">
          {items.map((item) => (
            <InboxRow
              key={item.inbox_id}
              item={item}
              onView={setViewing}
              onResolved={() => {
                void refresh();
              }}
            />
          ))}
        </ul>
      )}

      <UploadDropzone
        open={uploadOpen}
        onClose={() => setUploadOpen(false)}
        onUploaded={() => {
          void refresh();
        }}
      />
      <OcrSidePanel item={viewing} onClose={() => setViewing(null)} />
    </main>
  );
}
