"use client";

import { useCallback, useEffect, useState } from "react";

import { Sidebar } from "@/components/sidebar";
import { DemoBanner } from "@/components/shell/demo-banner";
import { Topbar } from "@/components/shell/topbar";
import { fetchInbox } from "@/lib/api";

const POLL_MS = 15000;

// Shell that polls the inbox count so the sidebar badge and the topbar bell
// stay current without each page reimplementing the polling. Cheap enough
// (one cheap COUNT-ish query every 15s) and avoids the worse problem of a
// stale "0 awaiting" sitting visible after a partner uploads something.
export function AppShell({ children }: { children: React.ReactNode }) {
  const [awaiting, setAwaiting] = useState(0);

  const tick = useCallback(async () => {
    try {
      const res = await fetchInbox();
      setAwaiting(res.awaiting_review);
    } catch {
      // Ignore — the next tick will retry.
    }
  }, []);

  useEffect(() => {
    void tick();
    const id = setInterval(tick, POLL_MS);
    return () => clearInterval(id);
  }, [tick]);

  return (
    <div className="flex min-h-screen">
      <Sidebar inboxAwaiting={awaiting} />
      <div className="flex min-h-screen flex-1 flex-col">
        <DemoBanner />
        <Topbar awaitingCount={awaiting} />
        <div className="flex-1">{children}</div>
      </div>
    </div>
  );
}
