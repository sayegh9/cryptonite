"use client";

import { useEffect, useState } from "react";
import { fetchApi } from "@/lib/api";
import {
  Database,
  Coins,
  ShieldCheck,
  ShieldAlert,
  RefreshCw,
  ArrowDownLeft,
  ArrowUpRight,
  Clock,
  AlertTriangle,
} from "lucide-react";

interface Balance {
  environment: string;
  account_id: string;
  asset: string;
  free: number;
  locked: number;
  total: number;
  reconciliation_status: string;
  updated_at: string;
}

interface Order {
  client_order_id: string;
  venue_order_id: string | null;
  strategy_id: string | null;
  instrument_id: string;
  side: string;
  order_type: string;
  time_in_force: string;
  price: number | null;
  quantity: number | null;
  filled_qty: number | null;
  status: string;
  raw_venue_status: string | null;
  reject_reason: string | null;
  created_at: string;
}

interface ReconciliationData {
  latest_run: {
    run_id: string;
    environment: string;
    account_id: string;
    trigger_type: string;
    status: string;
    balances_matched: boolean;
    orders_matched: boolean;
    positions_matched: boolean;
    diff_summary: any;
    started_at: string;
    completed_at: string;
  } | null;
  differences: Array<{
    difference_id: string;
    entity_type: string;
    entity_id: string;
    discrepancy_type: string;
    action_taken: string;
    resolved: boolean;
    created_at: string;
  }>;
  unresolved_count: number;
}

export function ReconciliationLedger() {
  const [balances, setBalances] = useState<Balance[]>([]);
  const [orders, setOrders] = useState<Order[]>([]);
  const [recon, setRecon] = useState<ReconciliationData | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");

  const loadData = async () => {
    try {
      const [balRes, ordRes, recRes] = await Promise.all([
        fetchApi("/control/balances"),
        fetchApi("/control/orders"),
        fetchApi("/control/reconciliation"),
      ]);
      setBalances(balRes);
      setOrders(ordRes);
      setRecon(recRes);
      setError("");
    } catch (err: any) {
      setError(err.message || "Failed to load ledger data");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  };

  useEffect(() => {
    loadData();
    const interval = setInterval(loadData, 5000);
    return () => clearInterval(interval);
  }, []);

  const handleManualRefresh = () => {
    setRefreshing(true);
    loadData();
  };

  if (loading) {
    return (
      <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-6 animate-pulse">
        <div className="h-6 w-48 bg-zinc-800 rounded mb-4"></div>
        <div className="h-32 bg-zinc-950 rounded"></div>
      </div>
    );
  }

  const hasDiscrepancy = (recon?.unresolved_count ?? 0) > 0;

  return (
    <div className="space-y-6">
      {/* Top Header & Refresh */}
      <div className="bg-zinc-950 border border-white/5 rounded p-6 flex items-center justify-between">
        <div className="flex items-center space-x-3">
          <Database className="w-5 h-5 text-cyan-400" />
          <h2 className="text-sm font-bold text-zinc-100 uppercase tracking-wider">Multi-Asset Ledger & Audit</h2>
        </div>
        <div className="flex items-center space-x-3">
          {recon?.latest_run && (
            <div className="text-[10px] font-mono text-zinc-500 flex items-center space-x-1">
              <Clock className="w-3.5 h-3.5" />
              <span>
                Audited {new Date(recon.latest_run.completed_at || recon.latest_run.started_at).toLocaleTimeString()}
              </span>
            </div>
          )}
          <button
            onClick={handleManualRefresh}
            className={`p-1.5 rounded bg-zinc-900 hover:bg-zinc-800 border border-white/5 text-zinc-300 transition-colors ${
              refreshing ? "animate-spin text-cyan-400" : ""
            }`}
            title="Refresh Ledger"
          >
            <RefreshCw className="w-4 h-4" />
          </button>
        </div>
      </div>

      {error && (
        <div className="bg-red-950/40 border border-red-900 text-red-400 text-xs p-3 rounded">
          {error}
        </div>
      )}

      {/* Reconciliation Health Shield Card */}
      <div
        className={`border rounded-lg p-4 flex items-center justify-between ${
          hasDiscrepancy
            ? "bg-red-950/20 border-red-900/60 text-red-400"
            : "bg-emerald-950/20 border-emerald-900/40 text-emerald-400"
        }`}
      >
        <div className="flex items-center space-x-3">
          {hasDiscrepancy ? (
            <ShieldAlert className="w-6 h-6 text-red-500 animate-pulse" />
          ) : (
            <ShieldCheck className="w-6 h-6 text-emerald-400" />
          )}
          <div>
            <div className="font-bold text-sm">
              {hasDiscrepancy
                ? `RECONCILIATION ANOMALIES DETECTED (${recon?.unresolved_count} UNRESOLVED)`
                : "AUTHORITATIVE RECONCILER: SYNCHRONIZED & RECONCILED"}
            </div>
            <div className="text-xs text-zinc-400 mt-0.5">
              Balances Matched:{" "}
              <span className={recon?.latest_run?.balances_matched ? "text-emerald-400 font-semibold" : "text-red-400 font-semibold"}>
                {recon?.latest_run?.balances_matched ? "YES" : "NO"}
              </span>{" "}
              • Orders Matched:{" "}
              <span className={recon?.latest_run?.orders_matched ? "text-emerald-400 font-semibold" : "text-red-400 font-semibold"}>
                {recon?.latest_run?.orders_matched ? "YES" : "NO"}
              </span>
            </div>
          </div>
        </div>
        <div className="text-right font-mono text-xs text-zinc-500">
          Run ID: {recon?.latest_run?.run_id ? recon.latest_run.run_id.slice(0, 8) + "..." : "NONE"}
        </div>
      </div>

      {/* Multi-Asset Balance Cards Grid */}
      <div>
        <h3 className="text-xs font-mono uppercase tracking-wider text-zinc-400 mb-3 flex items-center space-x-1.5">
          <Coins className="w-4 h-4 text-cyan-400" />
          <span>Settled Trading Balances (Durable Ledger)</span>
        </h3>
        <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 gap-3">
          {balances.length === 0 ? (
            <div className="col-span-full p-4 bg-zinc-900 border border-zinc-800 rounded text-center text-xs text-zinc-500 font-mono">
              No balances registered for environment
            </div>
          ) : (
            balances.map((b) => (
              <div
                key={`${b.account_id}-${b.asset}`}
                className="bg-zinc-950 border border-white/5 rounded p-4 hover:border-zinc-700/50 transition-colors"
              >
                <div className="flex items-center justify-between mb-2">
                  <span className="font-extrabold text-sm tracking-wide text-zinc-100">{b.asset}</span>
                  <span className="text-[10px] font-mono px-1.5 py-0.5 bg-zinc-900 border border-white/5 text-zinc-400 rounded">
                    {b.account_id.length > 14 ? b.account_id.slice(0, 12) + "..." : b.account_id}
                  </span>
                </div>
                <div className="text-lg font-mono font-bold text-cyan-400">
                  {b.total.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 6 })}
                </div>
                <div className="flex justify-between text-[10px] font-mono text-zinc-500 mt-1">
                  <span>Free: {b.free.toLocaleString(undefined, { maximumFractionDigits: 4 })}</span>
                  <span>Locked: {b.locked.toLocaleString(undefined, { maximumFractionDigits: 4 })}</span>
                </div>
              </div>
            ))
          )}
        </div>
      </div>

      {/* Recent Trading Orders Table */}
      <div className="bg-zinc-950 border border-white/5 rounded p-6">
        <h3 className="text-xs font-mono uppercase tracking-wider text-zinc-400 mb-4">
          Recent Orders ({orders.length})
        </h3>
        <div className="overflow-x-auto max-h-72 overflow-y-auto rounded border border-white/5">
          <table className="w-full text-left text-[11px] font-mono">
            <thead className="bg-zinc-900/50 border-b border-white/5 text-zinc-400 uppercase tracking-wider sticky top-0">
              <tr>
                <th className="py-2.5 px-4 font-semibold">Time</th>
                <th className="py-2.5 px-4 font-semibold">Instrument</th>
                <th className="py-2.5 px-4 font-semibold">Side</th>
                <th className="py-2.5 px-4 font-semibold">Type</th>
                <th className="py-2.5 px-4 font-semibold text-right">Quantity</th>
                <th className="py-2.5 px-4 font-semibold text-right">Price</th>
                <th className="py-2.5 px-4 font-semibold text-right">Filled</th>
                <th className="py-2.5 px-4 font-semibold text-center">Status</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-white/5 text-zinc-300">
              {orders.length === 0 ? (
                <tr>
                  <td colSpan={8} className="py-6 text-center text-zinc-500">
                    No orders logged in trading_orders
                  </td>
                </tr>
              ) : (
                orders.map((o) => {
                  const isBuy = o.side === "BUY";
                  let statusColor = "bg-zinc-800 text-zinc-300";
                  if (o.status === "FILLED") statusColor = "bg-emerald-950/70 text-emerald-400 border border-emerald-800";
                  else if (o.status === "NEW" || o.status === "ACCEPTED") statusColor = "bg-cyan-950/70 text-cyan-300 border border-cyan-800";
                  else if (o.status === "CANCELLED") statusColor = "bg-amber-950/70 text-amber-300 border border-amber-800";
                  else if (o.status === "REJECTED") statusColor = "bg-red-950/70 text-red-300 border border-red-800";

                  return (
                    <tr key={o.client_order_id} className="hover:bg-zinc-800/40 transition-colors">
                      <td className="py-2 px-3 text-zinc-500 whitespace-nowrap">
                        {new Date(o.created_at).toLocaleTimeString()}
                      </td>
                      <td className="py-2 px-3 font-semibold text-zinc-200">
                        {o.instrument_id || "BTCUSDT"}
                      </td>
                      <td className="py-2 px-3">
                        <span
                          className={`inline-flex items-center space-x-0.5 px-1.5 py-0.5 rounded text-[10px] font-bold ${
                            isBuy ? "bg-emerald-950/60 text-emerald-400" : "bg-red-950/60 text-red-400"
                          }`}
                        >
                          {isBuy ? <ArrowDownLeft className="w-3 h-3 mr-0.5" /> : <ArrowUpRight className="w-3 h-3 mr-0.5" />}
                          {o.side}
                        </span>
                      </td>
                      <td className="py-2 px-3 text-zinc-400">{o.order_type}</td>
                      <td className="py-2 px-3 text-right">{o.quantity ?? "—"}</td>
                      <td className="py-2 px-3 text-right">
                        {o.price ? `$${o.price.toLocaleString()}` : "MARKET"}
                      </td>
                      <td className="py-2 px-3 text-right text-zinc-400">
                        {o.filled_qty ?? "0"}
                      </td>
                      <td className="py-2 px-3 text-center">
                        <span className={`px-2 py-0.5 rounded text-[10px] font-bold ${statusColor}`}>
                          {o.status}
                        </span>
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
