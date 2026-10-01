"use client";

import { useState, useEffect } from "react";
import {
  XCircle,
  Trash2,
  Code2,
  Layers,
  Copy,
  Check,
  RefreshCw,
  } from "lucide-react";
import { Strategy } from "@/lib/types";

interface StrategyManagerModalProps {
  onClose: () => void;
  onStrategiesChanged: () => void;
}

export function StrategyManagerModal({
  onClose,
  onStrategiesChanged,
}: StrategyManagerModalProps) {
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedStrategySlug, setSelectedStrategySlug] = useState<string | null>(null);
  const [detailedStrategy, setDetailedStrategy] = useState<any | null>(null);
  const [codeLoading, setCodeLoading] = useState(false);
  const [copiedCode, setCopiedCode] = useState(false);
  const [deletingSlug, setDeletingSlug] = useState<string | null>(null);

  const fetchStrategies = async () => {
    setLoading(true);
    try {
      const res = await fetch("/api/v1/strategies");
      if (res.ok) {
        const data: Strategy[] = await res.json();
        const unique = Array.from(new Map(data.map((s) => [s.slug, s])).values());
        setStrategies(unique);
        if (unique.length > 0 && !selectedStrategySlug) {
          loadStrategyDetails(unique[0].slug);
        }
      }
    } catch (err) {
      console.error("Failed fetching strategies", err);
    } finally {
      setLoading(false);
    }
  };

  const loadStrategyDetails = async (slug: string) => {
    setSelectedStrategySlug(slug);
    setCodeLoading(true);
    try {
      const res = await fetch(`/api/v1/strategies/${slug}`);
      if (res.ok) {
        const data = await res.json();
        setDetailedStrategy(data);
      }
    } catch (err) {
      console.error("Failed loading strategy details", err);
    } finally {
      setCodeLoading(false);
    }
  };

  useEffect(() => {
    fetchStrategies();
  }, []);

  const handleDelete = async (slug: string, name: string) => {
    if (!confirm(`Are you sure you want to permanently delete '${name}'?`)) {
      return;
    }
    setDeletingSlug(slug);
    try {
      const res = await fetch(`/api/v1/strategies/${slug}`, {
        method: "DELETE",
        credentials: "include",
        headers: { "X-CSRF-Token": localStorage.getItem("csrf_token") ?? "" },
      });
      if (res.ok) {
        onStrategiesChanged();
        await fetchStrategies();
        if (selectedStrategySlug === slug) {
          setSelectedStrategySlug(null);
          setDetailedStrategy(null);
        }
      } else {
        const err = await res.json().catch(() => ({}));
        alert(err.detail || "Failed to delete strategy");
      }
    } catch (e: any) {
      alert("Error deleting strategy: " + e.message);
    } finally {
      setDeletingSlug(null);
    }
  };

  const handleCopyCode = () => {
    if (!detailedStrategy?.python_code) return;
    navigator.clipboard.writeText(detailedStrategy.python_code);
    setCopiedCode(true);
    setTimeout(() => setCopiedCode(false), 2000);
  };

  return (
    <div
      className="fixed inset-0 bg-black/75 backdrop-blur-sm z-50 flex items-center justify-center p-4 font-mono"
      onClick={onClose}
    >
      <div
        className="w-full max-w-4xl bg-zinc-950 border border-zinc-800 rounded-xl shadow-2xl overflow-hidden flex flex-col h-[85vh]"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Modal Header */}
        <div className="p-4 border-b border-zinc-800 flex justify-between items-center bg-zinc-900/60">
          <div className="flex items-center space-x-2">
            <div className="p-1.5 rounded bg-cyan-950/80 border border-cyan-700/60 text-cyan-400">
              <Layers className="w-4 h-4" />
            </div>
            <div>
              <h2 className="font-bold text-white text-sm">STRATEGY REGISTRY & MANAGEMENT</h2>
              <p className="text-[10px] text-zinc-400">
                Inspect code, review parameters, and delete custom/AI-generated strategies.
              </p>
            </div>
          </div>
          <div className="flex items-center space-x-2">
            <button
              onClick={fetchStrategies}
              className="p-1.5 bg-zinc-900 hover:bg-zinc-800 text-zinc-400 rounded border border-zinc-800 transition-colors"
              title="Refresh list"
            >
              <RefreshCw className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`} />
            </button>
            <button
              onClick={onClose}
              className="text-zinc-500 hover:text-white transition-colors p-1 rounded hover:bg-zinc-900"
            >
              <XCircle className="w-5 h-5" />
            </button>
          </div>
        </div>

        {/* Modal Body: 2 Columns */}
        <div className="flex-1 min-h-0 flex flex-col md:flex-row overflow-hidden">
          {/* Left: Strategy List */}
          <div className="w-full md:w-80 border-r border-zinc-800 flex flex-col bg-zinc-950/50">
            <div className="p-3 border-b border-zinc-800/80 bg-zinc-900/30 text-[10px] font-bold text-zinc-400 uppercase tracking-wider flex justify-between items-center">
              <span>Registered Strategies</span>
              <span className="text-zinc-500 font-normal">({strategies.length})</span>
            </div>
            <div className="flex-1 overflow-y-auto p-2 space-y-1.5">
              {strategies.map((strat) => {
                const isSelected = selectedStrategySlug === strat.slug;
                const isCore = ["rsi-reversal", "ema-cross", "supertrend"].includes(strat.slug);

                return (
                  <div
                    key={strat.slug}
                    onClick={() => loadStrategyDetails(strat.slug)}
                    className={`p-2.5 rounded-lg border transition-all cursor-pointer flex items-center justify-between group ${
                      isSelected
                        ? "bg-cyan-950/40 border-cyan-600/60 text-white"
                        : "bg-zinc-900/40 border-zinc-800/60 text-zinc-300 hover:bg-zinc-900 hover:border-zinc-700"
                    }`}
                  >
                    <div className="min-w-0 pr-2">
                      <div className="font-bold text-xs truncate flex items-center space-x-1.5">
                        <span className="truncate">{strat.name}</span>
                        {isCore ? (
                          <span className="px-1 py-0.2 rounded text-[8.5px] bg-emerald-950 text-emerald-400 border border-emerald-800 uppercase flex-none">
                            Core
                          </span>
                        ) : (
                          <span className="px-1 py-0.2 rounded text-[8.5px] bg-indigo-950 text-indigo-300 border border-indigo-800 uppercase flex-none">
                            AI
                          </span>
                        )}
                      </div>
                      <div className="text-[10px] text-zinc-500 font-mono truncate mt-0.5">
                        {strat.slug}
                      </div>
                    </div>

                    {!isCore && (
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          handleDelete(strat.slug, strat.name);
                        }}
                        disabled={deletingSlug === strat.slug}
                        className="p-1.5 text-zinc-500 hover:text-rose-400 rounded hover:bg-zinc-800 opacity-80 group-hover:opacity-100 transition-all flex-none"
                        title="Delete Strategy"
                      >
                        <Trash2 className="w-3.5 h-3.5" />
                      </button>
                    )}
                  </div>
                );
              })}
            </div>
          </div>

          {/* Right: Strategy Inspection Details */}
          <div className="flex-1 flex flex-col bg-zinc-950 overflow-hidden">
            {detailedStrategy ? (
              <div className="flex-1 overflow-y-auto p-5 space-y-5">
                {/* Strategy Header Info */}
                <div className="flex items-start justify-between border-b border-zinc-800/80 pb-4">
                  <div>
                    <div className="text-lg font-bold text-white flex items-center space-x-2">
                      <span>{detailedStrategy.name}</span>
                      {detailedStrategy.slug.startsWith("ai-gen") ? (
                        <span className="px-2 py-0.5 rounded text-[10px] bg-indigo-950/80 text-indigo-300 border border-indigo-700/60 font-bold uppercase">
                          AI Generated
                        </span>
                      ) : (
                        <span className="px-2 py-0.5 rounded text-[10px] bg-emerald-950/80 text-emerald-300 border border-emerald-700/60 font-bold uppercase">
                          Core Template
                        </span>
                      )}
                    </div>
                    <div className="text-xs text-zinc-400 mt-1 font-sans">
                      {detailedStrategy.description || "No description provided."}
                    </div>
                    <div className="text-[10px] text-zinc-500 font-mono mt-1">
                      Slug: <span className="text-zinc-400">{detailedStrategy.slug}</span> • Author:{" "}
                      <span className="text-zinc-400">{detailedStrategy.author}</span> • Active Version:{" "}
                      <span className="text-cyan-400">v{detailedStrategy.version_num}</span>
                    </div>
                  </div>

                  {!["rsi-reversal", "ema-cross", "supertrend"].includes(detailedStrategy.slug) && (
                    <button
                      onClick={() => handleDelete(detailedStrategy.slug, detailedStrategy.name)}
                      className="px-3 py-1.5 bg-rose-950 hover:bg-rose-900 border border-rose-800 text-rose-300 rounded text-xs font-bold transition-colors flex items-center space-x-1.5 flex-none"
                    >
                      <Trash2 className="w-3.5 h-3.5" />
                      <span>Delete Strategy</span>
                    </button>
                  )}
                </div>

                {/* Default Parameters */}
                <div>
                  <div className="text-[10px] font-bold text-zinc-400 uppercase tracking-wider mb-2">
                    Default Parameters & Schema
                  </div>
                  <pre className="p-3 bg-zinc-900/90 border border-zinc-800 rounded-lg text-[10.5px] text-zinc-300 overflow-x-auto">
                    {JSON.stringify(
                      {
                        default_parameters: detailedStrategy.default_parameters,
                        parameter_schema: detailedStrategy.parameter_schema,
                      },
                      null,
                      2
                    )}
                  </pre>
                </div>

                {/* Python Source Code */}
                <div>
                  <div className="flex items-center justify-between mb-2">
                    <div className="text-[10px] font-bold text-zinc-400 uppercase tracking-wider flex items-center space-x-1.5">
                      <Code2 className="w-3.5 h-3.5 text-cyan-400" />
                      <span>Python Strategy Implementation</span>
                    </div>
                    {detailedStrategy.python_code && (
                      <button
                        onClick={handleCopyCode}
                        className="px-2.5 py-1 bg-zinc-900 hover:bg-zinc-800 text-zinc-300 border border-zinc-700/60 rounded text-[10px] font-bold transition-colors flex items-center space-x-1"
                      >
                        {copiedCode ? (
                          <>
                            <Check className="w-3 h-3 text-emerald-400" />
                            <span className="text-emerald-400">Copied!</span>
                          </>
                        ) : (
                          <>
                            <Copy className="w-3 h-3" />
                            <span>Copy Code</span>
                          </>
                        )}
                      </button>
                    )}
                  </div>

                  {codeLoading ? (
                    <div className="p-8 text-center text-zinc-500 animate-pulse">
                      Loading strategy source code...
                    </div>
                  ) : detailedStrategy.python_code ? (
                    <pre className="p-4 bg-zinc-900/95 border border-zinc-800 rounded-lg text-[11px] text-zinc-200 overflow-x-auto leading-relaxed max-h-96">
                      <code>{detailedStrategy.python_code}</code>
                    </pre>
                  ) : (
                    <div className="p-6 bg-zinc-900/30 border border-zinc-800/80 rounded-lg text-center text-zinc-500 text-xs font-sans">
                      Core compiled Python strategy (defined natively in packages/strategies/registry.py).
                    </div>
                  )}
                </div>
              </div>
            ) : (
              <div className="flex-1 flex items-center justify-center p-8 text-center text-zinc-500">
                Select a strategy from the left panel to inspect details.
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
