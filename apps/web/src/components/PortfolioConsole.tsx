"use client";

import React, { useState, useEffect } from "react";
import {
  ShieldAlert,
  ShieldCheck,
  Play,
  Pause,
  Square,
  AlertTriangle,
  XCircle,
  Plus,
  RefreshCw,
  DollarSign,
  TrendingUp,
  Layers,
  ArrowUpRight,
  ArrowDownRight,
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
}

interface KillSwitchState {
  id: number;
  is_latched: boolean;
  latched_by?: string;
  reason?: string;
  latched_at?: string;
}

export function PortfolioConsole() {
  const [account, setAccount] = useState<Account | null>(null);
  const [positions, setPositions] = useState<Position[]>([]);
  const [orders, setOrders] = useState<Order[]>([]);
  const [strategies, setStrategies] = useState<StrategyInstance[]>([]);
  const [killSwitch, setKillSwitch] = useState<KillSwitchState>({ id: 1, is_latched: false });
  
  const [loading, setLoading] = useState<boolean>(true);
  const [showDeployModal, setShowDeployModal] = useState<boolean>(false);
  const [showKillModal, setShowKillModal] = useState<boolean>(false);
  const [killReason, setKillReason] = useState<string>("Manual operator trigger");
  
  // Strategy Deploy Form State
  const [deployStrategyName, setDeployStrategyName] = useState<string>("EMACrossover");
  const [deployInstrument, setDeployInstrument] = useState<string>("BTCUSDT.BINANCE");
  const [deployTimeframe, setDeployTimeframe] = useState<string>("1m");
  const [deployAllocation, setDeployAllocation] = useState<number>(5000);

  const fetchData = async () => {
    try {
      const [accRes, posRes, ordRes, stratRes, ksRes] = await Promise.all([
        fetch("/api/v1/paper/accounts").then((r) => r.json()).catch(() => []),
        fetch("/api/v1/paper/positions").then((r) => r.json()).catch(() => []),
        fetch("/api/v1/paper/orders").then((r) => r.json()).catch(() => []),
        fetch("/api/v1/paper/strategies").then((r) => r.json()).catch(() => []),
        fetch("/api/v1/paper/kill-switch").then((r) => r.json()).catch(() => ({ id: 1, is_latched: false })),
      ]);

      if (Array.isArray(accRes) && accRes.length > 0) {
        setAccount(accRes[0]);
      } else {
        setAccount({
          account_id: "PAPER-ACCOUNT-001",
          venue: "BINANCE",
          currency: "USDT",
          starting_balance: 10000,
          cash_balance: 10000,
          total_equity: 10000,
          realized_pnl: 0,
          daily_realized_pnl: 0,
          daily_loss_latched: false,
        });
      }

      setPositions(Array.isArray(posRes) ? posRes : []);
      setOrders(Array.isArray(ordRes) ? ordRes : []);
      setStrategies(Array.isArray(stratRes) ? stratRes : []);
      setKillSwitch(ksRes);
    } catch (e) {
      console.error("Failed fetching paper portfolio data:", e);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchData();
    const interval = setInterval(fetchData, 2000);
    return () => clearInterval(interval);
  }, []);

  const handleTripKillSwitch = async () => {
    await fetch("/api/v1/paper/kill-switch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason: killReason, operator: "web-ui" }),
    });
    setShowKillModal(false);
    fetchData();
  };

  const handleResetKillSwitch = async () => {
    await fetch("/api/v1/paper/kill-switch/reset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ operator: "web-ui" }),
    });
    fetchData();
  };

  const handlePauseStrategy = async (instanceId: string) => {
    await fetch(`/api/v1/paper/strategies/${instanceId}/pause`, { method: "POST" });
    fetchData();
  };

  const handleResumeStrategy = async (instanceId: string) => {
    await fetch(`/api/v1/paper/strategies/${instanceId}/resume`, { method: "POST" });
    fetchData();
  };

  const handleStopStrategy = async (instanceId: string) => {
    await fetch(`/api/v1/paper/strategies/${instanceId}/stop`, { method: "POST" });
    fetchData();
  };

  const handleClosePosition = async (instanceId: string, instrumentId: string) => {
    await fetch(`/api/v1/paper/strategies/${instanceId}/close-position`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ instrument_id: instrumentId }),
    });
    fetchData();
  };

  const handleCancelOrder = async (clientOrderId: string) => {
    await fetch(`/api/v1/paper/orders/${clientOrderId}/cancel`, { method: "POST" });
    fetchData();
  };

  const handleDeploy = async (e: React.FormEvent) => {
    e.preventDefault();
    await fetch("/api/v1/paper/strategies/deploy", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        strategy_name: deployStrategyName,
        strategy_version_id: "00000000-0000-0000-0000-000000000001",
        instrument_id: deployInstrument,
        timeframe: deployTimeframe,
        allocation_usd: deployAllocation,
      }),
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
            <button
              onClick={() => setShowKillModal(true)}
              className="flex items-center space-x-1.5 px-3 py-1.5 rounded bg-red-700 hover:bg-red-600 text-white font-bold tracking-wide transition-colors"
            >
              <ShieldAlert className="w-4 h-4" />
              <span>Trip Kill Switch</span>
            </button>
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
            {positions.length === 0 ? (
              <div className="text-center py-8 text-zinc-500 italic">No open positions.</div>
            ) : (
              <table className="w-full text-left">
                <thead>
                  <tr className="text-zinc-500 border-b border-zinc-800 pb-1">
                    <th className="py-1">Instrument</th>
                    <th>Side</th>
                    <th>Qty</th>
                    <th>Entry</th>
                    <th>Mark</th>
                    <th>Unrealized PnL</th>
                    <th className="text-right">Action</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-zinc-800/50">
                  {positions.map((pos) => (
                    <tr key={pos.position_id} className="hover:bg-zinc-800/30">
                      <td className="py-2 font-bold text-white">{pos.instrument_id}</td>
                      <td>
                        <span className="px-1.5 py-0.5 rounded text-[10px] bg-emerald-950 text-emerald-400 border border-emerald-800">
                          {pos.side}
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
                          onClick={() => handleClosePosition(pos.instance_id, pos.instrument_id)}
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
            {strategies.length === 0 ? (
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
                  {strategies.map((st) => (
                    <tr key={st.instance_id} className="hover:bg-zinc-800/30">
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
                            onClick={() => handlePauseStrategy(st.instance_id)}
                            className="p-1 bg-yellow-950 hover:bg-yellow-900 border border-yellow-800 text-yellow-300 rounded"
                            title="Pause"
                          >
                            <Pause className="w-3 h-3" />
                          </button>
                        ) : (
                          <button
                            onClick={() => handleResumeStrategy(st.instance_id)}
                            className="p-1 bg-emerald-950 hover:bg-emerald-900 border border-emerald-800 text-emerald-300 rounded"
                            title="Resume"
                          >
                            <Play className="w-3 h-3" />
                          </button>
                        )}
                        <button
                          onClick={() => handleStopStrategy(st.instance_id)}
                          className="p-1 bg-red-950 hover:bg-red-900 border border-red-800 text-red-300 rounded"
                          title="Stop"
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

      {/* Orders Table */}
      <div className="bg-zinc-900/50 border border-zinc-800 rounded flex flex-col flex-1">
        <div className="p-3 border-b border-zinc-800 flex items-center justify-between font-bold text-xs uppercase tracking-wider text-zinc-300">
          <span>Execution Orders Journal</span>
          <span className="text-[10px] text-zinc-500">{orders.length} Logged Orders</span>
        </div>
        <div className="p-3 overflow-y-auto">
          {orders.length === 0 ? (
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
          )}
        </div>
      </div>

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
                  <option value="EMACrossover">EMA Crossover</option>
                  <option value="RSIMomentum">RSI Momentum</option>
                  <option value="BollingerBreakout">Bollinger Breakout</option>
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
    </div>
  );
}
