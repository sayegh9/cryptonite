"use client";

import { useCallback, useEffect, useRef } from "react";
import { useSearchParams } from "next/navigation";
import { MANDATE_UPDATED_EVENT } from "@/lib/mission/events";
import { useAutoTradeStore, type AutoTradeView, type ProfileTab } from "@/lib/stores/useAutoTradeStore";
import { ProfileDetail } from "./detail/ProfileDetail";
import { ProfileList } from "./ProfileList";
import { ProfileWizard } from "./wizard/ProfileWizard";

const LIST_POLL_MS = 10_000;
const TABS: ProfileTab[] = ["timeline", "performance", "positions", "settings"];
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Calls `fn` now and every `ms` while the page is visible; never while hidden. */
export function useVisiblePoll(fn: () => void, ms: number, enabled = true) {
  const ref = useRef(fn);
  ref.current = fn;
  useEffect(() => {
    if (!enabled) return;
    let timer: ReturnType<typeof setInterval> | null = null;
    const start = () => {
      if (timer || document.visibilityState !== "visible") return;
      ref.current();
      timer = setInterval(() => ref.current(), ms);
    };
    const stop = () => {
      if (timer) clearInterval(timer);
      timer = null;
    };
    const onVis = () => (document.visibilityState === "visible" ? start() : stop());
    start();
    document.addEventListener("visibilitychange", onVis);
    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [ms, enabled]);
}

/**
 * The Auto-Trade workspace: list, create wizard and profile detail. The URL is the source of truth
 * (`?ws=autotrade&view=list|new|profile&id=<uuid>&tab=…`; `?ws=autotrade&id=<uuid>` opens a profile).
 */
export function AutoTradeWorkspace() {
  const params = useSearchParams();
  const id = params.get("id");
  const profileId = id && UUID.test(id) ? id : null;
  const rawView = params.get("view");
  const view: AutoTradeView = rawView === "new" ? "new" : profileId && (rawView === "profile" || !rawView) ? "profile" : "list";
  const tabParam = params.get("tab") as ProfileTab | null;
  const tab: ProfileTab = tabParam && TABS.includes(tabParam) ? tabParam : "timeline";

  const loadProfiles = useAutoTradeStore((s) => s.loadProfiles);
  const loadSummary = useAutoTradeStore((s) => s.loadSummary);
  const headingRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    void useAutoTradeStore.persist.rehydrate();
  }, []);
  useEffect(() => {
    const s = useAutoTradeStore.getState();
    s.setView(view);
    s.selectProfile(profileId);
    s.setActiveTab(tab);
  }, [view, profileId, tab]);

  const go = useCallback((next: { view: AutoTradeView; id?: string | null; tab?: ProfileTab }) => {
    const q = new URLSearchParams({ ws: "autotrade", view: next.view });
    if (next.id) q.set("id", next.id);
    if (next.tab && next.tab !== "timeline") q.set("tab", next.tab);
    const query = `?${q.toString()}`;
    if (query !== window.location.search) window.history.pushState(null, "", query);
  }, []);

  // The list (and each card's summary) refreshes every 10 s while visible; other workspaces' writes refresh it at once
  const refreshList = useCallback(async () => {
    await loadProfiles();
    for (const p of useAutoTradeStore.getState().profiles) void loadSummary(p.id);
  }, [loadProfiles, loadSummary]);
  useVisiblePoll(() => void refreshList(), LIST_POLL_MS, view === "list");
  useEffect(() => {
    const onUpdate = () => void refreshList();
    window.addEventListener(MANDATE_UPDATED_EVENT, onUpdate);
    return () => window.removeEventListener(MANDATE_UPDATED_EVENT, onUpdate);
  }, [refreshList]);

  // Focus the new view's heading after an in-workspace navigation (not on polling)
  const lastView = useRef(view);
  useEffect(() => {
    if (lastView.current === view) return;
    lastView.current = view;
    headingRef.current?.querySelector<HTMLElement>("h2")?.focus();
  }, [view]);

  return (
    <div ref={headingRef} className="mx-auto w-full max-w-[1400px] px-4 py-4 sm:px-6" data-testid="autotrade-workspace">
      {view === "list" && <ProfileList onCreate={() => go({ view: "new" })} onOpen={(pid) => go({ view: "profile", id: pid })} />}
      {view === "new" && (
        <ProfileWizard
          onCancel={() => go({ view: "list" })}
          onCreated={(pid) => go({ view: "profile", id: pid })}
          onOpenProfile={(pid) => go({ view: "profile", id: pid })}
        />
      )}
      {view === "profile" && profileId && (
        <ProfileDetail id={profileId} tab={tab} onTab={(t) => go({ view: "profile", id: profileId, tab: t })} onBack={() => go({ view: "list" })} />
      )}
    </div>
  );
}
