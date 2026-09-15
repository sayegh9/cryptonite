"use client";

import { useEffect, useState } from "react";
import { fetchApi } from "@/lib/api";
import { Play, Square, Pause, Plus, RefreshCw, Trash2, Cpu, FileSignature } from "lucide-react";
import { DeployStrategyModal } from "./DeployStrategyModal";
import { ManualTradeModal } from "./ManualTradeModal";

export function StrategyInstancesPanel() {
  const [instances, setInstances] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [showDeployModal, setShowDeployModal] = useState(false);
  const [showManualTradeModal, setShowManualTradeModal] = useState(false);

  const loadInstances = async () => {
    try {
      const data = await fetchApi("/paper/strategies");
      setInstances(data);
      setError("");
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
            className="flex items-center space-x-1.5 px-3 py-1.5 bg-amber-600 hover:bg-amber-500 text-white text-[10px] font-bold rounded transition-colors uppercase tracking-wider"
          >
            <FileSignature className="w-3.5 h-3.5" />
            <span>Manual Trade</span>
          </button>
          <button
            onClick={() => setShowDeployModal(true)}
            className="flex items-center space-x-1.5 px-3 py-1.5 bg-cyan-600 hover:bg-cyan-500 text-white text-[10px] font-bold rounded transition-colors uppercase tracking-wider"
          >
            <Plus className="w-3.5 h-3.5" />
            <span>Deploy New</span>
          </button>
        </div>
      </div>

      {error && (
        <div className="bg-rose-950/40 border border-rose-900/50 text-rose-400 text-[11px] font-mono p-3 rounded mb-4">
          {error}
        </div>
      )}

      <div className="overflow-x-auto rounded border border-white/5">
        <table className="w-full text-left text-[11px] font-mono">
          <thead className="bg-zinc-900/50 border-b border-white/5 text-zinc-400 uppercase tracking-wider">
            <tr>
              <th className="py-2.5 px-4 font-semibold">Instance ID</th>
              <th className="py-2.5 px-4 font-semibold">Strategy</th>
              <th className="py-2.5 px-4 font-semibold">Instrument</th>
              <th className="py-2.5 px-4 font-semibold">Status</th>
              <th className="py-2.5 px-4 font-semibold text-right">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-white/5 text-zinc-300">
            {instances.length === 0 ? (
              <tr>
                <td colSpan={5} className="py-6 text-center text-zinc-500">
                  No strategies deployed on this account.
                </td>
              </tr>
            ) : (
              instances.map((inst) => {
                const isRunning = inst.status === "RUNNING";
                const isPaused = inst.status === "PAUSED";
                const isStopped = inst.status === "STOPPED";
                
                let statusColor = "text-zinc-400 bg-zinc-800";
                if (isRunning) statusColor = "text-emerald-400 bg-emerald-950/70 border border-emerald-800/50";
                if (isPaused) statusColor = "text-amber-400 bg-amber-950/70 border border-amber-800/50";
                if (isStopped) statusColor = "text-red-400 bg-red-950/70 border border-red-800/50";

                return (
                  <tr key={inst.instance_id} className="hover:bg-zinc-800/40 transition-colors">
                    <td className="py-3 px-3 font-semibold text-zinc-200" title={inst.instance_id}>
                      {inst.instance_id.slice(0, 18)}...
                    </td>
                    <td className="py-3 px-3">
                      <span className="bg-zinc-950 px-1.5 py-0.5 rounded border border-zinc-800 text-zinc-400">
                        {inst.strategy_name}
                      </span>
                    </td>
                    <td className="py-3 px-3 text-cyan-400 font-semibold">{inst.instrument_id}</td>
                    <td className="py-3 px-3">
                      <span className={`px-2 py-0.5 rounded text-[10px] font-bold ${statusColor}`}>
                        {inst.status}
                      </span>
                    </td>
                    <td className="py-3 px-3 text-right space-x-1.5">
                      {isRunning && (
                        <>
                          <button
                            onClick={() => handleCommand(inst.instance_id, "pause")}
                            className="p-1.5 bg-amber-600 hover:bg-amber-500 text-white rounded transition-colors inline-flex"
                            title="Pause"
                          >
                            <Pause className="w-3.5 h-3.5" />
                          </button>
                          <button
                            onClick={() => handleCommand(inst.instance_id, "close-position")}
                            className="px-2 py-1 bg-purple-600 hover:bg-purple-500 text-white text-[10px] rounded transition-colors inline-flex font-bold uppercase"
                            title="Flatten Position"
                          >
                            FLATTEN
                          </button>
                        </>
                      )}
                      {isPaused && (
                        <button
                          onClick={() => handleCommand(inst.instance_id, "resume")}
                          className="p-1.5 bg-emerald-600 hover:bg-emerald-500 text-white rounded transition-colors inline-flex"
                          title="Resume"
                        >
                          <Play className="w-3.5 h-3.5" />
                        </button>
                      )}
                      {!isStopped && (
                        <button
                          onClick={() => {
                            if(confirm(`Are you sure you want to STOP instance ${inst.instance_id}?`)) {
                              handleCommand(inst.instance_id, "stop");
                            }
                          }}
                          className="p-1.5 bg-red-600 hover:bg-red-500 text-white rounded transition-colors inline-flex"
                          title="Stop"
                        >
                          <Square className="w-3.5 h-3.5" />
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>

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
            // Optionally could do something here
          }}
        />
      )}
    </div>
  );
}
