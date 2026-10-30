"use client";

import { useCallback, useEffect, useRef, type KeyboardEvent } from "react";
import { errorCopy, MODE_LABEL } from "@/lib/autotrade/copy";
import { useAutoTradeStore, type ProfileTab } from "@/lib/stores/useAutoTradeStore";
import { useVisiblePoll } from "../AutoTradeWorkspace";
import { btnSecondary } from "../Dialogs";
import { ProfileActions } from "../ProfileActions";
import { HealthBlock, modeLine, PnlScale, StatusPill } from "../ProfileCard";
import { PerformanceTab } from "./PerformanceTab";
import { PositionsTab } from "./PositionsTab";
import { SettingsTab } from "./SettingsTab";
import { TimelineTab } from "./TimelineTab";

const DETAIL_POLL_MS = 5_000;
const TABS: { id: ProfileTab; label: string }[] = [
  { id: "timeline", label: "Timeline" },
  { id: "performance", label: "Performance" },
  { id: "positions", label: "Positions" },
  { id: "settings", label: "Settings" },
];

export function ProfileDetail({ id, tab, onTab, onBack }: { id: string; tab: ProfileTab; onTab: (t: ProfileTab) => void; onBack: () => void }) {
  const detail = useAutoTradeStore((s) => s.detailById[id]);
  const listed = useAutoTradeStore((s) => s.profiles.find((p) => p.id === id) ?? null);
  const summary = useAutoTradeStore((s) => s.summaryById[id]?.data ?? null);
  const loadDetail = useAutoTradeStore((s) => s.loadDetail);
  const loadSummary = useAutoTradeStore((s) => s.loadSummary);
  const tabRefs = useRef<Record<string, HTMLButtonElement | null>>({});
  const profile = detail?.data ?? listed;

  const refresh = useCallback(() => {
    void loadDetail(id);
    void loadSummary(id);
  }, [id, loadDetail, loadSummary]);
  // The header polls every 5 s while visible; the Settings tab does not, so an edit in progress is never overwritten
  useVisiblePoll(refresh, DETAIL_POLL_MS, tab !== "settings");
  useEffect(() => {
    if (tab === "settings") refresh();
  }, [tab, refresh]);

  const onKey = (e: KeyboardEvent<HTMLButtonElement>, i: number) => {
    const n = TABS.length;
    const to = e.key === "ArrowRight" ? (i + 1) % n : e.key === "ArrowLeft" ? (i - 1 + n) % n : e.key === "Home" ? 0 : e.key === "End" ? n - 1 : -1;
    if (to < 0) return;
    e.preventDefault();
    onTab(TABS[to].id);
    tabRefs.current[TABS[to].id]?.focus();
  };

  if (!profile) {
    if (detail?.status === "error")
      return (
        <div role="alert" className="space-y-3">
          <h2 tabIndex={-1} className="text-lg font-bold text-zinc-100 outline-none">
            Profile unavailable
          </h2>
          <p className="text-sm text-zinc-300">{detail.error?.status === 404 ? "This profile doesn't exist or was archived." : errorCopy(detail.error!)}</p>
          <div className="flex gap-2">
            <button type="button" className={btnSecondary} onClick={refresh}>
              Retry
            </button>
            <button type="button" className={btnSecondary} onClick={onBack}>
              Back to profiles
            </button>
          </div>
        </div>
      );
    return (
      <div role="status" className="space-y-3">
        <span className="sr-only">Loading profile…</span>
        <div className="h-24 animate-pulse rounded-xl bg-zinc-900 motion-reduce:animate-none" aria-hidden="true" />
        <div className="h-64 animate-pulse rounded-xl bg-zinc-900 motion-reduce:animate-none" aria-hidden="true" />
      </div>
    );
  }

  return (
    <section aria-labelledby="profile-heading" className="space-y-4">
      <button type="button" onClick={onBack} className="text-xs text-zinc-400 underline hover:text-zinc-200">
        ← All profiles
      </button>
      {detail?.status === "error" && detail.data && (
        <p role="status" className="rounded-lg border border-amber-800/60 bg-amber-950/20 px-3 py-2 text-xs text-amber-200">
          Couldn&apos;t refresh this profile. Showing data from {detail.updatedAt ? new Date(detail.updatedAt).toLocaleTimeString() : "earlier"}.{" "}
          <button type="button" className="underline" onClick={refresh}>
            Retry
          </button>
        </p>
      )}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
        <header className="space-y-2 rounded-xl border border-zinc-800 bg-zinc-950 p-4 lg:col-span-8">
          <div className="flex flex-wrap items-center gap-2">
            <h2 id="profile-heading" tabIndex={-1} className="mr-auto text-lg font-bold text-zinc-100 outline-none">
              {profile.name}
            </h2>
            <StatusPill status={profile.status} />
            <span className="rounded border border-zinc-700 px-1.5 py-0.5 text-2xs font-bold text-zinc-300">{profile.environment}</span>
          </div>
          <p className="text-sm text-zinc-300">{modeLine(profile, summary)}</p>
          {profile.dry_run && <p className="text-xs text-cyan-300">Dry run: orders are simulated.</p>}
          <ProfileActions profile={profile} />
        </header>
        <aside aria-label="Today and health" className="space-y-3 rounded-xl border border-zinc-800 bg-zinc-950 p-4 lg:col-span-4">
          <PnlScale summary={summary} name={profile.name} />
          <HealthBlock summary={summary} />
          <p className="text-xs text-zinc-400">
            {MODE_LABEL[profile.execution_mode]} · auto-trade switch {summary?.master_switch ?? "unknown"} · {profile.positions?.length ?? 0} open position
            {(profile.positions?.length ?? 0) === 1 ? "" : "s"}
          </p>
        </aside>
      </div>

      <div role="tablist" aria-label="Profile sections" className="flex gap-1 overflow-x-auto border-b border-zinc-800">
        {TABS.map((t, i) => (
          <button
            key={t.id}
            ref={(el) => {
              tabRefs.current[t.id] = el;
            }}
            id={`profile-tab-${t.id}`}
            role="tab"
            type="button"
            aria-selected={tab === t.id}
            aria-controls={`profile-panel-${t.id}`}
            tabIndex={tab === t.id ? 0 : -1}
            onClick={() => onTab(t.id)}
            onKeyDown={(e) => onKey(e, i)}
            className={`shrink-0 border-b-2 px-3 py-2 text-sm ${tab === t.id ? "border-cyan-400 text-zinc-100" : "border-transparent text-zinc-400 hover:text-zinc-200"}`}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div id={`profile-panel-${tab}`} role="tabpanel" aria-labelledby={`profile-tab-${tab}`} tabIndex={0} className="outline-none">
        {tab === "timeline" && <TimelineTab id={id} />}
        {tab === "performance" && <PerformanceTab id={id} />}
        {tab === "positions" && <PositionsTab profile={profile} status={detail?.status ?? "idle"} onRetry={refresh} />}
        {tab === "settings" && <SettingsTab profile={profile} />}
      </div>
    </section>
  );
}
