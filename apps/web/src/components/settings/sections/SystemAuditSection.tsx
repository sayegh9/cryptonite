"use client";

import React from "react";
import {
  Activity,
  Key,
  ShieldAlert,
  RotateCcw,
  AlertTriangle,
  CheckCircle2,
  Info,
  Server,
  FileText,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import type { EnvHealth, AuditEvent, Warning } from "../types";

export interface SystemAuditSectionProps {
  envHealth: EnvHealth | null;
  auditEvents: AuditEvent[];
  loadingAudit: boolean;
  onReloadAudit: () => Promise<void>;
  onReloadHealth: () => Promise<void>;
}

const WARNING_CLASS: Record<Warning["severity"], string> = {
  critical: "border-red-900/60 bg-red-950/40 text-red-300",
  warn: "border-amber-900/60 bg-amber-950/30 text-amber-200",
  info: "border-cyan-900/60 bg-cyan-950/30 text-cyan-200",
};

export const SystemAuditSection: React.FC<SystemAuditSectionProps> = ({
  envHealth,
  auditEvents,
  loadingAudit,
  onReloadAudit,
  onReloadHealth,
}) => {
  const warnings = envHealth?.warnings ?? [];

  const formatTimestamp = (ts: string | null) => {
    if (!ts) return "—";
    try {
      const d = new Date(ts);
      return d.toLocaleString("en-US", {
        timeZone: "UTC",
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hour12: false,
      }) + " UTC";
    } catch {
      return ts;
    }
  };

  return (
    <div className="space-y-8 max-w-5xl">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-4 border-b border-white/10 pb-4">
        <div>
          <h2 className="text-xl font-bold tracking-tight text-white flex items-center gap-2">
            <Activity className="w-5 h-5 text-cyan-400" aria-hidden="true" />
            System Health & Immutable Audit Trail
          </h2>
          <p className="text-xs text-zinc-400 mt-1 leading-relaxed">
            Live infrastructure diagnostics, provider API keys, container environment modes, and cryptographically verified operator setting modifications.
          </p>
        </div>
        <Button
          type="button"
          onClick={() => {
            void onReloadHealth();
            void onReloadAudit();
          }}
          variant="outline"
          className="border-white/10 bg-zinc-900/60 hover:bg-zinc-800 text-zinc-300 text-xs gap-1.5"
        >
          <RotateCcw className="w-3.5 h-3.5 text-zinc-400" aria-hidden="true" />
          Refresh Diagnostics & Audit
        </Button>
      </div>

      {/* Area 1: Health Warnings Banner */}
      <section className="space-y-3">
        <h3 className="text-sm font-bold uppercase tracking-wider text-zinc-300 flex items-center gap-2">
          <Server className="w-4 h-4 text-cyan-400" aria-hidden="true" />
          Health Status & Diagnostic Checks
        </h3>

        {warnings.length === 0 ? (
          <div className="flex items-center gap-3 rounded-xl border border-emerald-500/30 bg-emerald-950/20 p-4 text-xs text-emerald-300 shadow-sm">
            <CheckCircle2 className="w-5 h-5 text-emerald-400 shrink-0" aria-hidden="true" />
            <div>
              <span className="font-semibold block text-emerald-200">
                All system health checks nominal
              </span>
              <span className="text-[11px] text-emerald-400/80">
                Zero active operational warnings across gateway routes, risk limits, database connections, and memory managers.
              </span>
            </div>
          </div>
        ) : (
          <div className="space-y-2">
            {warnings.map((w) => (
              <div
                key={w.code}
                className={`flex items-start gap-3 rounded-lg border p-3 text-xs leading-relaxed ${WARNING_CLASS[w.severity]}`}
              >
                {w.severity === "critical" ? (
                  <ShieldAlert className="w-4 h-4 shrink-0 text-red-400 mt-0.5" aria-hidden="true" />
                ) : (
                  <AlertTriangle className="w-4 h-4 shrink-0 text-amber-400 mt-0.5" aria-hidden="true" />
                )}
                <div>
                  <span className="font-mono font-semibold uppercase text-[10px] tracking-wider block opacity-75">
                    {w.code}
                  </span>
                  <span>{w.text}</span>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      {/* Area 2: Environment & API Keys Cards */}
      <section className="space-y-3">
        <h3 className="text-sm font-bold uppercase tracking-wider text-zinc-300 flex items-center gap-2">
          <Key className="w-4 h-4 text-cyan-400" aria-hidden="true" />
          Container Environment & Execution Keys
        </h3>

        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
          {/* Card 1: Gemini API Key */}
          <div className="rounded-xl border border-white/10 bg-zinc-950/60 p-4 space-y-2.5 flex flex-col justify-between">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold text-zinc-200">Gemini API Key</span>
              <span
                className={`text-[10px] font-bold px-2 py-0.5 rounded border ${
                  envHealth?.gemini_key_set
                    ? "border-emerald-500/50 bg-emerald-950/40 text-emerald-400"
                    : "border-red-500/50 bg-red-950/40 text-red-400"
                }`}
              >
                {envHealth?.gemini_key_set ? "CONFIGURED" : "MISSING"}
              </span>
            </div>
            <p className="text-[11px] text-zinc-400">
              Required for the AI gateway & multi-agent consensus debate engine
            </p>
          </div>

          {/* Card 2: Binance Demo Key */}
          <div className="rounded-xl border border-white/10 bg-zinc-950/60 p-4 space-y-2.5 flex flex-col justify-between">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold text-zinc-200">Binance API Key</span>
              <span
                className={`text-[10px] font-bold px-2 py-0.5 rounded border ${
                  envHealth?.binance_key_set
                    ? "border-emerald-500/50 bg-emerald-950/40 text-emerald-400"
                    : "border-amber-500/50 bg-amber-950/40 text-amber-400"
                }`}
              >
                {envHealth?.binance_key_set ? "CONFIGURED" : "MISSING"}
              </span>
            </div>
            <p className="text-[11px] text-zinc-400">
              Required for market data ingestion and order execution routing
            </p>
          </div>

          {/* Card 3: Execution Mode */}
          <div className="rounded-xl border border-white/10 bg-zinc-950/60 p-4 space-y-2.5 flex flex-col justify-between">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold text-zinc-200">Execution Mode</span>
              <span className="text-[10px] font-mono font-bold px-2 py-0.5 rounded border border-purple-500/50 bg-purple-950/40 text-purple-300">
                {envHealth?.environment_mode ?? "DEMO"}
              </span>
            </div>
            <p className="text-[11px] text-zinc-400">
              Paper simulation vs live exchange capital execution
            </p>
          </div>

          {/* Card 4: Emergency Kill Switch */}
          <div className="rounded-xl border border-white/10 bg-zinc-950/60 p-4 space-y-2.5 flex flex-col justify-between">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold text-zinc-200">Kill Switch</span>
              <span
                className={`text-[10px] font-bold px-2 py-0.5 rounded border ${
                  envHealth?.kill_switch_active
                    ? "border-red-500/50 bg-red-950/40 text-red-400"
                    : "border-emerald-500/50 bg-emerald-950/40 text-emerald-400"
                }`}
              >
                {envHealth?.kill_switch_active ? "LATCHED · TRADING HALTED" : "ARMED"}
              </span>
            </div>
            <p className="text-[11px] text-zinc-400">
              Emergency account-level stop latching all order dispatch
            </p>
          </div>
        </div>

        <div className="p-3.5 bg-blue-950/20 border border-blue-900/30 rounded-lg text-xs text-blue-300 flex items-start gap-2.5">
          <Info className="w-4 h-4 text-blue-400 shrink-0 mt-0.5" aria-hidden="true" />
          <span>
            <strong>Container Architecture:</strong> Modifying environment variables requires editing the host machine <span className="font-mono text-zinc-200">.env</span> file and issuing a container reboot (<span className="font-mono text-zinc-200">docker compose restart</span>).
          </span>
        </div>
      </section>

      {/* Area 3: Settings Change Audit Log */}
      <section className="space-y-3">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-bold uppercase tracking-wider text-zinc-300 flex items-center gap-2">
            <FileText className="w-4 h-4 text-cyan-400" aria-hidden="true" />
            Settings Change Audit Trail (Recent 50 Events)
          </h3>
          <span className="text-xs font-mono text-zinc-400">
            {auditEvents.length} Recorded Events
          </span>
        </div>

        {loadingAudit ? (
          <div className="p-8 text-center text-xs text-zinc-400 font-mono">
            Loading audit log stream…
          </div>
        ) : auditEvents.length === 0 ? (
          <div className="rounded-xl border border-white/10 bg-zinc-950/40 p-6 text-center text-xs text-zinc-400">
            No settings change events recorded in this audit ledger yet.
          </div>
        ) : (
          <div className="rounded-xl border border-white/10 bg-zinc-950/60 overflow-hidden">
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs" aria-label="Settings change audit trail">
                <thead>
                  <tr className="border-b border-white/10 bg-zinc-900/50 text-[10px] font-semibold uppercase tracking-wider text-zinc-400 font-mono">
                    <th scope="col" className="px-4 py-2.5">Timestamp</th>
                    <th scope="col" className="px-4 py-2.5">Operator</th>
                    <th scope="col" className="px-4 py-2.5">Action</th>
                    <th scope="col" className="px-4 py-2.5">Target / Key</th>
                    <th scope="col" className="px-4 py-2.5">Change Details</th>
                    <th scope="col" className="px-4 py-2.5 text-right">TOTP</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-white/5 font-mono text-[11px]">
                  {auditEvents.map((evt) => {
                    const d = evt.details || {};
                    const keyName = (d.key || d.role || d.setting || "—") as string;
                    const before = d.before !== undefined ? String(d.before) : null;
                    const after = d.after !== undefined ? String(d.after) : null;
                    const reason = (d.reason || d.note || null) as string | null;
                    const totpVerified = Boolean(d.totp_verified);

                    return (
                      <tr key={evt.event_id} className="hover:bg-white/[0.02] transition-colors">
                        <td className="px-4 py-2.5 whitespace-nowrap text-zinc-400">
                          {formatTimestamp(evt.occurred_at)}
                        </td>
                        <td className="px-4 py-2.5 whitespace-nowrap text-zinc-200 font-sans font-medium">
                          {evt.username || "operator"}
                        </td>
                        <td className="px-4 py-2.5 whitespace-nowrap">
                          <span
                            className={`px-1.5 py-0.5 rounded text-[10px] font-semibold ${
                              evt.action.includes("FAILED")
                                ? "bg-red-950/60 text-red-300 border border-red-900/60"
                                : "bg-cyan-950/60 text-cyan-300 border border-cyan-900/60"
                            }`}
                          >
                            {evt.action}
                          </span>
                        </td>
                        <td className="px-4 py-2.5 whitespace-nowrap text-cyan-300 font-semibold">
                          {keyName}
                        </td>
                        <td className="px-4 py-2.5 max-w-xs truncate text-zinc-400 font-sans">
                          {before && after ? (
                            <span>
                              <span className="line-through text-zinc-400 font-mono">{before}</span>{" "}
                              → <span className="text-zinc-200 font-mono">{after}</span>
                              {reason ? ` (${reason})` : ""}
                            </span>
                          ) : reason ? (
                            <span>{reason}</span>
                          ) : (
                            <span className="text-zinc-400">—</span>
                          )}
                        </td>
                        <td className="px-4 py-2.5 text-right whitespace-nowrap">
                          {totpVerified ? (
                            <span className="text-[10px] text-emerald-400 font-bold px-1.5 py-0.5 rounded bg-emerald-950/40 border border-emerald-900/50">
                              VERIFIED
                            </span>
                          ) : (
                            <span className="text-[10px] text-zinc-400">Free / None</span>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </section>
    </div>
  );
};
