"use client";

import { useCallback, useEffect } from "react";
import { Gauge, RefreshCw } from "lucide-react";
import { KillSwitchPanel } from "@/components/operator/KillSwitchPanel";
import { MANDATE_UPDATED_EVENT } from "@/lib/mission/events";
import { useAutonomyStore } from "@/lib/stores/useAutonomyStore";
import { type MissionView, useMandateStore } from "@/lib/stores/useMandateStore";
import { useShadowStore } from "@/lib/stores/useShadowStore";
import { ApprovalQueue } from "./ApprovalQueue";
import { AutonomyEvals } from "./AutonomyEvals";
import ShadowPanel from "./ShadowPanel";
import { MandateCard } from "./MandateCard";
import { ProfilesMoved } from "./ProfilesMoved";
import { MissionBanners } from "./MissionBanners";
import { MissionHealth } from "./MissionHealth";
import { dispatchSwitchWorkspace } from "@/lib/navigation";

const VIEWS: { id: MissionView; label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "mandates", label: "Mandate editor" },
  { id: "autonomy", label: "Autonomy & Evals" },
];

let hydrated = false;

/**
 * Mission Control (blueprint §7.1): mandates, the approval queue with countdowns, halted and degraded
 * states, one PAPER kill switch; plus the Mandate editor and Autonomy & Evals views.
 */
export function MissionControl({ isActive }: { isActive: boolean }) {
  const { view, mandates, mandatesOk, approvals, status, loaded, error, refresh } = useMandateStore();

  useEffect(() => {
    if (!hydrated) {
      hydrated = true;
      void useMandateStore.persist.rehydrate();
      void useAutonomyStore.persist.rehydrate();
      void useShadowStore.persist.rehydrate();
    }
  }, []);

  // Shares the header's in-flight refresh (useMandateStore.refresh); a click or a mandate change asks
  // for data fetched after it
  const reload = useCallback(() => void refresh(), [refresh]);
  const reloadNow = useCallback(() => void refresh({ after: performance.now() }), [refresh]);

  useEffect(() => {
    if (!isActive) return;
    reload();
    const t = setInterval(reload, 5000);
    const onUpdated = (e: Event) => void refresh({ after: e.timeStamp });
    window.addEventListener(MANDATE_UPDATED_EVENT, onUpdated);
    return () => {
      clearInterval(t);
      window.removeEventListener(MANDATE_UPDATED_EVENT, onUpdated);
    };
  }, [isActive, reload, refresh]);

  return (
    <div data-testid="mission-control" className="flex h-full min-h-0 flex-col overflow-y-auto bg-zinc-950 p-4 sm:p-5 text-zinc-100">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
        <h2 className="flex items-center gap-2 font-mono text-xs font-bold uppercase tracking-widest text-zinc-200">
          <Gauge className="h-4 w-4 text-cyan-400" /> Mission Control · PAPER
        </h2>
        <div className="flex items-center gap-3">
          <div role="tablist" aria-label="Mission Control views" className="flex gap-1 rounded-xl border border-zinc-800 bg-zinc-900/80 p-0.5 font-mono text-2xs">
            {VIEWS.map((v) => (
              <button
                key={v.id}
                role="tab"
                aria-selected={view === v.id}
                onClick={() => dispatchSwitchWorkspace({ mode: "mission", view: v.id })}
                className={`rounded-lg px-2.5 py-1 font-bold transition-colors ${view === v.id ? "bg-cyan-950/80 text-cyan-300 border border-cyan-500/40 shadow-xs" : "text-zinc-400 hover:text-zinc-200"}`}
              >
                {v.label}
              </button>
            ))}
          </div>
          <button type="button" onClick={reloadNow} aria-label="Refresh Mission Control" className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-1.5 text-zinc-400 hover:text-zinc-200 transition-colors">
            <RefreshCw className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>

      <MissionBanners status={status} mandates={mandates} />
      {error && (
        <div role="alert" className="mt-2 flex flex-wrap items-center gap-3 font-mono text-2xs text-zinc-300">
          <span>{error}</span>
          {error.toLowerCase().includes("sign in") && (
            <button
              type="button"
              onClick={() => window.dispatchEvent(new CustomEvent("cryptonite:open-login"))}
              className="rounded-lg bg-cyan-600 hover:bg-cyan-500 px-3 py-1 font-mono text-2xs font-bold text-zinc-950 transition-colors"
            >
              Sign in as Operator
            </button>
          )}
        </div>
      )}

      <div className="mt-3 min-h-0 flex-1">
        {view === "overview" && <MissionHealth status={status} />}
        {view === "overview" && (
          <div className="mt-4 grid grid-cols-1 gap-4 xl:grid-cols-12">
            <div className="space-y-3 xl:col-span-7">
              {loaded && mandatesOk === false && mandates.length === 0 && (
                <p role="status" className="rounded-2xl border border-amber-800/60 bg-amber-950/20 p-4 font-mono text-2xs text-amber-200">
                  Mandates could not be loaded, so the AI's authority is unknown here, not zero.
                </p>
              )}
              {loaded && mandatesOk === true && mandates.length === 0 && (
                <p className="rounded-2xl border border-zinc-800 bg-zinc-900/60 p-4 font-mono text-2xs text-zinc-400">
                  No mandates. The AI cannot place any order until you create one in Auto-Trade; every mandate starts at L1 Copilot.
                </p>
              )}
              <div className="grid grid-cols-1 gap-3.5 md:grid-cols-2">
                {mandates.map((m) => (
                  <MandateCard key={m.id} mandate={m} onChanged={reload} />
                ))}
              </div>
              {status?.account && (
                <p className="font-mono text-2xs text-zinc-500" data-testid="account-backstop">
                  Account drawdown {status.account.drawdown_pct == null ? "—" : `${Number(status.account.drawdown_pct).toFixed(2)}%`} of the {status.account.backstop_pct}% backstop
                  (latches everything).
                </p>
              )}
            </div>
            <div className="space-y-3 xl:col-span-5">
              <ApprovalQueue approvals={approvals} onDecided={reload} />
              <KillSwitchPanel />
            </div>
          </div>
        )}
        {view === "mandates" && (
          <ProfilesMoved />
        )}
        {view === "autonomy" && (
          <div className="space-y-3">
            <AutonomyEvals mandates={mandates} />
            <ShadowPanel />
          </div>
        )}
      </div>
    </div>
  );
}
