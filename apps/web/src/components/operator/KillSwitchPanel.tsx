"use client";

import { useState, useEffect } from "react";
import { fetchApi } from "@/lib/api";
import { AlertTriangle, Power, ShieldAlert, CheckCircle, RefreshCcw } from "lucide-react";

/** Kill switch for one (environment, account). DEMO targets the demo executor; PAPER the paper engine. */
export function KillSwitchPanel({
  environment = "PAPER",
  accountId = "PAPER-ACCOUNT-001",
}: { environment?: "DEMO" | "PAPER"; accountId?: string } = {}) {
  const [status, setStatus] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [actionLoading, setActionLoading] = useState(false);
  const [error, setError] = useState("");
  
  // Reset flow state
  const [showReset, setShowReset] = useState(false);
  const [stepUpPassword, setStepUpPassword] = useState("");
  const [resetReason, setResetReason] = useState("");

  const loadStatus = async () => {
    try {
      const qs = new URLSearchParams({ environment, account_id: accountId });
      const data = await fetchApi(`/control/kill-switch/status?${qs}`);
      setStatus(data);
      setError("");
    } catch (err: any) {
      setError(err.message || "Failed to load kill-switch status");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadStatus();
    const interval = setInterval(loadStatus, 5000); // Poll every 5s
    return () => clearInterval(interval);
  }, []);

  const handleTrip = async () => {
    if (!confirm("EMERGENCY: Are you sure you want to trip the execution kill-switch? All trading will halt!")) return;
    setActionLoading(true);
    try {
      await fetchApi("/control/kill-switch/trip", {
        method: "POST",
        headers: { "X-CSRF-Token": localStorage.getItem("csrf_token") || "" },
        body: JSON.stringify({ reason: "Manual operator emergency trip via UI", environment, account_id: accountId })
      });
      await loadStatus();
    } catch (err: any) {
      setError(err.message);
    } finally {
      setActionLoading(false);
    }
  };

  const handleReset = async (e: React.FormEvent) => {
    e.preventDefault();
    setActionLoading(true);
    try {
      await fetchApi("/control/kill-switch/reset", {
        method: "POST",
        headers: { "X-CSRF-Token": localStorage.getItem("csrf_token") || "" },
        body: JSON.stringify({
          step_up_password: stepUpPassword,
          reason: resetReason,
          environment,
          account_id: accountId
        })
      });
      alert(
        environment === "PAPER"
          ? "Reset requested. The paper engine resets only when no orders or positions are open."
          : "Reset requested successfully! The executor daemon will evaluate the 7-point checklist."
      );
      setShowReset(false);
      setStepUpPassword("");
      setResetReason("");
      await loadStatus();
    } catch (err: any) {
      setError(err.message);
    } finally {
      setActionLoading(false);
    }
  };

  if (loading) return <div className="text-zinc-500 font-mono animate-pulse">Loading Kill-Switch status...</div>;

  const isLatched = status?.is_latched;

  return (
    <div className="bg-zinc-950 border border-white/5 rounded p-6">
      <div className="flex items-center justify-between mb-6">
        <h2 className="text-sm font-bold flex items-center space-x-2 uppercase tracking-wider text-zinc-200">
          <Power className="w-4 h-4 text-cyan-400" />
          <span>Execution Kill-Switch · {environment}</span>
        </h2>
        <div className="flex items-center space-x-2">
          <span className="text-[10px] font-mono text-zinc-500">
            GEN: {status?.latch_generation}
          </span>
          <button onClick={loadStatus} className="p-1 hover:text-cyan-400 text-zinc-500 transition-colors">
            <RefreshCcw className="w-3.5 h-3.5" />
          </button>
        </div>
      </div>

      {error && <div className="bg-rose-950/50 text-rose-400 text-[11px] font-mono px-3 py-2 rounded mb-4">{error}</div>}

      <div className="flex items-center justify-center p-6 border border-dashed border-white/5 rounded mb-6 bg-zinc-950">
        {isLatched ? (
          <div className="flex flex-col items-center text-rose-500 animate-pulse">
            <ShieldAlert className="w-12 h-12 mb-2" />
            <span className="text-xl font-black tracking-widest">LATCHED</span>
            <span className="text-[10px] font-mono mt-1 text-zinc-500">TRADING HALTED</span>
          </div>
        ) : (
          <div className="flex flex-col items-center text-emerald-500">
            <CheckCircle className="w-12 h-12 mb-2" />
            <span className="text-xl font-black tracking-widest">ARMED</span>
            <span className="text-[10px] font-mono mt-1 text-zinc-500">EXECUTOR OPERATIONAL</span>
          </div>
        )}
      </div>

      {/* Primary Control */}
      {!showReset ? (
        <div className="space-y-3">
          <button
            onClick={handleTrip}
            disabled={isLatched || actionLoading}
            className={`w-full py-3 rounded font-black text-sm tracking-widest uppercase transition-colors ${
              isLatched
                ? "bg-zinc-900 text-zinc-600 cursor-not-allowed border border-white/5"
                : "bg-rose-600 hover:bg-rose-500 text-white shadow-[0_0_15px_rgba(225,29,72,0.3)]"
            }`}
          >
            {actionLoading ? "PROCESSING..." : "TRIP KILL-SWITCH"}
          </button>

          {isLatched && (
            <button
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
            <AlertTriangle className="w-3.5 h-3.5" />
            <span>Authoritative Reset</span>
          </h3>
          <form onSubmit={handleReset} className="space-y-3">
            <div>
              <label className="block text-[10px] font-mono text-zinc-400 mb-1.5 font-bold uppercase tracking-wider">Audit Reason (Min 5 chars)</label>
              <input
                type="text"
                className="w-full bg-zinc-950 border border-white/5 rounded px-3 py-2 text-zinc-200 focus:border-amber-500 focus:outline-none text-xs transition-colors"
                value={resetReason}
                onChange={(e) => setResetReason(e.target.value)}
                required
                minLength={5}
              />
            </div>
            <div>
              <label className="block text-[10px] font-mono text-zinc-400 mb-1.5 font-bold uppercase tracking-wider">Step-Up Password</label>
              <input
                type="password"
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
                className="flex-1 bg-zinc-900 hover:bg-zinc-800 border border-white/5 text-zinc-300 py-2 rounded font-bold text-[10px] uppercase tracking-wider transition-colors"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={actionLoading}
                className="flex-1 bg-amber-600 hover:bg-amber-500 text-white py-2 rounded font-bold text-[10px] uppercase tracking-wider transition-colors"
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
