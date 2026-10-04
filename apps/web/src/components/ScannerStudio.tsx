"use client";

import { fetchApi } from "@/lib/api";
import React, { useEffect, useState } from "react";
import { ScanSpec, ScanJob, SignalCandidate } from "@/lib/types";
import { getStreamClient } from "@/lib/ws";
import {
  Play,
  RotateCcw,
  Sliders,
  CheckCircle2,
  ExternalLink,
  Flame,
  Clock,
  ShieldAlert,
} from "lucide-react";

interface Props {
  onViewOnChart: (symbol: string, timeframe: string, candleTime: number) => void;
  onCreateClick: () => void;
}

export const ScannerStudio: React.FC<Props> = ({ onViewOnChart, onCreateClick }) => {
  const [specs, setSpecs] = useState<ScanSpec[]>([]);
  const [selectedSpecId, setSelectedSpecId] = useState<string>("");
  const [loadingSpecs, setLoadingSpecs] = useState<boolean>(true);
  const [, setActiveJob] = useState<ScanJob | null>(null);
  const [running, setRunning] = useState<boolean>(false);
  const [candidates, setCandidates] = useState<SignalCandidate[]>([]);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);

  // Load Scan Specs & Presets
  const fetchSpecs = async () => {
    try {
      setLoadingSpecs(true);
      const data = await fetchApi("/scanners");
      setSpecs(data);
      if (data.length > 0 && !selectedSpecId) {
        setSelectedSpecId(data[0].id);
      }
    } catch (error) {
      console.error("[ScannerStudio] Error loading specs:", error);
    } finally {
      setLoadingSpecs(false);
    }
  };

  // Load existing signals
  const fetchSignals = async () => {
    try {
      const data = await fetchApi("/signals?limit=50");
      const list = Array.isArray(data) ? data : (data.signals || []);
      setCandidates(list);
    } catch (err) {
      console.error("[ScannerStudio] Error loading signals:", err);
    }
  };

  useEffect(() => {
    fetchSpecs();
    fetchSignals();
  }, []);

  // Listen to live signal candidate stream
  useEffect(() => {
    const client = getStreamClient();
    const unsubscribe = client.subscribe((msg: any) => {
      if (msg.type === "signal_candidate") {
        const candidateData = msg.data || msg;
        setCandidates((prev) => [candidateData as SignalCandidate, ...prev.slice(0, 99)]);
      }
    });
    return () => {
      unsubscribe();
    };
  }, []);

  const selectedSpec = specs.find((s) => s.id === selectedSpecId);

  // Trigger Asynchronous Scan Run
  const handleTriggerScan = async () => {
    if (!selectedSpec || !selectedSpec.active_version_id) return;
    setRunning(true);
    setStatusMessage("Enqueuing scan job...");

    try {
      const reqId = `req-${crypto.randomUUID()}`;
      const job: ScanJob = await fetchApi(`/scanners/${selectedSpec.id}/run`, {
        method: "POST",
        headers: { "X-CSRF-Token": localStorage.getItem("csrf_token") ?? "" },
        body: JSON.stringify({
          request_id: reqId,
          spec_version_id: selectedSpec.active_version_id,
        }),
      });

      setActiveJob(job);
      setStatusMessage(`Job ${job.id.slice(0, 8)} accepted. Running vectorized Polars scan...`);

      // Poll job status until completed
      const interval = setInterval(async () => {
        try {
          const pollData: ScanJob = await fetchApi(`/scanners/jobs/${job.id}`);
          setActiveJob(pollData);

          if (pollData.status === "COMPLETED") {
            clearInterval(interval);
            setRunning(false);
            setStatusMessage(`Scan completed successfully! ${pollData.candidate_count ?? 0} candidates found.`);
            fetchSignals();
          } else if (pollData.status === "FAILED") {
            clearInterval(interval);
            setRunning(false);
            setStatusMessage(`Scan failed: ${pollData.error_message || "Unknown error"}`);
          }
        } catch {
          // ignore poll error
        }
      }, 1000);
    } catch (err: any) {
      setRunning(false);
      setStatusMessage(`Error: ${err.message}`);
    }
  };

  return (
    <div id="scanner-studio" className="flex flex-col h-full w-full bg-zinc-950 border-t border-white/5">
      {/* Studio Header / Controls */}
      <div className="flex flex-wrap items-center justify-between p-3 bg-zinc-950 border-b border-white/5 gap-2">
        <div className="flex items-center space-x-3">
          <div className="flex items-center space-x-1.5 text-cyan-400 font-semibold text-xs uppercase tracking-wider">
            <Sliders className="w-4 h-4" />
            <span>Polars Scanner Studio</span>
          </div>

          {/* Preset Selector */}
          <select
            aria-label="Select scanner preset"
            value={selectedSpecId}
            onChange={(e) => setSelectedSpecId(e.target.value)}
            disabled={loadingSpecs || running}
            className="bg-zinc-900 border border-white/5 rounded px-2.5 py-1 text-xs text-zinc-200 focus-visible:ring-2 focus-visible:ring-cyan-500/50 focus:outline-none font-medium transition-all"
          >
            {specs.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name} (v{s.active_version?.version_num || 1})
              </option>
            ))}
          </select>

          {selectedSpec && (
            <span className="text-2xs text-zinc-500 max-w-md truncate hidden lg:inline">
              {selectedSpec.description}
            </span>
          )}
        </div>

        {/* Action Buttons */}
        <div className="flex items-center space-x-2">
          {statusMessage && (
            <span className="text-2xs font-mono text-zinc-400 mr-2 flex items-center space-x-1">
              {running ? (
                <RotateCcw className="w-3 h-3 animate-spin text-cyan-400" />
              ) : (
                <CheckCircle2 className="w-3 h-3 text-emerald-400" />
              )}
              <span>{statusMessage}</span>
            </span>
          )}

          <button
            onClick={onCreateClick}
            className="flex items-center space-x-1.5 px-3 py-1.5 rounded text-xs font-bold transition-all shadow-sm bg-cyan-600 hover:bg-cyan-500 text-zinc-950 shadow-cyan-500/20 focus-visible:ring-2 focus-visible:ring-cyan-500/50 outline-none"
          >
            <span>Create Scanner</span>
          </button>
          
          <button
            onClick={handleTriggerScan}
            disabled={running || !selectedSpec}
            className={`flex items-center space-x-1.5 px-3 py-1.5 rounded text-xs font-bold transition-all shadow-sm focus-visible:ring-2 focus-visible:ring-cyan-500/50 outline-none ${
              running
                ? "bg-zinc-800 text-zinc-500 cursor-not-allowed"
                : "bg-cyan-600 hover:bg-cyan-500 text-zinc-950 shadow-cyan-500/20"
            }`}
          >
            <Play className="w-3.5 h-3.5 fill-current" />
            <span>{running ? "Scanning..." : "Run Scanner"}</span>
          </button>
        </div>
      </div>

      {/* Main Studio Body: Rules Preview & Candidate Stream */}
      <div className="flex-1 grid grid-cols-1 lg:grid-cols-3 min-h-0 divide-y lg:divide-y-0 lg:divide-x divide-white/5 overflow-hidden">
        {/* Rules & Universe Inspection Pane */}
        <div className="p-4 bg-zinc-950 overflow-y-auto space-y-4 font-mono text-xs">
          <div className="flex items-center justify-between text-zinc-400 border-b border-white/5 pb-2">
            <span className="font-semibold text-2xs uppercase tracking-wider text-zinc-300">
              Active Spec Rules
            </span>
            <span className="text-2xs text-zinc-500">
              Hash: {selectedSpec?.active_version?.spec_hash?.slice(0, 8)}...
            </span>
          </div>

          {selectedSpec?.active_version && (
            <div className="space-y-2">
              <div>
                <span className="text-zinc-500 text-2xs uppercase">Universe:</span>
                <div className="flex flex-wrap gap-1 mt-0.5">
                  {selectedSpec.active_version.universe.instrument_ids.map((id) => (
                    <span key={id} className="px-1.5 py-0.5 bg-zinc-900 border border-zinc-800 rounded text-2xs text-cyan-400">
                      {id.split(".")[0]}
                    </span>
                  ))}
                  {selectedSpec.active_version.universe.timeframes.map((tf) => (
                    <span key={tf} className="px-1.5 py-0.5 bg-zinc-900 border border-zinc-800 rounded text-2xs text-zinc-400">
                      {tf}
                    </span>
                  ))}
                </div>
              </div>

              <div>
                <span className="text-zinc-500 text-2xs uppercase">Rule Groups:</span>
                <div className="space-y-1.5 mt-1">
                  {(Array.isArray(selectedSpec.active_version.rules)
                    ? selectedSpec.active_version.rules
                    : []
                  ).map((rg: any, idx: number) => (
                    <div key={idx} className="p-2 bg-zinc-900/70 border border-zinc-800 rounded">
                      <div className="flex items-center justify-between text-2xs font-bold text-zinc-300 mb-1">
                        <span className="uppercase text-cyan-300">{rg.category}</span>
                        <span className="text-zinc-500">{rg.logic}</span>
                      </div>
                      <div className="space-y-0.5">
                        {(rg.conditions || []).map((c: any, cIdx: number) => (
                          <div key={cIdx} className="text-2xs text-zinc-400">
                            {c.field} <span className="text-amber-400 font-bold">{c.operator}</span> {c.value}
                          </div>
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          )}
        </div>

        {/* Signal Candidates Stream Pane */}
        <div className="lg:col-span-2 flex flex-col h-full min-h-0 bg-zinc-950">
          <div className="flex items-center justify-between px-4 py-3 border-b border-white/5 bg-zinc-950">
            <div className="flex items-center space-x-2">
              <Flame className="w-3.5 h-3.5 text-amber-400" />
              <span className="text-xs font-bold text-zinc-200">
                Signal Candidates ({candidates.length})
              </span>
            </div>
            <span className="text-2xs text-zinc-500 font-mono">
              Zero-Trading Authority: Analytical Signals Only
            </span>
          </div>

          <div className="flex-1 overflow-y-auto">
            {candidates.length === 0 ? (
              <div className="flex flex-col items-center justify-center h-full p-6 text-center text-zinc-500">
                <ShieldAlert className="w-8 h-8 mb-2 opacity-40" />
                <span className="text-xs font-mono">No signals generated yet. Click "Run Scanner" to evaluate.</span>
              </div>
            ) : (
              candidates.map((cand, idx) => {
                const instId = cand.instrument_id || (cand as any).symbol || "UNKNOWN";
                const rawSym = instId.includes(".") ? instId.split(".")[0] : instId;
                const openNs = cand.bar_open_ns || ((cand as any).time ? (cand as any).time * 1_000_000_000 : Date.now() * 1_000_000);
                const candleTimeSec = Math.floor(openNs / 1_000_000_000);
                const dateStr = new Date(candleTimeSec * 1000).toLocaleTimeString([], {
                  hour: "2-digit",
                  minute: "2-digit",
                  second: "2-digit",
                });

                return (
                  <div
                    key={cand.id || `candidate-${idx}-${openNs}`}
                    className="p-3 hover:bg-zinc-900/40 transition-colors flex flex-col sm:flex-row sm:items-center justify-between gap-3"
                  >
                    <div className="flex flex-col space-y-1">
                      <div className="flex items-center space-x-2">
                        <span className="font-mono font-bold text-xs text-zinc-100">
                          {rawSym}
                        </span>
                        <span className="px-1.5 py-0.2 text-2xs font-mono bg-zinc-800 text-cyan-400 rounded">
                          {cand.timeframe}
                        </span>
                        <span className="text-2xs font-mono text-zinc-500 flex items-center space-x-1">
                          <Clock className="w-2.5 h-2.5" />
                          <span>{dateStr}</span>
                        </span>
                      </div>

                      <div className="flex flex-wrap gap-1">
                        {(cand.matched_rules || []).map((r, i) => (
                          <span
                            key={i}
                            className="px-1.5 py-0.5 text-2xs font-mono bg-emerald-950/60 text-emerald-400 border border-emerald-800/40 rounded"
                          >
                            {r}
                          </span>
                        ))}
                      </div>
                    </div>

                    {/* Metrics and Risk Levels */}
                    <div className="flex items-center space-x-4 font-mono text-xs">
                      <div className="flex flex-col items-end">
                        <span className="text-2xs text-zinc-500">Entry</span>
                        <span className="font-semibold text-zinc-200">
                          ${Number(cand.suggested_entry).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                        </span>
                      </div>
                      <div className="flex flex-col items-end">
                        <span className="text-2xs text-red-400/80">Stop</span>
                        <span className="font-semibold text-red-400">
                          ${Number(cand.suggested_stop).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                        </span>
                      </div>
                      <div className="flex flex-col items-end">
                        <span className="text-2xs text-emerald-400/80">Target</span>
                        <span className="font-semibold text-emerald-400">
                          ${Number(cand.suggested_target).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                        </span>
                      </div>

                      {/* View on Chart Button */}
                      <button
                        onClick={() => onViewOnChart(rawSym, cand.timeframe, candleTimeSec)}
                        title="View candle on chart"
                        className="p-1.5 bg-zinc-900 hover:bg-cyan-500/20 hover:text-cyan-400 text-zinc-400 border border-zinc-800 rounded transition-all flex items-center space-x-1"
                      >
                        <ExternalLink className="w-3.5 h-3.5" />
                        <span className="text-2xs font-sans">Chart</span>
                      </button>
                    </div>
                  </div>
                );
              })
            )}
          </div>
        </div>
      </div>
    </div>
  );
};
