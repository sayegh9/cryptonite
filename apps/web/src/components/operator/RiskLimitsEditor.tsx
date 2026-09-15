"use client";

import { useState } from "react";
import { fetchApi } from "@/lib/api";
import { ShieldAlert, AlertTriangle, KeyRound } from "lucide-react";

export function RiskLimitsEditor() {
  const [maxDailyLossPct, setMaxDailyLossPct] = useState("5.0");
  const [maxOrderNotionalUsd, setMaxOrderNotionalUsd] = useState("10000");
  const [maxPositionNotionalUsd, setMaxPositionNotionalUsd] = useState("50000");
  const [stepUpPassword, setStepUpPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError("");
    setSuccess("");

    try {
      await fetchApi("/control/risk-limits", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": localStorage.getItem("csrf_token") || "",
        },
        body: JSON.stringify({
          max_daily_loss_pct: parseFloat(maxDailyLossPct),
          max_order_notional_usd: parseFloat(maxOrderNotionalUsd),
          max_position_notional_usd: parseFloat(maxPositionNotionalUsd),
          step_up_password: stepUpPassword,
          environment: "DEMO",
          account_id: "DEMO-ACCOUNT-001"
        }),
      });
      setSuccess("Risk limits updated successfully.");
      setStepUpPassword(""); // Clear the password for security
    } catch (err: any) {
      setError(err.message || "Failed to update risk limits.");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="bg-zinc-950 border border-white/5 rounded p-6">
      <div className="flex items-center justify-between mb-6">
        <h2 className="text-sm font-bold flex items-center space-x-2 uppercase tracking-wider text-zinc-200">
          <ShieldAlert className="w-4 h-4 text-amber-500" />
          <span>Risk Limits Configuration</span>
        </h2>
      </div>

      {error && <div className="bg-rose-950/50 text-rose-400 text-[11px] font-mono px-3 py-2 rounded mb-4">{error}</div>}
      {success && <div className="bg-emerald-950/50 text-emerald-400 text-[11px] font-mono px-3 py-2 rounded mb-4">{success}</div>}

      <form onSubmit={handleSubmit} className="space-y-4">
        <div>
          <label className="block text-[10px] font-mono text-zinc-400 mb-1.5 font-bold uppercase tracking-wider">Max Daily Loss (%)</label>
          <input
            type="number"
            step="0.1"
            className="w-full bg-zinc-900 border border-white/5 rounded px-3 py-2 text-zinc-200 focus:border-amber-500 focus:outline-none text-xs transition-colors"
            value={maxDailyLossPct}
            onChange={(e) => setMaxDailyLossPct(e.target.value)}
            required
          />
        </div>
        <div>
          <label className="block text-[10px] font-mono text-zinc-400 mb-1.5 font-bold uppercase tracking-wider">Max Order Notional (USD)</label>
          <input
            type="number"
            step="100"
            className="w-full bg-zinc-900 border border-white/5 rounded px-3 py-2 text-zinc-200 focus:border-amber-500 focus:outline-none text-xs transition-colors"
            value={maxOrderNotionalUsd}
            onChange={(e) => setMaxOrderNotionalUsd(e.target.value)}
            required
          />
        </div>
        <div>
          <label className="block text-[10px] font-mono text-zinc-400 mb-1.5 font-bold uppercase tracking-wider">Max Position Notional (USD)</label>
          <input
            type="number"
            step="100"
            className="w-full bg-zinc-900 border border-white/5 rounded px-3 py-2 text-zinc-200 focus:border-amber-500 focus:outline-none text-xs transition-colors"
            value={maxPositionNotionalUsd}
            onChange={(e) => setMaxPositionNotionalUsd(e.target.value)}
            required
          />
        </div>

        <div className="pt-4 border-t border-white/5">
          <div className="bg-zinc-900/50 border border-amber-900/30 p-4 rounded">
            <h3 className="text-amber-500 font-bold text-xs mb-3 flex items-center space-x-2 uppercase tracking-wider">
              <AlertTriangle className="w-3.5 h-3.5" />
              <span>Authorization Required</span>
            </h3>
            <label className="block text-[10px] font-mono text-zinc-400 mb-1.5 font-bold uppercase tracking-wider">Step-Up Password</label>
            <div className="relative">
              <KeyRound className="absolute left-3 top-2.5 w-3.5 h-3.5 text-zinc-500" />
              <input
                type="password"
                className="w-full bg-zinc-950 border border-white/5 rounded pl-9 pr-3 py-2 text-zinc-200 focus:border-amber-500 focus:outline-none text-xs transition-colors"
                value={stepUpPassword}
                onChange={(e) => setStepUpPassword(e.target.value)}
                required
              />
            </div>
          </div>
        </div>

        <button
          type="submit"
          disabled={loading}
          className="w-full mt-4 bg-amber-600 hover:bg-amber-500 text-white py-2.5 rounded font-bold uppercase text-xs tracking-wider transition-colors"
        >
          {loading ? "SAVING..." : "UPDATE RISK LIMITS"}
        </button>
      </form>
    </div>
  );
}
