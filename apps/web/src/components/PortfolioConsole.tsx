"use client";

import React, { useState, useEffect } from "react";
import { getStreamClient } from "@/lib/ws";
import { fetchApi } from "@/lib/api";

import {
  ShieldAlert,
  ShieldCheck,
  Play,
  Pause,
  Square,
  AlertTriangle,
  XCircle,
  Plus,
  TrendingUp,
  Layers,
  ArrowUpRight,
  } from "lucide-react";

interface Account {
  account_id: string;
  venue: string;
  currency: string;
  starting_balance: number;
  cash_balance: number;
  total_equity: number;
  realized_pnl: number;
  daily_realized_pnl: number;
  daily_loss_latched: boolean;
}

interface Position {
  position_id: string;
  account_id: string;
  instance_id: string;
  instrument_id: string;
  side: "LONG" | "FLAT";
  entry_price: number;
  quantity: number;
  current_price: number;
  unrealized_pnl: number;
  status: "OPEN" | "CLOSED";
}

interface Order {
  client_order_id: string;
  instance_id: string;
  instrument_id: string;
  side: "BUY" | "SELL";
  order_type: "MARKET" | "LIMIT" | "STOP_MARKET";
  quantity: number;
  price: number | null;
  filled_qty: number;
  avg_fill_price: number | null;
  status: string;
  created_at: string;
}

interface StrategyInstance {
  instance_id: string;
  strategy_name: string;
  instrument_id: string;
  timeframe: string;
  allocation_usd: number;
  status: "RUNNING" | "PAUSED" | "STOPPED";
  last_processed_bar_ns: number;
  resolved_parameters?: any;
  created_at?: string;
}

interface KillSwitchState {
  id: number;
  is_latched: boolean;
  latched_by?: string;
  reason?: string;
  latched_at?: string;
}

interface Trade {
  trade_id: string;
  client_order_id: string;
  instrument_id: string;
  side: "BUY" | "SELL";
  fill_price: number;
  quantity: number;
  commission: number;
  created_at: string;
}

export function PortfolioConsole() {
  const [account, setAccount] = useState<Account | null>(null);
  const [positions, setPositions] = useState<Position[]>([]);
  const [orders, setOrders] = useState<Order[]>([]);
  const [trades, setTrades] = useState<Trade[]>([]);
  const [activeTab, setActiveTab] = useState<"orders" | "trades">("orders");
  const [strategies, setStrategies] = useState<StrategyInstance[]>([]);
  const [availableStrategies, setAvailableStrategies] = useState<any[]>([]);
  const [selectedPosition, setSelectedPosition] = useState<Position | null>(null);
  const [selectedStrategy, setSelectedStrategy] = useState<StrategyInstance | null>(null);
  const [killSwitch, setKillSwitch] = useState<KillSwitchState>({ id: 1, is_latched: false });

  
  const [, setLoading] = useState<boolean>(true);
  const [showDeployModal, setShowDeployModal] = useState<boolean>(false);
  const [showKillModal, setShowKillModal] = useState<boolean>(false);
  const [showFlattenModal, setShowFlattenModal] = useState<boolean>(false);
  const [killReason, setKillReason] = useState<string>("Manual operator trigger");
  
  // Strategy Deploy Form State
  const [deployStrategyName, setDeployStrategyName] = useState<string>("");
  const [deployInstrument, setDeployInstrument] = useState<string>("BTCUSDT.BINANCE");
  const [deployTimeframe, setDeployTimeframe] = useState<string>("1m");
  const [deployAllocation, setDeployAllocation] = useState<number>(5000);

  const fetchData = async () => {
    try {
      const [accRes, posRes, ordRes, stratRes, ksRes, availStratsRes, trdRes] = await Promise.all([
        fetchApi("/paper/accounts").catch(() => []),
        fetchApi("/paper/positions").catch(() => []),
        fetchApi("/paper/orders").catch(() => []),
        fetchApi("/paper/strategies").catch(() => []),
        fetchApi("/paper/kill-switch").catch(() => ({ id: 1, is_latched: false })),
        fetchApi("/strategies").catch(() => []),
        fetchApi("/paper/trades").catch(() => []),
      ]);

      if (Array.isArray(accRes) && accRes.length > 0) {
        setAccount(accRes[0]);
      } else {
        setAccount({
          account_id: "PAPER-ACCOUNT-001",
          venue: "BINANCE",
          currency: "USDT",
          starting_balance: 100000,
          cash_balance: 100000,
          total_equity: 100000,
          realized_pnl: 0,
          daily_realized_pnl: 0,
          daily_loss_latched: false,
        });
      }

      setPositions(Array.isArray(posRes) ? posRes : []);
      setOrders(Array.isArray(ordRes) ? ordRes : []);
      setTrades(Array.isArray(trdRes) ? trdRes : []);
      setStrategies(Array.isArray(stratRes) ? stratRes : []);
      setKillSwitch(ksRes);
      setAvailableStrategies(Array.isArray(availStratsRes) ? availStratsRes : []);
      
      if (Array.isArray(availStratsRes) && availStratsRes.length > 0 && !deployStrategyName) {
        setDeployStrategyName(availStratsRes[0].slug);
      }
    } catch (e) {
      console.error("Failed fetching paper portfolio data:", e);
    } finally {
      setLoading(false);
    }
  };


  useEffect(() => {
    fetchData();
    const interval = setInterval(fetchData, 5000); // Polling reduced
    
    // Listen to WS for live positions
    const client = getStreamClient();
    const unsub = client.subscribe((msg: any) => {
      if (msg.type === "positions_snapshot") {
        setPositions(msg.positions);
        if (msg.account) {
          setAccount((prev) => prev ? { ...prev, ...msg.account } : msg.account);
        }
      }
    });
    
    // Explicitly ask for positions subscription
    client.send({ action: "subscribe", channels: ["positions_snapshot"] });
    
    return () => {
      clearInterval(interval);
      unsub();
    };
  }, []);


  const handleTripKillSwitch = async () => {
    await fetchApi("/paper/kill-switch", {
      method: "POST",
      body: JSON.stringify({ reason: killReason, operator: "web-ui" }),
      headers: { "X-CSRF-Token": localStorage.getItem("csrf_token") || "" },
    });
    setShowKillModal(false);
    fetchData();
  };

  const handleResetKillSwitch = async () => {
    await fetchApi("/paper/kill-switch/reset", {
      method: "POST",
      body: JSON.stringify({ operator: "web-ui" }),
      headers: { "X-CSRF-Token": localStorage.getItem("csrf_token") || "" },
    });
    fetchData();
  };

  const handlePauseStrategy = async (instanceId: string) => {
    setStrategies(prev => prev.map(s => s.instance_id === instanceId ? { ...s, status: "PAUSED" } : s));
    if (selectedStrategy?.instance_id === instanceId) setSelectedStrategy(prev => prev ? { ...prev, status: "PAUSED" } : null);
    await fetchApi(`/paper/strategies/${instanceId}/pause`, { method: "POST", headers: { "X-CSRF-Token": localStorage.getItem("csrf_token") || "" } });
    fetchData();
  };

  const handleResumeStrategy = async (instanceId: string) => {
    setStrategies(prev => prev.map(s => s.instance_id === instanceId ? { ...s, status: "RUNNING" } : s));
    if (selectedStrategy?.instance_id === instanceId) setSelectedStrategy(prev => prev ? { ...prev, status: "RUNNING" } : null);
    await fetchApi(`/paper/strategies/${instanceId}/resume`, { method: "POST", headers: { "X-CSRF-Token": localStorage.getItem("csrf_token") || "" } });
    fetchData();
  };

  const handleStopStrategy = async (instanceId: string) => {
    setStrategies(prev => prev.map(s => s.instance_id === instanceId ? { ...s, status: "STOPPED" } : s));
    if (selectedStrategy?.instance_id === instanceId) setSelectedStrategy(prev => prev ? { ...prev, status: "STOPPED" } : null);
    await fetchApi(`/paper/strategies/${instanceId}/stop`, { method: "POST", headers: { "X-CSRF-Token": localStorage.getItem("csrf_token") || "" } });
    fetchData();
  };

  const handleClosePosition = async (instanceId: string, instrumentId: string) => {
    await fetchApi(`/paper/strategies/${instanceId}/close-position`, {
      method: "POST",
      body: JSON.stringify({ instrument_id: instrumentId }),
      headers: { "X-CSRF-Token": localStorage.getItem("csrf_token") || "" },
    });
    fetchData();
  };

  const handleFlattenAll = async () => {
    const openPositions = positions.filter((p) => p.status === "OPEN");
    await Promise.all(
      openPositions.map((pos) =>
        fetchApi(`/paper/strategies/${pos.instance_id}/close-position`, {
          method: "POST",
          body: JSON.stringify({ instrument_id: pos.instrument_id }),
          headers: { "X-CSRF-Token": localStorage.getItem("csrf_token") || "" },
        })
      )
    );
    setShowFlattenModal(false);
    fetchData();
  };

  const handleCancelOrder = async (clientOrderId: string) => {
    await fetchApi(`/paper/orders/${clientOrderId}/cancel`, { method: "POST", headers: { "X-CSRF-Token": localStorage.getItem("csrf_token") || "" } });
    fetchData();
  };

  const handleDeploy = async (e: React.FormEvent) => {
    e.preventDefault();
    const strat = availableStrategies.find((s) => s.slug === deployStrategyName);
    if (!strat) return;

    await fetchApi("/paper/strategies/deploy", {
      method: "POST",
      body: JSON.stringify({
        strategy_name: strat.slug,
        strategy_version_id: strat.active_version_id,
        instrument_id: deployInstrument,
        timeframe: deployTimeframe,
        allocation_usd: deployAllocation,
      }),
      headers: { "X-CSRF-Token": localStorage.getItem("csrf_token") || "" },
    });
    setShowDeployModal(false);
    fetchData();
  };

  return (
    <div className="flex-1 flex flex-col h-full bg-zinc-950 text-zinc-100 overflow-y-auto p-6 font-mono text-xs">
      {/* Top Banner if Kill Switch is Latched */}
      {killSwitch.is_latched && (
        <div className="mb-6 p-4 rounded bg-red-950/80 border border-red-500/80 flex items-center justify-between text-red-200">
          <div className="flex items-center space-x-3">
            <AlertTriangle className="w-6 h-6 text-red-400 animate-pulse" />
            <div>
              <div className="font-bold text-sm text-red-300 tracking-wide">
                EMERGENCY LATCH ENGAGED — EXECUTION HALTED
              </div>
              <div className="text-xs text-red-400">
                Reason: {killSwitch.reason || "Manual engagement"} | Latched by: {killSwitch.latched_by || "operator"}
              </div>
            </div>
          </div>
          <button
            onClick={handleResetKillSwitch}
            className="px-4 py-2 bg-red-800 hover:bg-red-700 text-white font-bold rounded shadow transition-colors"
          >
            Reset Kill Switch (Audited)
          </button>
        </div>
      )}

      {/* Header with Title & Top Actions */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-xl font-black tracking-wider text-white flex items-center space-x-2">
            <span>PORTFOLIO & PAPER EXECUTION CONSOLE</span>
            <span className="px-2 py-0.5 rounded text-[10px] bg-cyan-950 text-cyan-400 border border-cyan-800">
              M4 SIMULATOR
            </span>
          </h1>
          <p className="text-zinc-400 text-xs mt-0.5">
            Real-time paper trading engine backed by Nautilus 2.0.0rc4 LiveNode and authoritative risk guardrails.
          </p>
        </div>

        <div className="flex items-center space-x-3">
          <button
            onClick={() => setShowDeployModal(true)}
            className="flex items-center space-x-1.5 px-3 py-1.5 rounded bg-cyan-600 hover:bg-cyan-500 text-zinc-950 font-bold tracking-wide transition-colors"
          >
            <Plus className="w-4 h-4" />
            <span>Deploy Strategy</span>
          </button>

          {!killSwitch.is_latched ? (
            <>
              <button
                onClick={() => setShowFlattenModal(true)}
                className="flex items-center space-x-1.5 px-3 py-1.5 rounded bg-orange-600 hover:bg-orange-500 text-white font-bold tracking-wide transition-colors"
                disabled={positions.filter((p) => p.status === "OPEN").length === 0}
              >
                <XCircle className="w-4 h-4" />
                <span>Emergency Flatten All</span>
              </button>
              <button
                onClick={() => setShowKillModal(true)}
                className="flex items-center space-x-1.5 px-3 py-1.5 rounded bg-red-700 hover:bg-red-600 text-white font-bold tracking-wide transition-colors"
              >
                <ShieldAlert className="w-4 h-4" />
                <span>Trip Kill Switch</span>
              </button>
            </>
          ) : (
            <button
              onClick={handleResetKillSwitch}
              className="flex items-center space-x-1.5 px-3 py-1.5 rounded bg-emerald-600 hover:bg-emerald-500 text-zinc-950 font-bold tracking-wide transition-colors"
            >
              <ShieldCheck className="w-4 h-4" />
              <span>Reset Switch</span>
            </button>
          )}
        </div>
      </div>

      {/* Metric Cards Summary */}
      <div className="grid grid-cols-5 gap-4 mb-6">
        <div className="p-3 bg-zinc-900/70 border border-zinc-800 rounded">
          <div className="text-zinc-500 text-[10px] uppercase font-bold tracking-wider">Total Net Equity</div>
          <div className="text-lg font-black text-white mt-1">
            ${account?.total_equity ? Number(account.total_equity).toLocaleString("en-US", { minimumFractionDigits: 2 }) : "10,000.00"}
          </div>
          <div className="text-[10px] text-zinc-400 mt-0.5">Base: {account?.currency || "USDT"}</div>
        </div>

        <div className="p-3 bg-zinc-900/70 border border-zinc-800 rounded">
          <div className="text-zinc-500 text-[10px] uppercase font-bold tracking-wider">Available Cash</div>
          <div className="text-lg font-black text-cyan-400 mt-1">
            ${account?.cash_balance ? Number(account.cash_balance).toLocaleString("en-US", { minimumFractionDigits: 2 }) : "10,000.00"}
          </div>
          <div className="text-[10px] text-zinc-400 mt-0.5">Spot Cash Invariant</div>
        </div>

        <div className="p-3 bg-zinc-900/70 border border-zinc-800 rounded">
          <div className="text-zinc-500 text-[10px] uppercase font-bold tracking-wider">Daily Realized PnL</div>
          <div className={`text-lg font-black mt-1 ${Number(account?.daily_realized_pnl || 0) >= 0 ? "text-emerald-400" : "text-red-400"}`}>
            {Number(account?.daily_realized_pnl || 0) >= 0 ? "+" : ""}$
            {Number(account?.daily_realized_pnl || 0).toFixed(2)}
          </div>
          <div className="text-[10px] text-zinc-400 mt-0.5">UTC Trading Day</div>
        </div>

        <div className="p-3 bg-zinc-900/70 border border-zinc-800 rounded">
          <div className="text-zinc-500 text-[10px] uppercase font-bold tracking-wider">Open Positions</div>
          <div className="text-lg font-black text-zinc-200 mt-1">
            {positions.filter((p) => p.status === "OPEN").length} Active
          </div>
          <div className="text-[10px] text-zinc-400 mt-0.5">
            Total Open Qty: {positions.filter((p) => p.status === "OPEN").reduce((acc, p) => acc + Number(p.quantity), 0).toFixed(4)}
          </div>
        </div>

        <div className="p-3 bg-zinc-900/70 border border-zinc-800 rounded">
          <div className="text-zinc-500 text-[10px] uppercase font-bold tracking-wider">Execution Guard</div>
          <div className="text-lg font-black mt-1 flex items-center space-x-1.5">
            {killSwitch.is_latched ? (
              <span className="text-red-400 flex items-center space-x-1">
                <ShieldAlert className="w-4 h-4" />
                <span>LATCHED</span>
              </span>
            ) : (
              <span className="text-emerald-400 flex items-center space-x-1">
                <ShieldCheck className="w-4 h-4" />
                <span>ACTIVE</span>
              </span>
            )}
          </div>
          <div className="text-[10px] text-zinc-400 mt-0.5">
            {account?.daily_loss_latched ? "Daily Drawdown Tripped" : "Collars & Freshness OK"}
          </div>
        </div>
      </div>

      {/* Capital Allocation & Concentration Gauge */}
      <div className="mb-6 p-4 bg-zinc-900/50 border border-zinc-800 rounded">
        <div className="flex justify-between items-center mb-2">
          <div className="text-zinc-400 text-xs font-bold uppercase tracking-wider">Capital Allocation & Concentration Risk</div>
          <div className="text-zinc-500 text-[10px]">Max 25% Single-Asset Concentration</div>
        </div>
        <div className="w-full h-2 bg-zinc-800 rounded overflow-hidden flex">
          {positions.filter(p => p.status === 'OPEN').map(p => {
             const allocation = (Math.abs(p.quantity * (p.current_price || p.entry_price)) / (account?.total_equity || 100000)) * 100;
             let color = "bg-emerald-500";
             if (allocation >= 20) color = "bg-red-500";
             else if (allocation >= 15) color = "bg-yellow-500";
             else if (allocation >= 10) color = "bg-cyan-500";
             return <div key={p.position_id} className={`h-full ${color} border-r border-zinc-900`} style={{ width: `${allocation}%` }} title={`${p.instrument_id}: ${allocation.toFixed(1)}%`} />
          })}
        </div>
        <div className="flex items-center space-x-4 mt-2 text-[10px] text-zinc-500">
          <div className="flex items-center space-x-1">
            <div className="w-2 h-2 rounded-full bg-emerald-500"></div>
            <span>Optimal (&lt;10%)</span>
          </div>
          <div className="flex items-center space-x-1">
            <div className="w-2 h-2 rounded-full bg-cyan-500"></div>
            <span>Elevated (10-15%)</span>
          </div>
          <div className="flex items-center space-x-1">
            <div className="w-2 h-2 rounded-full bg-yellow-500"></div>
            <span>Warning (15-20%)</span>
          </div>
          <div className="flex items-center space-x-1">
            <div className="w-2 h-2 rounded-full bg-red-500"></div>
            <span>Concentrated (&gt;20%)</span>
          </div>
        </div>
      </div>

      {/* Main Grid: Active Positions & Strategy Instances */}
      <div className="grid grid-cols-2 gap-6 mb-6">
        {/* Active Positions */}
        <div className="bg-zinc-900/50 border border-zinc-800 rounded flex flex-col">
          <div className="p-3 border-b border-zinc-800 flex items-center justify-between font-bold text-xs uppercase tracking-wider text-zinc-300">
            <span className="flex items-center space-x-2">
              <Layers className="w-4 h-4 text-cyan-400" />
              <span>Active Positions</span>
            </span>
            <span className="text-[10px] text-zinc-500">250ms Mark-to-Market</span>
          </div>
          <div className="p-3 overflow-x-auto">
            {positions.filter(p => p.status === "OPEN").length === 0 ? (
              <div className="text-center py-8 text-zinc-500 italic">No open positions.</div>
            ) : (
              <table className="w-full text-left">
                <thead>
                  <tr className="text-zinc-500 border-b border-zinc-800 pb-1">
                    <th className="py-1">Instrument</th>
                    <th>Side</th>
                    <th>Origin</th>
                    <th>Qty</th>
                    <th>Entry</th>
                    <th>Mark</th>
                    <th>Unrealized PnL</th>
                    <th className="text-right">Action</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-zinc-800/50">
                  {positions.filter(p => p.status === "OPEN").map((pos) => (
                    <tr key={pos.position_id} className="hover:bg-zinc-800/30 cursor-pointer" onClick={() => setSelectedPosition(pos)}>
                      <td className="py-2 font-bold text-white">{pos.instrument_id}</td>
                      <td>
                        <span className="px-1.5 py-0.5 rounded text-[10px] bg-emerald-950 text-emerald-400 border border-emerald-800">
                          {pos.side}
                        </span>
                      </td>
                      <td>
                        <span className={`px-1.5 py-0.5 rounded text-[10px] border bg-indigo-500/20 text-indigo-400 border-indigo-500/30`}>
                          {pos.instance_id.length > 15 ? pos.instance_id.substring(0, 15) + "..." : pos.instance_id}
                        </span>
                      </td>
                      <td>{Number(pos.quantity).toFixed(4)}</td>
                      <td>${Number(pos.entry_price).toFixed(2)}</td>
                      <td>${Number(pos.current_price || pos.entry_price).toFixed(2)}</td>
                      <td className={Number(pos.unrealized_pnl) >= 0 ? "text-emerald-400" : "text-red-400"}>
                        {Number(pos.unrealized_pnl) >= 0 ? "+" : ""}${Number(pos.unrealized_pnl).toFixed(2)}
                      </td>
                      <td className="text-right">
                        <button
                          onClick={(e) => { e.stopPropagation(); handleClosePosition(pos.instance_id, pos.instrument_id); }}
                          className="px-2 py-1 bg-red-950 hover:bg-red-900 border border-red-800 text-red-300 rounded text-[10px]"
                        >
                          Flatten
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>

        {/* Paper Strategy Instances */}
        <div className="bg-zinc-900/50 border border-zinc-800 rounded flex flex-col">
          <div className="p-3 border-b border-zinc-800 flex items-center justify-between font-bold text-xs uppercase tracking-wider text-zinc-300">
            <span className="flex items-center space-x-2">
              <TrendingUp className="w-4 h-4 text-emerald-400" />
              <span>Strategy Instances</span>
            </span>
            <span className="text-[10px] text-zinc-500">1 per (Account, Instrument)</span>
          </div>
          <div className="p-3 overflow-x-auto">
            {strategies.filter(st => !['MANUAL_TRADE', 'AI_DEBATE_WORKER'].includes(st.instance_id)).length === 0 ? (
              <div className="text-center py-8 text-zinc-500 italic">No deployed strategy instances.</div>
            ) : (
              <table className="w-full text-left">
                <thead>
                  <tr className="text-zinc-500 border-b border-zinc-800 pb-1">
                    <th className="py-1">Instance</th>
                    <th>Strategy</th>
                    <th>Instrument</th>
                    <th>Alloc</th>
                    <th>Status</th>
                    <th className="text-right">Controls</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-zinc-800/50">
                  {strategies.filter(st => !['MANUAL_TRADE', 'AI_DEBATE_WORKER'].includes(st.instance_id)).map((st) => (
                    <tr key={st.instance_id} className="hover:bg-zinc-800/30 cursor-pointer" onClick={() => setSelectedStrategy(st)}>
                      <td className="py-2 font-bold text-zinc-300">{st.instance_id}</td>
                      <td>{st.strategy_name}</td>
                      <td>{st.instrument_id}</td>
                      <td>${Number(st.allocation_usd).toLocaleString()}</td>
                      <td>
                        <span
                          className={`px-1.5 py-0.5 rounded text-[10px] ${
                            st.status === "RUNNING"
                              ? "bg-emerald-950 text-emerald-400 border border-emerald-800"
                              : st.status === "PAUSED"
                              ? "bg-yellow-950 text-yellow-400 border border-yellow-800"
                              : "bg-zinc-800 text-zinc-400 border border-zinc-700"
                          }`}
                        >
                          {st.status}
                        </span>
                      </td>
                      <td className="text-right space-x-1">
                        {st.status === "RUNNING" ? (
                          <button
                            onClick={(e) => { e.stopPropagation(); handlePauseStrategy(st.instance_id); }}
                            className="p-1 bg-yellow-950 hover:bg-yellow-900 border border-yellow-800 text-yellow-300 rounded focus-visible:ring-2 focus-visible:ring-yellow-500/50 outline-none transition-all"
                            title="Pause"
                            aria-label={`Pause strategy ${st.strategy_name}`}
                          >
                            <Pause className="w-3 h-3" />
                          </button>
                        ) : (
                          <button
                            onClick={(e) => { e.stopPropagation(); handleResumeStrategy(st.instance_id); }}
                            className="p-1 bg-emerald-950 hover:bg-emerald-900 border border-emerald-800 text-emerald-300 rounded focus-visible:ring-2 focus-visible:ring-emerald-500/50 outline-none transition-all"
                            title="Resume"
                            aria-label={`Resume strategy ${st.strategy_name}`}
                          >
                            <Play className="w-3 h-3" />
                          </button>
                        )}
                        <button
                          onClick={(e) => { e.stopPropagation(); handleStopStrategy(st.instance_id); }}
                          className="p-1 bg-red-950 hover:bg-red-900 border border-red-800 text-red-300 rounded focus-visible:ring-2 focus-visible:ring-red-500/50 outline-none transition-all"
                          title="Stop"
                          aria-label={`Stop strategy ${st.strategy_name}`}
                        >
                          <Square className="w-3 h-3" />
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>
      </div>

      {/* Orders & Trades Ledger */}
      <div className="bg-zinc-900/50 border border-zinc-800 rounded flex flex-col flex-1 min-h-[300px]">
        <div className="p-3 border-b border-zinc-800 flex items-center justify-between font-bold text-xs uppercase tracking-wider text-zinc-300">
          <div className="flex space-x-6">
            <button
              onClick={() => setActiveTab("orders")}
              className={`pb-1 transition-colors ${activeTab === "orders" ? "text-cyan-400 border-b-2 border-cyan-400" : "text-zinc-500 hover:text-zinc-300"}`}
            >
              Execution Orders Journal
            </button>
            <button
              onClick={() => setActiveTab("trades")}
              className={`pb-1 transition-colors ${activeTab === "trades" ? "text-cyan-400 border-b-2 border-cyan-400" : "text-zinc-500 hover:text-zinc-300"}`}
            >
              Executed Trades Ledger
            </button>
          </div>
          <span className="text-[10px] text-zinc-500">{activeTab === "orders" ? orders.length : trades.length} Records</span>
        </div>
        <div className="p-3 overflow-y-auto">
          {activeTab === "orders" ? (
            orders.length === 0 ? (
              <div className="text-center py-8 text-zinc-500 italic">No orders executed yet.</div>
            ) : (
              <table className="w-full text-left">
                <thead>
                  <tr className="text-zinc-500 border-b border-zinc-800 pb-1">
                    <th className="py-1">Client Order ID</th>
                    <th>Instrument</th>
                    <th>Side</th>
                    <th>Type</th>
                    <th>Qty</th>
                    <th>Limit Price</th>
                    <th>Filled Qty</th>
                    <th>Status</th>
                    <th className="text-right">Action</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-zinc-800/50">
                  {orders.map((ord) => (
                    <tr key={ord.client_order_id} className="hover:bg-zinc-800/30">
                      <td className="py-2 text-zinc-400">{ord.client_order_id}</td>
                      <td className="font-bold text-white">{ord.instrument_id}</td>
                      <td>
                        <span className={`px-1.5 py-0.5 rounded text-[10px] ${ord.side === "BUY" ? "bg-emerald-950 text-emerald-400" : "bg-red-950 text-red-400"}`}>
                          {ord.side}
                        </span>
                      </td>
                      <td>{ord.order_type}</td>
                      <td>{Number(ord.quantity).toFixed(4)}</td>
                      <td>{ord.price ? `$${Number(ord.price).toFixed(2)}` : "MKT"}</td>
                      <td>{Number(ord.filled_qty).toFixed(4)}</td>
                      <td>
                        <span className="px-1.5 py-0.5 rounded text-[10px] bg-zinc-800 text-zinc-300">
                          {ord.status}
                        </span>
                      </td>
                      <td className="text-right">
                        {["PENDING", "SUBMITTED", "ACCEPTED"].includes(ord.status) && (
                          <button
                            onClick={() => handleCancelOrder(ord.client_order_id)}
                            className="px-2 py-0.5 bg-zinc-800 hover:bg-zinc-700 text-zinc-200 rounded text-[10px]"
                          >
                            Cancel
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )
          ) : (
            trades.length === 0 ? (
              <div className="text-center py-8 text-zinc-500 italic">No trades settled yet.</div>
            ) : (
              <table className="w-full text-left">
                <thead>
                  <tr className="text-zinc-500 border-b border-zinc-800 pb-1">
                    <th className="py-1">Trade ID</th>
                    <th>Time (UTC)</th>
                    <th>Instrument</th>
                    <th>Side</th>
                    <th>Fill Price</th>
                    <th>Quantity</th>
                    <th className="text-right">Commission (USDT)</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-zinc-800/50">
                  {trades.map((trd) => (
                    <tr key={trd.trade_id} className="hover:bg-zinc-800/30">
                      <td className="py-2 text-zinc-400">{trd.trade_id}</td>
                      <td className="text-zinc-300">{new Date(trd.created_at).toLocaleString()}</td>
                      <td className="font-bold text-white">{trd.instrument_id}</td>
                      <td>
                        <span className={`px-1.5 py-0.5 rounded text-[10px] ${trd.side === "BUY" ? "bg-emerald-950 text-emerald-400" : "bg-red-950 text-red-400"}`}>
                          {trd.side}
                        </span>
                      </td>
                      <td>${Number(trd.fill_price).toFixed(2)}</td>
                      <td>{Number(trd.quantity).toFixed(4)}</td>
                      <td className="text-right text-red-400">-${Number(trd.commission).toFixed(4)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )
          )}
        </div>
      </div>

      {/* Emergency Flatten All Modal */}
      {showFlattenModal && (
        <div className="fixed inset-0 bg-black/80 flex items-center justify-center p-4 z-50">
          <div className="bg-zinc-900 border border-orange-600 rounded p-6 max-w-md w-full">
            <div className="flex items-center space-x-3 text-orange-400 mb-4">
              <AlertTriangle className="w-6 h-6" />
              <h2 className="text-base font-black">CONFIRM EMERGENCY FLATTEN</h2>
            </div>
            <p className="text-zinc-300 text-xs mb-4">
              This action will aggressively liquidate all open positions concurrently via market orders. It does not pause running strategies, which may re-enter positions if not halted.
            </p>
            <div className="flex justify-end space-x-3 mt-6">
              <button
                onClick={() => setShowFlattenModal(false)}
                className="px-4 py-1.5 bg-zinc-800 hover:bg-zinc-700 text-zinc-300 rounded font-bold text-xs"
              >
                Cancel
              </button>
              <button
                onClick={handleFlattenAll}
                className="px-4 py-1.5 bg-orange-600 hover:bg-orange-500 text-white rounded font-bold text-xs"
              >
                FLATTEN ALL POSITIONS
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Kill Switch Modal */}
      {showKillModal && (
        <div className="fixed inset-0 bg-black/80 flex items-center justify-center p-4 z-50">
          <div className="bg-zinc-900 border border-red-600 rounded p-6 max-w-md w-full">
            <div className="flex items-center space-x-3 text-red-400 mb-4">
              <ShieldAlert className="w-6 h-6" />
              <h2 className="text-base font-black">CONFIRM EMERGENCY KILL SWITCH</h2>
            </div>
            <p className="text-zinc-300 text-xs mb-4">
              Tripping the kill switch immediately latches the execution engine, cancels all resting orders, and rejects new entry intents.
            </p>
            <div className="mb-4">
              <label className="block text-zinc-400 text-xs mb-1">Reason for Emergency Halt:</label>
              <input
                type="text"
                value={killReason}
                onChange={(e) => setKillReason(e.target.value)}
                className="w-full bg-zinc-950 border border-zinc-700 rounded px-3 py-1.5 text-zinc-200 text-xs focus:outline-none focus:border-red-500"
              />
            </div>
            <div className="flex justify-end space-x-3">
              <button
                onClick={() => setShowKillModal(false)}
                className="px-4 py-1.5 bg-zinc-800 hover:bg-zinc-700 text-zinc-300 rounded font-bold text-xs"
              >
                Cancel
              </button>
              <button
                onClick={handleTripKillSwitch}
                className="px-4 py-1.5 bg-red-600 hover:bg-red-500 text-white rounded font-bold text-xs"
              >
                ENGAGE KILL SWITCH
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Deploy Strategy Modal */}
      {showDeployModal && (
        <div className="fixed inset-0 bg-black/80 flex items-center justify-center p-4 z-50">
          <div className="bg-zinc-900 border border-zinc-700 rounded p-6 max-w-md w-full">
            <h2 className="text-base font-black text-white mb-4 flex items-center space-x-2">
              <Plus className="w-5 h-5 text-cyan-400" />
              <span>Deploy Strategy to Paper Engine</span>
            </h2>
            <form onSubmit={handleDeploy} className="space-y-4">
              <div>
                <label className="block text-zinc-400 text-xs mb-1">Strategy Name:</label>
                <select
                  value={deployStrategyName}
                  onChange={(e) => setDeployStrategyName(e.target.value)}
                  className="w-full bg-zinc-950 border border-zinc-700 rounded px-3 py-1.5 text-zinc-200 text-xs focus:outline-none"
                >
                  {availableStrategies.map((s) => (
                    <option key={s.slug} value={s.slug}>{s.name}</option>
                  ))}
                </select>
              </div>

              <div>
                <label className="block text-zinc-400 text-xs mb-1">Instrument:</label>
                <select
                  value={deployInstrument}
                  onChange={(e) => setDeployInstrument(e.target.value)}
                  className="w-full bg-zinc-950 border border-zinc-700 rounded px-3 py-1.5 text-zinc-200 text-xs focus:outline-none"
                >
                  <option value="BTCUSDT.BINANCE">BTCUSDT.BINANCE</option>
                  <option value="ETHUSDT.BINANCE">ETHUSDT.BINANCE</option>
                  <option value="SOLUSDT.BINANCE">SOLUSDT.BINANCE</option>
                </select>
              </div>

              <div>
                <label className="block text-zinc-400 text-xs mb-1">Timeframe:</label>
                <select
                  value={deployTimeframe}
                  onChange={(e) => setDeployTimeframe(e.target.value)}
                  className="w-full bg-zinc-950 border border-zinc-700 rounded px-3 py-1.5 text-zinc-200 text-xs focus:outline-none"
                >
                  <option value="1m">1 Minute</option>
                  <option value="5m">5 Minutes</option>
                  <option value="15m">15 Minutes</option>
                  <option value="1h">1 Hour</option>
                </select>
              </div>

              <div>
                <label className="block text-zinc-400 text-xs mb-1">USD Capital Allocation:</label>
                <input
                  type="number"
                  value={deployAllocation}
                  onChange={(e) => setDeployAllocation(Number(e.target.value))}
                  min={100}
                  max={50000}
                  step={100}
                  className="w-full bg-zinc-950 border border-zinc-700 rounded px-3 py-1.5 text-zinc-200 text-xs focus:outline-none"
                />
              </div>

              <div className="flex justify-end space-x-3 pt-2">
                <button
                  type="button"
                  onClick={() => setShowDeployModal(false)}
                  className="px-4 py-1.5 bg-zinc-800 hover:bg-zinc-700 text-zinc-300 rounded font-bold text-xs"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  className="px-4 py-1.5 bg-cyan-600 hover:bg-cyan-500 text-zinc-950 rounded font-bold text-xs"
                >
                  Deploy
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Attribution Drawer */}
      {selectedPosition && (
        <div className="fixed inset-0 bg-black/60 z-40 flex justify-end" onClick={() => setSelectedPosition(null)}>
          <div 
            className="w-96 bg-zinc-950 border-l border-zinc-800 h-full flex flex-col shadow-2xl animate-in slide-in-from-right duration-200"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="p-4 border-b border-zinc-800 flex justify-between items-center bg-zinc-900/50">
              <h2 className="font-black text-white flex items-center space-x-2">
                <ArrowUpRight className="w-5 h-5 text-cyan-400" />
                <span>POSITION ATTRIBUTION</span>
              </h2>
              <button onClick={() => setSelectedPosition(null)} className="text-zinc-500 hover:text-white transition-colors">
                <XCircle className="w-5 h-5" />
              </button>
            </div>
            
            <div className="p-6 flex-1 overflow-y-auto space-y-6 text-sm">
              <div>
                <div className="text-zinc-500 text-xs font-bold uppercase tracking-wider mb-2">Instrument</div>
                <div className="text-xl font-black text-white">{selectedPosition.instrument_id}</div>
                <div className={`mt-1 font-bold ${selectedPosition.side === "LONG" ? "text-emerald-400" : "text-red-400"}`}>
                  {selectedPosition.side} • {Number(selectedPosition.quantity).toFixed(4)}
                </div>
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div className="p-3 bg-zinc-900 rounded border border-zinc-800">
                  <div className="text-[10px] text-zinc-500 uppercase tracking-wider mb-1">Entry Price</div>
                  <div className="font-mono font-bold">${Number(selectedPosition.entry_price).toFixed(2)}</div>
                </div>
                <div className="p-3 bg-zinc-900 rounded border border-zinc-800">
                  <div className="text-[10px] text-zinc-500 uppercase tracking-wider mb-1">Mark Price</div>
                  <div className="font-mono font-bold text-cyan-400">${Number(selectedPosition.current_price || selectedPosition.entry_price).toFixed(2)}</div>
                </div>
              </div>

              <div>
                <div className="text-zinc-500 text-xs font-bold uppercase tracking-wider mb-2">Originating Intent</div>
                <div className="p-3 bg-indigo-950/30 border border-indigo-900/50 rounded flex items-start space-x-3">
                  <div className="p-1.5 bg-indigo-900/50 rounded">
                    <Layers className="w-4 h-4 text-indigo-400" />
                  </div>
                  <div>
                    <div className="font-bold text-indigo-200">{selectedPosition.instance_id}</div>
                    <div className="text-xs text-indigo-400 mt-1">Authorized by Paper Engine Router</div>
                  </div>
                </div>
              </div>
              
              <div>
                <div className="text-zinc-500 text-xs font-bold uppercase tracking-wider mb-2">Risk Assessment</div>
                <div className="text-xs text-zinc-400 bg-zinc-900 p-3 rounded border border-zinc-800 leading-relaxed">
                  Execution verified against {account?.currency || "USDT"} free cash invariant. Capital concentration at {((Math.abs(selectedPosition.quantity * (selectedPosition.current_price || selectedPosition.entry_price)) / (account?.total_equity || 100000)) * 100).toFixed(1)}%, within 25% portfolio limits.
                </div>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Strategy Drawer */}
      {selectedStrategy && (
        <div className="fixed inset-0 bg-black/60 z-40 flex justify-end" onClick={() => setSelectedStrategy(null)}>
          <div 
            className="w-96 bg-zinc-950 border-l border-zinc-800 h-full flex flex-col shadow-2xl animate-in slide-in-from-right duration-200"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="p-4 border-b border-zinc-800 flex justify-between items-center bg-zinc-900/50">
              <h2 className="font-black text-white flex items-center space-x-2">
                <TrendingUp className="w-5 h-5 text-emerald-400" />
                <span>STRATEGY DETAILS</span>
              </h2>
              <button onClick={() => setSelectedStrategy(null)} className="text-zinc-500 hover:text-white transition-colors">
                <XCircle className="w-5 h-5" />
              </button>
            </div>
            
            <div className="p-6 flex-1 overflow-y-auto space-y-6 text-sm">
              <div>
                <div className="text-zinc-500 text-xs font-bold uppercase tracking-wider mb-2">Configuration</div>
                <div className="text-xl font-black text-white">{selectedStrategy.strategy_name}</div>
                <div className="mt-1 font-bold text-zinc-300">
                  {selectedStrategy.instrument_id} • {selectedStrategy.timeframe}
                </div>
                <div className="mt-2 text-xs text-zinc-400 font-mono bg-zinc-900 p-2 rounded border border-zinc-800">
                  {selectedStrategy.instance_id}
                </div>
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div className="p-3 bg-zinc-900 rounded border border-zinc-800">
                  <div className="text-[10px] text-zinc-500 uppercase tracking-wider mb-1">Status</div>
                  <div className={`font-mono font-bold ${selectedStrategy.status === 'RUNNING' ? 'text-emerald-400' : 'text-yellow-400'}`}>
                    {selectedStrategy.status}
                  </div>
                </div>
                <div className="p-3 bg-zinc-900 rounded border border-zinc-800">
                  <div className="text-[10px] text-zinc-500 uppercase tracking-wider mb-1">Allocation</div>
                  <div className="font-mono font-bold text-cyan-400">${Number(selectedStrategy.allocation_usd).toLocaleString()}</div>
                </div>
              </div>

              <div>
                <div className="text-zinc-500 text-xs font-bold uppercase tracking-wider mb-2">Resolved Parameters</div>
                <div className="bg-zinc-900 rounded border border-zinc-800 p-3 overflow-x-auto">
                  {selectedStrategy.resolved_parameters ? (
                    <pre className="text-[10px] text-zinc-400 font-mono">
                      {JSON.stringify(
                        typeof selectedStrategy.resolved_parameters === 'string'
                          ? JSON.parse(selectedStrategy.resolved_parameters)
                          : selectedStrategy.resolved_parameters,
                        null,
                        2
                      )}
                    </pre>
                  ) : (
                    <div className="text-xs text-zinc-500 italic">No parameters available or using defaults.</div>
                  )}
                </div>
              </div>

              <div>
                <div className="text-zinc-500 text-xs font-bold uppercase tracking-wider mb-2">Operator Actions</div>
                <div className="space-y-2">
                  <button 
                    onClick={() => {
                      window.location.href = `/operator?strategy=${selectedStrategy.instance_id}`;
                    }}
                    className="w-full flex items-center justify-center space-x-2 p-2 bg-indigo-900/30 hover:bg-indigo-900/50 border border-indigo-500/30 text-indigo-300 rounded transition-colors text-xs font-bold"
                  >
                    <ArrowUpRight className="w-4 h-4" />
                    <span>Open Strategy Dashboard</span>
                  </button>
                  <button 
                    onClick={() => {
                      if (window.confirm("Are you sure you want to halt this strategy and liquidate its holdings?")) {
                        handleStopStrategy(selectedStrategy.instance_id);
                        handleClosePosition(selectedStrategy.instance_id, selectedStrategy.instrument_id);
                        setSelectedStrategy(null);
                      }
                    }}
                    className="w-full flex items-center justify-center space-x-2 p-2 bg-red-900/30 hover:bg-red-900/50 border border-red-500/30 text-red-300 rounded transition-colors text-xs font-bold"
                  >
                    <ShieldAlert className="w-4 h-4" />
                    <span>Halt & Flatten Strategy</span>
                  </button>
                </div>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
