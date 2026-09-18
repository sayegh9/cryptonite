"use client";

import { useState, useEffect } from "react";
import { fetchApi } from "@/lib/api";
import { X } from "lucide-react";

export function DeployStrategyModal({ onClose, onDeployed }: { onClose: () => void, onDeployed: () => void }) {
  const [strategies, setStrategies] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [deploying, setDeploying] = useState(false);
  const [error, setError] = useState("");

  const [selectedSlug, setSelectedSlug] = useState("");
  const [instrumentId, setInstrumentId] = useState("BTCUSDT.BINANCE");
  const [timeframe, setTimeframe] = useState("1-MINUTE-LAST-EXTERNAL");
  const [allocationUsd, setAllocationUsd] = useState(10000.0);
  const [parameters, setParameters] = useState<Record<string, any>>({});

  useEffect(() => {
    fetchApi("/strategies")
      .then((data) => {
        setStrategies(data);
        if (data.length > 0) {
          handleSelectStrategy(data[0].slug, data);
        }
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, []);

  const handleSelectStrategy = (slug: string, strats = strategies) => {
    setSelectedSlug(slug);
    const strat = strats.find((s) => s.slug === slug);
    if (strat) {
      setParameters(strat.default_parameters || {});
    }
  };

  const selectedStrategy = strategies.find((s) => s.slug === selectedSlug);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedStrategy) return;
    
    setDeploying(true);
    setError("");

    try {
      await fetchApi("/paper/strategies/deploy", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": localStorage.getItem("csrf_token") || "",
        },
        body: JSON.stringify({
          strategy_name: selectedStrategy.slug,
          strategy_version_id: selectedStrategy.active_version_id,
          instrument_id: instrumentId,
          timeframe: `${instrumentId}-${timeframe}`,
          allocation_usd: allocationUsd,
          parameters: parameters,
        }),
      });
      onDeployed();
    } catch (err: any) {
      setError(err.message || "Deployment failed");
      setDeploying(false);
    }
  };

  if (loading) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div className="bg-zinc-950 border border-white/10 w-full max-w-lg rounded shadow-2xl overflow-hidden flex flex-col">
        <div className="flex items-center justify-between px-5 py-4 border-b border-white/5 bg-zinc-900/50">
          <h2 className="text-sm font-bold text-zinc-100 uppercase tracking-wider">Deploy New Strategy</h2>
          <button onClick={onClose} className="text-zinc-500 hover:text-zinc-300 transition-colors">
            <X className="w-5 h-5" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="p-5 overflow-y-auto max-h-[80vh]">
          {error && (
            <div className="mb-4 bg-rose-950/40 border border-rose-900/50 text-rose-400 text-[11px] font-mono p-3 rounded">
              {error}
            </div>
          )}

          <div className="space-y-4">
            <div>
              <label className="block text-[10px] font-bold font-mono text-zinc-400 mb-1.5 uppercase tracking-wider">Strategy Protocol</label>
              <select
                value={selectedSlug}
                onChange={(e) => handleSelectStrategy(e.target.value)}
                className="w-full bg-zinc-900 border border-white/5 rounded px-3 py-2 text-sm text-zinc-200 focus:border-cyan-500 focus:outline-none"
              >
                {strategies.map((s) => (
                  <option key={s.slug} value={s.slug}>
                    {s.name} ({s.version_num})
                  </option>
                ))}
              </select>
            </div>

            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="block text-[10px] font-bold font-mono text-zinc-400 mb-1.5 uppercase tracking-wider">Instrument</label>
                <input
                  type="text"
                  value={instrumentId}
                  onChange={(e) => setInstrumentId(e.target.value)}
                  className="w-full bg-zinc-900 border border-white/5 rounded px-3 py-2 text-sm text-zinc-200 focus:border-cyan-500 focus:outline-none uppercase"
                  required
                />
              </div>
              <div>
                <label className="block text-[10px] font-bold font-mono text-zinc-400 mb-1.5 uppercase tracking-wider">Timeframe</label>
                <select
                  value={timeframe}
                  onChange={(e) => setTimeframe(e.target.value)}
                  className="w-full bg-zinc-900 border border-white/5 rounded px-3 py-2 text-sm text-zinc-200 focus:border-cyan-500 focus:outline-none"
                >
                  <option value="1-MINUTE-LAST-EXTERNAL">1 Minute</option>
                  <option value="5-MINUTE-LAST-EXTERNAL">5 Minute</option>
                  <option value="15-MINUTE-LAST-EXTERNAL">15 Minute</option>
                  <option value="1-HOUR-LAST-EXTERNAL">1 Hour</option>
                </select>
              </div>
              <div>
                <label className="block text-[10px] font-bold font-mono text-zinc-400 mb-1.5 uppercase tracking-wider">Allocation (USD)</label>
                <input
                  type="number"
                  step="100"
                  min="0"
                  value={allocationUsd}
                  onChange={(e) => setAllocationUsd(Number(e.target.value))}
                  className="w-full bg-zinc-900 border border-white/5 rounded px-3 py-2 text-sm text-zinc-200 focus:border-cyan-500 focus:outline-none"
                  required
                />
              </div>
            </div>

            {selectedStrategy && selectedStrategy.parameter_schema && Object.keys(selectedStrategy.parameter_schema).length > 0 && (
              <div className="pt-4 mt-2 border-t border-white/5">
                <h4 className="text-[10px] font-bold text-zinc-400 mb-3 uppercase tracking-wider">Parameters</h4>
                <div className="space-y-3">
                  {Object.entries(selectedStrategy.parameter_schema).map(([key, schema]: [string, any]) => (
                    <div key={key}>
                      <label className="flex items-center justify-between text-[10px] uppercase font-bold mb-1">
                        <span className="font-mono text-cyan-400">{key}</span>
                        <span className="text-zinc-500 tracking-wider">{schema.title}</span>
                      </label>
                      <input
                        type="number"
                        step={schema.type === "number" ? "0.0001" : "1"}
                        min={schema.minimum}
                        max={schema.maximum}
                        value={parameters[key] ?? schema.default}
                        onChange={(e) => setParameters({ ...parameters, [key]: Number(e.target.value) })}
                        className="w-full bg-zinc-900 border border-white/5 rounded px-3 py-1.5 text-sm font-mono text-zinc-200 focus:border-cyan-500 focus:outline-none"
                        required
                      />
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>

          <div className="mt-8 flex space-x-2">
            <button
              type="button"
              onClick={onClose}
              className="flex-1 py-2.5 bg-zinc-900 hover:bg-zinc-800 border border-white/5 text-zinc-300 text-xs font-bold uppercase tracking-wider rounded transition-colors"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={deploying}
              className="flex-1 py-2.5 bg-cyan-600 hover:bg-cyan-500 text-white text-xs font-bold uppercase tracking-wider rounded transition-colors"
            >
              {deploying ? "Dispatching..." : "Deploy Strategy"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
