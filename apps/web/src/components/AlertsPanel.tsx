"use client";

import React, { useEffect, useState } from "react";
import { Bell, Trash2, Plus, X, TrendingUp, TrendingDown } from "lucide-react";
import { getStreamClient } from "@/lib/ws";
import { fetchApi } from "@/lib/api";
import { UserAlert } from "@/lib/types";

export const AlertsPanel: React.FC = () => {
  const [alerts, setAlerts] = useState<UserAlert[]>([]);
  const [loading, setLoading] = useState(true);
  const [showCreateForm, setShowCreateForm] = useState(false);
  const [newSymbol, setNewSymbol] = useState("");
  const [newCondition, setNewCondition] = useState<"above" | "below">("above");
  const [newThreshold, setNewThreshold] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  const loadAlerts = async () => {
    setLoading(true);
    try {
      const data = await fetchApi("/alerts");
      setAlerts(data);
    } catch (err) {
      console.error("Failed to load alerts", err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadAlerts();

    const client = getStreamClient();
    const unsub = client.subscribe((msg: any) => {
      if (msg.type === "alert_triggered") {
        setAlerts((prev) =>
          prev.map((a) =>
            a.id === msg.alert_id
              ? { ...a, is_active: false, triggered_at: new Date().toISOString() }
              : a
          )
        );
      }
    });

    return () => unsub();
  }, []);

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newSymbol || !newThreshold) return;

    setCreating(true);
    setCreateError(null);
    try {
      await fetchApi("/alerts", {
        method: "POST",
        headers: { "X-CSRF-Token": localStorage.getItem("csrf_token") ?? "" },
        body: JSON.stringify({
          instrument_id: newSymbol.toUpperCase() + ".BINANCE",
          condition: newCondition,
          threshold: parseFloat(newThreshold),
        }),
      });
      setShowCreateForm(false);
      setNewSymbol("");
      setNewThreshold("");
      loadAlerts();
    } catch (err: any) {
      setCreateError(err.message || "Failed to create alert");
    } finally {
      setCreating(false);
    }
  };

  const handleDelete = async (id: string) => {
    try {
      await fetchApi(`/alerts/${id}`, {
        method: "DELETE",
        headers: { "X-CSRF-Token": localStorage.getItem("csrf_token") ?? "" },
      });
      setAlerts((prev) => prev.filter((a) => a.id !== id));
    } catch (err) {
      console.error("Failed to delete alert", err);
    }
  };

  return (
    <div className="h-full flex flex-col bg-zinc-950 text-zinc-100 font-mono">
      <div className="h-12 border-b border-white/5 flex items-center justify-between px-4 shrink-0">
        <h2 className="text-xs font-bold tracking-widest uppercase text-zinc-400">
          Price Alerts
        </h2>
        <button
          onClick={() => setShowCreateForm(!showCreateForm)}
          className="flex items-center space-x-1 text-xs px-2 py-1 bg-cyan-900/30 text-cyan-400 hover:bg-cyan-900/50 rounded transition-colors"
        >
          {showCreateForm ? <X className="w-3 h-3" /> : <Plus className="w-3 h-3" />}
          <span>{showCreateForm ? "CANCEL" : "NEW ALERT"}</span>
        </button>
      </div>

      {showCreateForm && (
        <div className="p-4 border-b border-white/5 bg-zinc-900/30 shrink-0">
          <form onSubmit={handleCreate} className="space-y-4">
            <div className="flex space-x-2">
              <input
                type="text"
                placeholder="BTCUSDT"
                value={newSymbol}
                onChange={(e) => setNewSymbol(e.target.value)}
                className="flex-1 bg-zinc-900 border border-white/10 rounded px-3 py-1.5 text-xs focus:outline-none focus:border-cyan-500"
                required
              />
              <select
                value={newCondition}
                onChange={(e) => setNewCondition(e.target.value as "above" | "below")}
                className="bg-zinc-900 border border-white/10 rounded px-2 py-1.5 text-xs focus:outline-none focus:border-cyan-500"
              >
                <option value="above">&gt; ABOVE</option>
                <option value="below">&lt; BELOW</option>
              </select>
              <input
                type="number"
                step="any"
                placeholder="Price"
                value={newThreshold}
                onChange={(e) => setNewThreshold(e.target.value)}
                className="flex-1 bg-zinc-900 border border-white/10 rounded px-3 py-1.5 text-xs focus:outline-none focus:border-cyan-500"
                required
              />
            </div>
            {createError && <div className="text-red-400 text-xs">{createError}</div>}
            <button
              type="submit"
              disabled={creating}
              className="w-full py-1.5 bg-cyan-600 hover:bg-cyan-500 disabled:opacity-50 text-white rounded text-xs font-bold transition-colors"
            >
              {creating ? "CREATING..." : "CREATE ALERT"}
            </button>
          </form>
        </div>
      )}

      <div className="flex-1 overflow-y-auto p-4 space-y-2">
        {loading ? (
          <div className="animate-pulse space-y-2">
            {[1, 2, 3].map((i) => (
              <div key={i} className="h-12 bg-zinc-900 rounded"></div>
            ))}
          </div>
        ) : alerts.length === 0 ? (
          <div className="h-full flex flex-col items-center justify-center text-zinc-500 space-y-2">
            <Bell className="w-8 h-8 opacity-20" />
            <p className="text-xs">No alerts set. Create one above.</p>
          </div>
        ) : (
          alerts.map((alert) => (
            <div
              key={alert.id}
              className={`flex items-center justify-between p-3 rounded border ${
                alert.is_active
                  ? "bg-zinc-900/50 border-white/5"
                  : "bg-zinc-900/20 border-white/5 opacity-50"
              }`}
            >
              <div className="flex items-center space-x-4">
                <div className="font-bold text-sm">
                  {alert.instrument_id.replace(".BINANCE", "")}
                </div>
                <div
                  className={`flex items-center space-x-1 text-xs px-2 py-0.5 rounded-sm ${
                    alert.condition === "above"
                      ? "bg-emerald-950 text-emerald-400"
                      : "bg-red-950 text-red-400"
                  }`}
                >
                  {alert.condition === "above" ? (
                    <TrendingUp className="w-3 h-3" />
                  ) : (
                    <TrendingDown className="w-3 h-3" />
                  )}
                  <span>{alert.condition.toUpperCase()}</span>
                </div>
                <div className="text-sm">
                  ${alert.threshold.toLocaleString(undefined, { minimumFractionDigits: 2 })}
                </div>
              </div>

              <div className="flex items-center space-x-4">
                {alert.is_active ? (
                  <span className="text-[10px] text-emerald-500 bg-emerald-950/50 px-2 py-0.5 rounded">
                    ACTIVE
                  </span>
                ) : (
                  <div className="text-right">
                    <span className="text-[10px] text-amber-500 bg-amber-950/50 px-2 py-0.5 rounded block">
                      TRIGGERED
                    </span>
                    {alert.triggered_at && (
                      <span className="text-[9px] text-zinc-500 block mt-1">
                        {new Date(alert.triggered_at).toLocaleTimeString()}
                      </span>
                    )}
                  </div>
                )}
                <button
                  onClick={() => handleDelete(alert.id)}
                  className="p-1.5 text-zinc-600 hover:text-red-400 hover:bg-red-950/30 rounded transition-colors"
                >
                  <Trash2 className="w-4 h-4" />
                </button>
              </div>
            </div>
          ))
        )}
      </div>

      <div className="p-2 border-t border-white/5 text-center text-[10px] text-zinc-600 shrink-0">
        Alerts evaluate every 15s against live Binance prices
      </div>
    </div>
  );
};
