"use client";

import { Inbox, Plus } from "lucide-react";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";

import { FilterBar } from "@/components/dashboard/filter-bar";
import { NoticeTable } from "@/components/dashboard/notice-table";
import { StatusTabs } from "@/components/dashboard/status-tabs";
import { Button } from "@/components/ui/button";
import {
  fetchClients,
  fetchInbox,
  fetchNotices,
  fetchStatusCounts,
  type ClientSummary,
  type NoticeListResponse,
  type StatusCounts,
} from "@/lib/api";

export default function DashboardPage() {
  const router = useRouter();
  const [activeStatus, setActiveStatus] = useState<string>("issued");
  const [filterLaw, setFilterLaw] = useState<"GST" | "IT" | "">("");
  const [filterClientId, setFilterClientId] = useState<string>("");
  const [filterStateCode, setFilterStateCode] = useState<string>("");

  const [counts, setCounts] = useState<StatusCounts | null>(null);
  const [data, setData] = useState<NoticeListResponse | null>(null);
  const [clients, setClients] = useState<ClientSummary[]>([]);
  const [inboxAwaiting, setInboxAwaiting] = useState<number>(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadCounts = useCallback(async () => {
    try {
      const [c, cs, ib] = await Promise.all([
        fetchStatusCounts(),
        fetchClients(),
        fetchInbox(),
      ]);
      setCounts(c);
      setClients(cs.clients);
      setInboxAwaiting(ib.awaiting_review);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "failed to load dashboard");
    }
  }, []);

  const loadNotices = useCallback(async () => {
    try {
      const filters: Parameters<typeof fetchNotices>[0] = {
        status: activeStatus,
        page_size: 200,
      };
      if (filterLaw) filters.law = filterLaw;
      if (filterClientId) filters.client_id = filterClientId;
      if (filterStateCode) filters.state_code = filterStateCode;
      const res = await fetchNotices(filters);
      setData(res);
      setError(null);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "failed to load notices");
    } finally {
      setLoading(false);
    }
  }, [activeStatus, filterLaw, filterClientId, filterStateCode]);

  useEffect(() => {
    void loadCounts();
  }, [loadCounts]);

  useEffect(() => {
    setLoading(true);
    void loadNotices();
  }, [loadNotices]);

  const decisionsToday = useMemo(() => {
    if (!counts) return 0;
    return (counts.due ?? 0) + (counts.due_date_over ?? 0);
  }, [counts]);

  const today = useMemo(
    () =>
      new Date().toLocaleDateString("en-IN", {
        weekday: "long",
        day: "2-digit",
        month: "long",
        year: "numeric",
      }),
    [],
  );

  // Greeting branch: morning / afternoon / evening based on local hour.
  const greeting = useMemo(() => {
    const h = new Date().getHours();
    if (h < 12) return "Good morning";
    if (h < 17) return "Good afternoon";
    return "Good evening";
  }, []);

  return (
    <main className="mx-auto max-w-[1400px] px-10 py-8 pb-16">
      <header className="mb-7 flex flex-wrap items-end justify-between gap-6">
        <div>
          <p className="mb-2 text-[11px] font-medium uppercase tracking-[0.08em] text-slate">
            {today}
          </p>
          <h1 className="font-serif text-[36px] font-medium leading-tight tracking-tight text-navy-deep">
            {greeting}, Rohan
          </h1>
          <p className="mt-1 font-serif text-[17px] italic text-slate">
            You have {decisionsToday}{" "}
            {decisionsToday === 1 ? "decision" : "decisions"} today across{" "}
            {clients.length} {clients.length === 1 ? "client" : "clients"}.
          </p>
        </div>
        <div className="flex items-center gap-2.5">
          <Button variant="ghost" size="sm" onClick={() => router.push("/inbox")}>
            <Inbox className="mr-1.5 h-3.5 w-3.5" aria-hidden />
            Inbox · {inboxAwaiting} awaiting
          </Button>
          <Button
            variant="gold"
            size="sm"
            onClick={() => router.push("/inbox?upload=1")}
          >
            <Plus className="mr-1.5 h-3.5 w-3.5" aria-hidden />
            Upload notice
          </Button>
        </div>
      </header>

      <StatusTabs counts={counts ?? {}} active={activeStatus} onChange={setActiveStatus} />

      <FilterBar
        clients={clients}
        law={filterLaw}
        clientId={filterClientId}
        stateCode={filterStateCode}
        onLawChange={setFilterLaw}
        onClientChange={setFilterClientId}
        onStateChange={setFilterStateCode}
        resultCount={data?.total ?? 0}
      />

      {error ? (
        <p className="mb-4 rounded border border-alarm/30 bg-alarm-bg px-3 py-2 text-sm text-alarm">
          {error}
        </p>
      ) : null}

      {loading && !data ? (
        <p className="py-10 text-center font-serif italic text-slate">Loading notices…</p>
      ) : (
        <NoticeTable notices={data?.notices ?? []} />
      )}
    </main>
  );
}
