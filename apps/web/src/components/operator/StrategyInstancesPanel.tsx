"use client";

import { useEffect, useState } from "react";
import { fetchApi } from "@/lib/api";
import {
  Play,
  Square,
  Pause,
  Plus,
  RefreshCw,
  Cpu,
  FileSignature,
  BrainCircuit,
  XCircle,
  TrendingUp,
  Layers,
  } from "lucide-react";
import { buildQuery, dispatchSwitchWorkspace } from "@/lib/navigation";
import { fmtCompactUsd, fmtSignedUsd, strategyStats } from "@/lib/portfolio/metrics";
import type { PaperPosition } from "@/lib/stores/usePortfolioStore";
import { DeployStrategyModal } from "./DeployStrategyModal";
import { ManualTradeModal } from "./ManualTradeModal";

/** AI mandates are paused, resumed and stopped in Mission Control; the API refuses instance commands for them. */
const isAiMandate = (instanceId: string) => instanceId.startsWith("AI_MANDATE_");

const SEGMENTS = 8;

/** Capital in use vs. allocated, as discrete segments (over-deployment turns the bar red). */
function AllocationBar({ used, allocated }: { used: number; allocated: number }) {
  const ratio = allocated > 0 ? used / allocated : 0;
  const filled = Math.min(SEGMENTS, Math.round(ratio * SEGMENTS));
  const over = ratio > 1.001;
  const text = `${fmtCompactUsd(used)} / ${fmtCompactUsd(allocated)}`;
  return (
    <div className="w-[72px]">
      <div
        role="meter"
        aria-label="Allocation in use"
        aria-valuemin={0}
        aria-valuemax={Math.max(allocated, 0)}
        aria-valuenow={Math.max(used, 0)}
        aria-valuetext={`${text}${over ? " (over-allocated)" : ""}`}
        className="flex gap-px"
      >
        {Array.from({ length: SEGMENTS }, (_, i) => (
          <span
            key={i}
            className={`h-1.5 flex-1 rounded-sm ${i < filled ? (over ? "bg-red-500" : "bg-emerald-500") : "bg-zinc-800"}`}
          />
        ))}
      </div>
      <div className="mt-1 whitespace-nowrap text-[10px] leading-none tabular-nums text-zinc-400" data-testid="allocation-text">{text}</div>
    </div>
  );
}

function MandateManagedNote() {
  return (
    <span className="inline-block max-w-[11rem] align-middle text-right text-2xs leading-tight text-zinc-400 font-sans">
      AI mandate — manage it in{" "}
      <a
        href={`/${buildQuery("mission", "overview")}`}
        onClick={(e) => {
          // Modified clicks keep the browser's behaviour (new tab, new window, download)
          if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
          e.preventDefault();
          dispatchSwitchWorkspace({ mode: "mission", view: "overview" });
        }}
        className="font-bold text-cyan-400 underline hover:text-cyan-300"
      >
        Mission Control
      </a>
    </span>
  );
}

export function StrategyInstancesPanel() {
  const [instances, setInstances] = useState<any[]>([]);
  const [selectedInstance, setSelectedInstance] = useState<any | null>(null);
  const [positions, setPositions] = useState<PaperPosition[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [showDeployModal, setShowDeployModal] = useState(false);
  const [showManualTradeModal, setShowManualTradeModal] = useState(false);

  const loadInstances = async () => {
    try {
      const data = await fetchApi("/paper/strategies");
      setInstances(data);
      if (selectedInstance) {
        const updated = data.find((i: any) => i.instance_id === selectedInstance.instance_id);
        if (updated) setSelectedInstance(updated);
      }
      setError("");
      // P&L and allocation use come from the positions ledger; failing to load it only blanks those two columns
      try {
        const rows = await fetchApi("/paper/positions");
        setPositions(Array.isArray(rows) ? rows : null);
      } catch {
        setPositions(null);
      }
    } catch (err: any) {
      setError(err.message || "Failed to load strategy instances");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadInstances();
    const interval = setInterval(loadInstances, 3000); // Polling for live status
    return () => clearInterval(interval);
  }, []);

  const handleCommand = async (instance_id: string, command: "pause" | "resume" | "stop" | "close-position") => {
    try {
      await fetchApi(`/paper/strategies/${instance_id}/${command}`, {
        method: "POST",
        headers: { "X-CSRF-Token": localStorage.getItem("csrf_token") || "" },
      });
      loadInstances();
    } catch (err: any) {
      alert(`Error dispatching command: ${err.message}`);
    }
  };

  if (loading) {
    return <div className="text-zinc-500 font-mono animate-pulse">Loading execution instances...</div>;
  }

  return (
    <div className="bg-zinc-950 border border-white/5 rounded p-6">
      <div className="flex items-center justify-between mb-6">
        <h3 className="text-sm font-bold text-zinc-200 flex items-center space-x-2 uppercase tracking-wider">
          <Cpu className="w-4 h-4 text-cyan-400" />
          <span>Active Strategy Instances</span>
        </h3>
        <div className="flex space-x-2">
          <button
            onClick={loadInstances}
            className="p-1.5 bg-zinc-900 hover:bg-zinc-800 text-zinc-400 rounded transition-colors border border-white/5"
            title="Refresh"
          >
            <RefreshCw className="w-3.5 h-3.5" />
          </button>
          <button
            onClick={() => setShowManualTradeModal(true)}
            className="flex items-center space-x-1.5 px-3 py-1.5 bg-amber-600 hover:bg-amber-500 text-white text-2xs font-bold rounded transition-colors uppercase tracking-wider"
          >
            <FileSignature className="w-3.5 h-3.5" />
            <span>Manual Trade</span>
          </button>
          <button
            onClick={() => setShowDeployModal(true)}
            className="flex items-center space-x-1.5 px-3 py-1.5 bg-cyan-600 hover:bg-cyan-500 text-zinc-950 text-2xs font-bold rounded transition-colors uppercase tracking-wider"
          >
            <Plus className="w-3.5 h-3.5" />
            <span>Deploy New</span>
          </button>
        </div>
      </div>

      {error && (
        <div className="bg-rose-950/40 border border-rose-900/50 text-rose-400 text-2xs font-mono p-3 rounded mb-4">
          {error}
        </div>
      )}

      <div className="overflow-x-auto rounded border border-white/5">
        <table className="w-full text-left text-2xs font-mono">
          <thead className="bg-zinc-900/50 border-b border-white/5 text-zinc-400 uppercase tracking-wider">
            <tr>
              <th className="py-2.5 px-2 font-semibold">Instance ID</th>
              <th className="py-2.5 px-2 font-semibold">Strategy</th>
              <th className="py-2.5 px-2 font-semibold">Instrument</th>
              <th className="py-2.5 px-2 font-semibold">Status</th>
              <th className="py-2.5 px-2 font-semibold text-right whitespace-nowrap">7d P&amp;L</th>
              <th className="py-2.5 px-2 font-semibold">Allocation</th>
              <th className="py-2.5 px-2 font-semibold text-right sticky right-0 z-10 bg-zinc-900 border-l border-white/10 shadow-[-6px_0_6px_-6px_rgba(0,0,0,0.6)]">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-white/5 text-zinc-300">
            {instances.length === 0 ? (
              <tr>
                <td colSpan={7} className="py-6 text-center text-zinc-500">
                  No strategies deployed on this account.
                </td>
              </tr>
            ) : (
              instances.map((inst) => {
                const isSystem = ["AI_DEBATE_WORKER", "MANUAL_TRADE"].includes(inst.instance_id);
                const isRunning = inst.status === "RUNNING";
                const isPaused = inst.status === "PAUSED";
                const isStopped = inst.status === "STOPPED" || inst.status === "STOP";
                const managedByMandate = isAiMandate(inst.instance_id);
                const stats = positions ? strategyStats(positions, inst.instance_id) : null;
                const alloc = Number(inst.allocation_usd);

                let statusColor = "text-zinc-400 bg-zinc-800";
                if (isRunning) statusColor = "text-emerald-400 bg-emerald-950/70 border border-emerald-800/50";
                if (isPaused) statusColor = "text-amber-400 bg-amber-950/70 border border-amber-800/50";
                if (isStopped) statusColor = "text-zinc-400 bg-zinc-900 border border-zinc-700";

                return (
                  <tr
                    key={inst.instance_id}
                    onClick={() => setSelectedInstance(inst)}
                    className="hover:bg-zinc-800/40 transition-colors cursor-pointer group"
                  >
                    <td className="py-3 px-2 font-semibold text-zinc-200">
                      <div className="flex items-center space-x-1.5">
                        <span className="group-hover:text-cyan-400 transition-colors inline-block max-w-[7.5rem] truncate align-bottom" title={inst.instance_id}>
                          {inst.instance_id}
                        </span>
                        {isSystem && (
                          <span className="px-1.5 py-0.5 rounded text-2xs bg-indigo-950/80 text-indigo-300 border border-indigo-700/60 font-bold uppercase tracking-wider">
                            System
                          </span>
                        )}
                      </div>
                    </td>
                    <td className="py-3 px-2">
                      <span className="bg-zinc-950 px-1.5 py-0.5 rounded border border-zinc-800 text-zinc-400">
                        {inst.strategy_name}
                      </span>
                    </td>
                    <td className="py-3 px-2 text-cyan-400 font-semibold">{inst.instrument_id}</td>
                    <td className="py-3 px-2">
                      <span className={`px-2 py-0.5 rounded text-2xs font-bold ${statusColor}`}>
                        {inst.status}
                      </span>
                    </td>
                    <td
                      className={`py-3 px-2 text-right tabular-nums font-bold whitespace-nowrap ${
                        !stats ? "text-zinc-600" : stats.pnl7d > 0 ? "text-emerald-400" : stats.pnl7d < 0 ? "text-red-400" : "text-zinc-500"
                      }`}
                      data-testid="instance-pnl"
                    >
                      {stats ? fmtSignedUsd(stats.pnl7d) : "—"}
                    </td>
                    <td className="py-3 px-2">
                      {stats && Number.isFinite(alloc) ? (
                        <AllocationBar used={stats.openNotional} allocated={alloc} />
                      ) : (
                        <span className="text-zinc-600">—</span>
                      )}
                    </td>
                    <td className="py-3 px-2 text-right sticky right-0 z-10 bg-zinc-950 group-hover:bg-zinc-900 border-l border-white/10 shadow-[-6px_0_6px_-6px_rgba(0,0,0,0.6)]" onClick={(e) => e.stopPropagation()}>
                      <div className="flex flex-col items-end gap-1">
{managedByMandate && <MandateManagedNote />}
<div className="flex flex-nowrap items-center justify-end gap-1.5">
                      {isRunning && !managedByMandate && (
                        <button
                          onClick={() => handleCommand(inst.instance_id, "pause")}
                          className="p-1.5 bg-amber-600 hover:bg-amber-500 text-white rounded transition-colors inline-flex"
                          title="Pause"
                        >
                          <Pause className="w-3.5 h-3.5" />
                        </button>
                      )}
                      {isRunning && (
                        // Flatten reduces risk, so it stays on AI mandate rows too
                        <button
                          onClick={() => handleCommand(inst.instance_id, "close-position")}
                          className="px-2 py-1 bg-purple-600 hover:bg-purple-500 text-white text-2xs rounded transition-colors inline-flex font-bold uppercase"
                          title="Flatten Position"
                        >
                          FLATTEN
                        </button>
                      )}
                      {isPaused && !managedByMandate && (
                        <button
                          onClick={() => handleCommand(inst.instance_id, "resume")}
                          className="p-1.5 bg-emerald-600 hover:bg-emerald-500 text-white rounded transition-colors inline-flex"
                          title="Resume"
                        >
                          <Play className="w-3.5 h-3.5" />
                        </button>
                      )}
                      {!isStopped && (
                        <>
                          <button
                            onClick={() => {
                              window.dispatchEvent(
                                new CustomEvent("cryptonite:open-copilot", {
                                  detail: {
                                    mode: "explain",
                                    prompt: `Synthesize performance delta for ${inst.strategy_name} (${inst.instance_id}). Compare the live paper trading metrics against its historical backtest benchmark.`,
                                    autoSend: true,
                                  },
                                })
                              );
                            }}
                            className="p-1.5 bg-cyan-600 hover:bg-cyan-500 text-zinc-950 rounded transition-colors inline-flex"
                            title="Analyze Paper vs Backtest Delta"
                          >
                            <BrainCircuit className="w-3.5 h-3.5" />
                          </button>
                          {!managedByMandate && (
                            <button
                              onClick={() => {
                                if (confirm(`Are you sure you want to STOP instance ${inst.instance_id}?`)) {
                                  handleCommand(inst.instance_id, "stop");
                                }
                              }}
                              className="p-1.5 bg-red-600 hover:bg-red-500 text-white rounded transition-colors inline-flex"
                              title="Stop"
                            >
                              <Square className="w-3.5 h-3.5" />
                            </button>
                          )}
                        </>
                      )}
                    </div>
</div>
</td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>

      {/* Instance Details Slide-Over Drawer */}
      {selectedInstance && (
        <div
          className="fixed inset-0 bg-black/60 z-50 flex justify-end"
          onClick={() => setSelectedInstance(null)}
        >
          <div
            className="w-full max-w-md bg-zinc-950 border-l border-zinc-800 h-full flex flex-col shadow-2xl animate-in slide-in-from-right duration-200 text-zinc-200 font-mono text-xs"
            onClick={(e) => e.stopPropagation()}
          >
            {/* Drawer Header */}
            <div className="p-4 border-b border-zinc-800 flex justify-between items-center bg-zinc-900/60">
              <h2 className="font-bold text-white flex items-center space-x-2">
                <Cpu className="w-4 h-4 text-cyan-400" />
                <span>INSTANCE DETAILS</span>
              </h2>
              <button
                onClick={() => setSelectedInstance(null)}
                className="text-zinc-500 hover:text-white transition-colors p-1 rounded hover:bg-zinc-800"
              >
                <XCircle className="w-5 h-5" />
              </button>
            </div>

            {/* Drawer Body */}
            <div className="p-6 flex-1 overflow-y-auto space-y-5">
              {/* Type Banner */}
              {["AI_DEBATE_WORKER", "MANUAL_TRADE"].includes(selectedInstance.instance_id) ? (
                <div className="p-3 bg-indigo-950/40 border border-indigo-700/50 rounded-lg space-y-1">
                  <div className="text-indigo-300 font-bold flex items-center space-x-1.5">
                    <Layers className="w-4 h-4 text-indigo-400" />
                    <span>System Execution Router</span>
                  </div>
                  <p className="text-2xs text-zinc-400 font-sans leading-relaxed">
                    Internal platform engine that automatically receives and routes orders generated by AI Debates or Manual Trade actions into the matching engine.
                  </p>
                </div>
              ) : (
                <div className="p-3 bg-cyan-950/40 border border-cyan-700/50 rounded-lg space-y-1">
                  <div className="text-cyan-300 font-bold flex items-center space-x-1.5">
                    <TrendingUp className="w-4 h-4 text-cyan-400" />
                    <span>Quantitative Strategy Instance</span>
                  </div>
                  <p className="text-2xs text-zinc-400 font-sans leading-relaxed">
                    Autonomous algorithmic strategy executing discrete tick/bar signals in the paper simulation environment.
                  </p>
                </div>
              )}

              {/* Identity & Configuration */}
              <div className="space-y-3">
                <div className="text-zinc-500 text-2xs font-bold uppercase tracking-wider">
                  Identification
                </div>
                <div className="p-3 bg-zinc-900/80 rounded-lg border border-zinc-800 space-y-2">
                  <div className="flex justify-between items-center">
                    <span className="text-zinc-500">Instance ID:</span>
                    <span className="font-bold text-zinc-100">{selectedInstance.instance_id}</span>
                  </div>
                  <div className="flex justify-between items-center">
                    <span className="text-zinc-500">Strategy Name:</span>
                    <span className="text-cyan-400 font-semibold">{selectedInstance.strategy_name}</span>
                  </div>
                  <div className="flex justify-between items-center">
                    <span className="text-zinc-500">Account ID:</span>
                    <span className="text-zinc-300">{selectedInstance.account_id || "PAPER-ACCOUNT-001"}</span>
                  </div>
                </div>
              </div>

              {/* Market Parameters */}
              <div className="space-y-3">
                <div className="text-zinc-500 text-2xs font-bold uppercase tracking-wider">
                  Market & Allocation
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <div className="p-3 bg-zinc-900/80 rounded-lg border border-zinc-800">
                    <div className="text-2xs text-zinc-500 uppercase tracking-wider mb-1">Instrument</div>
                    <div className="font-bold text-cyan-400">{selectedInstance.instrument_id}</div>
                  </div>
                  <div className="p-3 bg-zinc-900/80 rounded-lg border border-zinc-800">
                    <div className="text-2xs text-zinc-500 uppercase tracking-wider mb-1">Timeframe</div>
                    <div className="font-bold text-white">{selectedInstance.timeframe || "1m"}</div>
                  </div>
                  <div className="p-3 bg-zinc-900/80 rounded-lg border border-zinc-800 col-span-2">
                    <div className="text-2xs text-zinc-500 uppercase tracking-wider mb-1">Allocated Capital</div>
                    <div className="font-bold text-emerald-400 text-sm">
                      ${Number(selectedInstance.allocation_usd || 0).toLocaleString()} USD
                    </div>
                  </div>
                </div>
              </div>

              {/* Status & Telemetry */}
              <div className="space-y-3">
                <div className="text-zinc-500 text-2xs font-bold uppercase tracking-wider">
                  Runtime State
                </div>
                <div className="p-3 bg-zinc-900/80 rounded-lg border border-zinc-800 space-y-2">
                  <div className="flex justify-between items-center">
                    <span className="text-zinc-500">Execution Status:</span>
                    <span
                      className={`px-2 py-0.5 rounded text-2xs font-bold ${
                        selectedInstance.status === "RUNNING"
                          ? "bg-emerald-950 text-emerald-400 border border-emerald-800"
                          : selectedInstance.status === "PAUSED"
                          ? "bg-amber-950 text-amber-400 border border-amber-800"
                          : "bg-zinc-800 text-zinc-400 border border-zinc-700"
                      }`}
                    >
                      {selectedInstance.status}
                    </span>
                  </div>
                  {selectedInstance.created_at && (
                    <div className="flex justify-between items-center text-2xs">
                      <span className="text-zinc-500">Deployed At:</span>
                      <span className="text-zinc-400">
                        {new Date(selectedInstance.created_at).toLocaleString()}
                      </span>
                    </div>
                  )}
                  {selectedInstance.updated_at && (
                    <div className="flex justify-between items-center text-2xs">
                      <span className="text-zinc-500">Last Activity:</span>
                      <span className="text-zinc-400">
                        {new Date(selectedInstance.updated_at).toLocaleString()}
                      </span>
                    </div>
                  )}
                </div>
              </div>

              {/* Parameters Payload */}
              {selectedInstance.resolved_parameters && (
                <div className="space-y-2">
                  <div className="text-zinc-500 text-2xs font-bold uppercase tracking-wider">
                    Resolved Parameters
                  </div>
                  <pre className="p-3 bg-zinc-900 rounded-lg border border-zinc-800 text-2xs text-zinc-300 overflow-x-auto">
                    {JSON.stringify(
                      typeof selectedInstance.resolved_parameters === "string"
                        ? JSON.parse(selectedInstance.resolved_parameters)
                        : selectedInstance.resolved_parameters,
                      null,
                      2
                    )}
                  </pre>
                </div>
              )}
            </div>

            {/* Drawer Footer / Quick Actions */}
            <div className="p-4 border-t border-zinc-800 bg-zinc-900/40 flex items-center justify-between gap-2">
              {isAiMandate(selectedInstance.instance_id) ? (
                <>
                  <MandateManagedNote />
                  {selectedInstance.status === "RUNNING" && (
                    // Flatten reduces risk, so it stays on AI mandates; Pause, Resume and Stop are managed in Mission Control
                    <button
                      onClick={() => handleCommand(selectedInstance.instance_id, "close-position")}
                      className="px-4 py-2 bg-purple-600 hover:bg-purple-500 text-white rounded font-bold transition-colors flex items-center justify-center space-x-1"
                      title="Flatten Position"
                    >
                      <span>Flatten Position</span>
                    </button>
                  )}
                </>
              ) : (
                <>
              {selectedInstance.status === "RUNNING" ? (
                <button
                  onClick={() => handleCommand(selectedInstance.instance_id, "pause")}
                  className="flex-1 py-2 bg-amber-600 hover:bg-amber-500 text-white rounded font-bold transition-colors flex items-center justify-center space-x-1"
                >
                  <Pause className="w-3.5 h-3.5" />
                  <span>Pause Strategy</span>
                </button>
              ) : selectedInstance.status === "PAUSED" ? (
                <button
                  onClick={() => handleCommand(selectedInstance.instance_id, "resume")}
                  className="flex-1 py-2 bg-emerald-600 hover:bg-emerald-500 text-white rounded font-bold transition-colors flex items-center justify-center space-x-1"
                >
                  <Play className="w-3.5 h-3.5" />
                  <span>Resume Strategy</span>
                </button>
              ) : null}

              {selectedInstance.status !== "STOPPED" && selectedInstance.status !== "STOP" && (
                <button
                  onClick={() => {
                    if (confirm(`Are you sure you want to STOP instance ${selectedInstance.instance_id}?`)) {
                      handleCommand(selectedInstance.instance_id, "stop");
                    }
                  }}
                  className="px-4 py-2 bg-red-950 hover:bg-red-900 border border-red-800 text-red-300 rounded font-bold transition-colors flex items-center justify-center space-x-1"
                >
                  <Square className="w-3.5 h-3.5" />
                  <span>Stop</span>
                </button>
              )}
                </>
              )}
            </div>
          </div>
        </div>
      )}

      {showDeployModal && (
        <DeployStrategyModal
          onClose={() => setShowDeployModal(false)}
          onDeployed={() => {
            setShowDeployModal(false);
            loadInstances();
          }}
        />
      )}
      {showManualTradeModal && (
        <ManualTradeModal
          onClose={() => setShowManualTradeModal(false)}
          onOrderSubmitted={() => {
            setShowManualTradeModal(false);
          }}
        />
      )}
    </div>
  );
}
