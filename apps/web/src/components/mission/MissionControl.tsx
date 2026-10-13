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
import { MandateEditor } from "./MandateEditor";
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
  const { view, mandates, mandatesOk, approvals, status, loaded, error, refresh, selectedMandateId, selectMandate } = useMandateStore();

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
    <div data-testid="mission-control" className="flex h-full min-h-0 flex-col overflow-y-auto bg-zinc-950 p-4 text-zinc-100">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="flex items-center gap-2 font-mono text-xs font-bold uppercase tracking-widest text-zinc-300">
          <Gauge className="h-4 w-4 text-cyan-400" /> Mission Control · PAPER
        </h2>
        <div className="flex items-center gap-3">
          <div role="tablist" aria-label="Mission Control views" className="flex gap-1 font-mono text-2xs">
            {VIEWS.map((v) => (
              <button
                key={v.id}
                role="tab"
                aria-selected={view === v.id}
                onClick={() => dispatchSwitchWorkspace({ mode: "mission", view: v.id })}
                className={`rounded px-2 py-1 font-bold ${view === v.id ? "bg-cyan-900/50 text-cyan-300" : "text-zinc-400 hover:text-zinc-200"}`}
              >
                {v.label}
              </button>
            ))}
          </div>
          <button type="button" onClick={reloadNow} aria-label="Refresh Mission Control" className="text-zinc-500 hover:text-zinc-200">
            <RefreshCw className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>

      <MissionBanners status={status} />
      {error && (
        <p role="alert" className="mt-2 font-mono text-2xs text-zinc-400">
          {error}
        </p>
      )}

      <div className="mt-3 min-h-0 flex-1">
        {view === "overview" && <MissionHealth status={status} />}
        {view === "overview" && (
          <div className="mt-3 grid grid-cols-1 gap-4 xl:grid-cols-12">
            <div className="space-y-3 xl:col-span-7">
              {loaded && mandatesOk === false && mandates.length === 0 && (
                <p role="status" className="rounded border border-amber-800/60 p-3 font-mono text-2xs text-amber-200">
                  Mandates could not be loaded, so the AI's authority is unknown here, not zero.
                </p>
              )}
              {loaded && mandatesOk === true && mandates.length === 0 && (
                <p className="rounded border border-zinc-800 p-3 font-mono text-2xs text-zinc-500">
                  No mandates. The AI cannot place any order until you create one (Mandate editor); every mandate starts at L1 Copilot.
                </p>
              )}
              <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
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
          <MandateEditor mandates={mandates} selectedId={selectedMandateId} onSelect={selectMandate} onSaved={reload} />
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
