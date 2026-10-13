"use client";

import { useId, useState } from "react";
import { fetchApi } from "@/lib/api";
import { useKillSwitch, type KillSwitchEnvironment, type KillSwitchView } from "@/lib/stores/useKillSwitchStore";
import { useSlowFlag } from "@/lib/hooks/useSlowFlag";
import { AlertTriangle, Power, ShieldAlert, CheckCircle, RefreshCcw, HelpCircle } from "lucide-react";

const TRIP_LABEL: Record<KillSwitchEnvironment, string> = {
  PAPER: "TRIP KILL-SWITCH",
  DEMO: "TRIP DEMO KILL-SWITCH",
};

/** Each environment wears its own colour, so a PAPER and a DEMO panel are never mistaken for each other */
const ENV_PILL: Record<KillSwitchEnvironment, string> = {
  PAPER: "border-cyan-800/60 bg-cyan-950/40 text-cyan-300",
  DEMO: "border-violet-800/60 bg-violet-950/40 text-violet-300",
};

/** Kill switch for one (environment, account). DEMO targets the demo executor; PAPER the paper engine. */
export function KillSwitchPanel({
  environment = "PAPER",
  accountId = "PAPER-ACCOUNT-001",
}: { environment?: KillSwitchEnvironment; accountId?: string } = {}) {
  const ks = useKillSwitch(environment, accountId);
  const view = ks.view;
  const slow = useSlowFlag(view === "checking");
  const [actionLoading, setActionLoading] = useState(false);
  const [actionError, setActionError] = useState("");

  // Reset flow state
  const [showReset, setShowReset] = useState(false);
  const [stepUpPassword, setStepUpPassword] = useState("");
  const [resetReason, setResetReason] = useState("");
  const reasonId = useId();
  const passwordId = useId();

  const handleTrip = async () => {
    if (!confirm(`Trip the ${environment} kill-switch? All ${environment} trading halts until an operator resets it.`)) return;
    setActionLoading(true);
    setActionError("");
    try {
      await fetchApi("/control/kill-switch/trip", {
        method: "POST",
        headers: { "X-CSRF-Token": localStorage.getItem("csrf_token") || "" },
        body: JSON.stringify({ reason: "Manual operator emergency trip via UI", environment, account_id: accountId }),
      });
      await ks.refresh();
    } catch (err: any) {
      setActionError(err.message || "The trip request failed. Check the status below before assuming trading is halted.");
    } finally {
      setActionLoading(false);
    }
  };

  const handleReset = async (e: React.FormEvent) => {
    e.preventDefault();
    setActionLoading(true);
    setActionError("");
    try {
      await fetchApi("/control/kill-switch/reset", {
        method: "POST",
        headers: { "X-CSRF-Token": localStorage.getItem("csrf_token") || "" },
        body: JSON.stringify({
          step_up_password: stepUpPassword,
          reason: resetReason,
          environment,
          account_id: accountId,
        }),
      });
      alert(
        environment === "PAPER"
          ? "Reset requested. The paper engine resets only when no orders or positions are open."
          : "Reset requested successfully! The executor daemon will evaluate the 7-point checklist."
      );
      setShowReset(false);
      setStepUpPassword("");
      setResetReason("");
      await ks.refresh();
    } catch (err: any) {
      setActionError(err.message || "The reset request failed.");
    } finally {
      setActionLoading(false);
    }
  };

  // Trip stays reachable when the status is unknown: halting is the safe direction. It is off only while
  // there is no operator session to send it with, or while the switch is already latched.
  const tripDisabled = actionLoading || view === "latched" || view === "checking" || view === "signed-out";
  const lastConfirmed = ks.updatedAt ? new Date(ks.updatedAt).toLocaleTimeString() : null;

  return (
    <div className="bg-zinc-950 border border-white/5 rounded p-6" data-testid={`kill-switch-${environment.toLowerCase()}`}>
      <div className="flex items-center justify-between mb-6">
        <div className="min-w-0">
          <h2 className="text-sm font-bold flex items-center space-x-2 uppercase tracking-wider text-zinc-200">
            <Power className="w-4 h-4 text-cyan-400" aria-hidden="true" />
            <span>Execution Kill-Switch</span>
            <span className={`rounded border px-1.5 py-0.5 font-mono text-2xs ${ENV_PILL[environment]}`}>{environment}</span>
          </h2>
          <p className="mt-1 font-mono text-2xs text-zinc-500">{accountId}</p>
        </div>
        <div className="flex items-center space-x-2">
          <span className="text-2xs font-mono text-zinc-500">GEN: {ks.status?.latch_generation ?? "—"}</span>
          <button
            type="button"
            onClick={() => void ks.refresh()}
            disabled={ks.pending || view === "signed-out"}
            aria-label="Refresh kill switch status"
            title="Refresh kill switch status"
            className="inline-flex h-7 w-7 items-center justify-center rounded hover:text-cyan-400 text-zinc-500 transition-colors disabled:opacity-40"
          >
            <RefreshCcw className={`w-3.5 h-3.5 ${ks.pending ? "animate-spin" : ""}`} aria-hidden="true" />
          </button>
        </div>
      </div>

      {actionError && (
        <div role="alert" className="bg-rose-950/50 text-rose-300 text-2xs font-mono px-3 py-2 rounded mb-4">
          {actionError}
        </div>
      )}

      <StatusBlock view={view} slow={slow} error={ks.error} lastConfirmed={lastConfirmed} onRetry={() => void ks.refresh()} />

      {/* Primary Control */}
      {!showReset ? (
        <div className="space-y-3">
          <button
            type="button"
            onClick={handleTrip}
            disabled={tripDisabled}
            className={`w-full py-3 rounded font-black text-sm tracking-widest uppercase transition-colors ${
              view === "latched" || view === "signed-out" || view === "checking"
                ? "bg-zinc-900 text-zinc-500 cursor-not-allowed border border-white/5"
                : environment === "DEMO"
                  ? "border border-rose-600 text-rose-300 hover:bg-rose-950/60"
                  : "bg-rose-600 hover:bg-rose-500 text-white shadow-[0_0_15px_rgba(225,29,72,0.3)]"
            }`}
          >
            {actionLoading ? "PROCESSING..." : TRIP_LABEL[environment]}
          </button>
          {tripDisabled && !actionLoading && (
            <p className="text-2xs font-mono text-zinc-500 text-center" role="note">
              {view === "latched"
                ? "Already latched: trading is halted. Reset below once the cause is cleared."
                : view === "signed-out"
                  ? "Sign in as operator to use the kill switch."
                  : "Checking the executor before the switch can be used."}
            </p>
          )}

          {view === "latched" && (
            <button
              type="button"
              onClick={() => setShowReset(true)}
              className="w-full py-2 bg-zinc-900 hover:bg-zinc-800 text-zinc-300 rounded font-bold uppercase transition-colors text-xs border border-white/5"
            >
              Initiate Secure Reset Protocol
            </button>
          )}
        </div>
      ) : (
        <div className="bg-zinc-900/50 border border-amber-900/30 p-4 rounded">
          <h3 className="text-amber-500 font-bold text-xs mb-4 flex items-center space-x-2 uppercase tracking-wider">
            <AlertTriangle className="w-3.5 h-3.5" aria-hidden="true" />
            <span>Authoritative Reset</span>
          </h3>
          <form onSubmit={handleReset} className="space-y-3">
            <div>
              <label htmlFor={reasonId} className="block text-2xs font-mono text-zinc-400 mb-1.5 font-bold uppercase tracking-wider">
                Audit Reason (Min 5 chars)
              </label>
              <input
                id={reasonId}
                type="text"
                className="w-full bg-zinc-950 border border-white/5 rounded px-3 py-2 text-zinc-200 focus:border-amber-500 focus:outline-none text-xs transition-colors"
                value={resetReason}
                onChange={(e) => setResetReason(e.target.value)}
                required
                minLength={5}
              />
            </div>
            <div>
              <label htmlFor={passwordId} className="block text-2xs font-mono text-zinc-400 mb-1.5 font-bold uppercase tracking-wider">
                Step-Up Password
              </label>
              <input
                id={passwordId}
                type="password"
                autoComplete="current-password"
                className="w-full bg-zinc-950 border border-white/5 rounded px-3 py-2 text-zinc-200 focus:border-amber-500 focus:outline-none text-xs transition-colors"
                value={stepUpPassword}
                onChange={(e) => setStepUpPassword(e.target.value)}
                required
              />
            </div>
            <div className="flex space-x-2 pt-2">
              <button
                type="button"
                onClick={() => setShowReset(false)}
                className="flex-1 bg-zinc-900 hover:bg-zinc-800 border border-white/5 text-zinc-300 py-2 rounded font-bold text-2xs uppercase tracking-wider transition-colors"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={actionLoading}
                className="flex-1 bg-amber-600 hover:bg-amber-500 text-white py-2 rounded font-bold text-2xs uppercase tracking-wider transition-colors"
              >
                Request Reset
              </button>
            </div>
          </form>
        </div>
      )}
    </div>
  );
}

/** One block per state. Only a confirmed answer is ever shown as ARMED or LATCHED. */
function StatusBlock({
  view,
  slow,
  error,
  lastConfirmed,
  onRetry,
}: {
  view: KillSwitchView;
  slow: boolean;
  error: string | null;
  lastConfirmed: string | null;
  onRetry: () => void;
}) {
  const base = "flex flex-col items-center justify-center p-6 border rounded mb-6 text-center";

  if (view === "armed") {
    return (
      <div className={`${base} border-emerald-900/50 bg-emerald-950/10 text-emerald-400`} role="status">
        <CheckCircle className="w-12 h-12 mb-2" aria-hidden="true" />
        <span className="text-xl font-black tracking-widest">ARMED</span>
        <span className="text-2xs font-mono mt-1 text-zinc-500">EXECUTOR OPERATIONAL</span>
      </div>
    );
  }

  if (view === "latched") {
    return (
      <div className={`${base} border-rose-900/60 bg-rose-950/20 text-rose-400`} role="status">
        <ShieldAlert className="w-12 h-12 mb-2" aria-hidden="true" />
        <span className="text-xl font-black tracking-widest">LATCHED</span>
        <span className="text-2xs font-mono mt-1 text-zinc-400">TRADING HALTED</span>
      </div>
    );
  }

  if (view === "checking") {
    return (
      <div className={`${base} border-dashed border-zinc-700 text-zinc-400`} role="status">
        <RefreshCcw className="w-8 h-8 mb-2 animate-spin text-zinc-500" aria-hidden="true" />
        <span className="text-sm font-mono">{slow ? "Still checking the executor…" : "Checking kill-switch status…"}</span>
        {slow && <span className="text-2xs font-mono mt-1 text-zinc-500">This is taking longer than usual.</span>}
      </div>
    );
  }

  if (view === "signed-out") {
    return (
      <div className={`${base} border-dashed border-amber-700/70 bg-amber-950/20 text-amber-200`} role="status">
        <HelpCircle className="w-10 h-10 mb-2" aria-hidden="true" />
        <span className="text-xl font-black tracking-widest">UNKNOWN</span>
        <span className="text-2xs font-mono mt-1 text-amber-200/80">Sign in as operator to see the kill switch.</span>
      </div>
    );
  }

  // unknown: the status could not be confirmed, so it is never shown as armed
  return (
    <div className={`${base} border-dashed border-amber-700/70 bg-amber-950/20 text-amber-200`} role="status">
      <HelpCircle className="w-10 h-10 mb-2" aria-hidden="true" />
      <span className="text-xl font-black tracking-widest">UNKNOWN</span>
      <span className="text-2xs font-mono mt-1 text-amber-200/80">
        {error ?? "The last status check did not complete."}
      </span>
      {lastConfirmed && <span className="text-2xs font-mono mt-1 text-zinc-400">Last confirmed at {lastConfirmed}</span>}
      <button
        type="button"
        onClick={onRetry}
        className="mt-3 rounded border border-amber-700/70 px-3 py-1 text-2xs font-mono uppercase tracking-wider text-amber-200 hover:bg-amber-950/60"
      >
        Retry
      </button>
    </div>
  );
}
