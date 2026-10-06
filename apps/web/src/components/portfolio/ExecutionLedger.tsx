"use client";

import React, { useEffect, useMemo, useState } from "react";
import { ArrowDown, ArrowUp, ChevronLeft, ChevronRight, Download, FilterX } from "lucide-react";
import {
  usePortfolioStore,
  type LedgerRange,
  type PaperOrder,
  type PaperTrade,
} from "@/lib/stores/usePortfolioStore";
import { postCommand } from "@/lib/portfolio/actions";
import { downloadCsv, fmtLedgerQty, fmtPrice, toCsv } from "@/lib/portfolio/metrics";

const RANGE_MS: Record<LedgerRange, number> = { "24h": 86_400_000, "7d": 7 * 86_400_000, "30d": 30 * 86_400_000, all: Infinity };
const CANCELLABLE = new Set(["PENDING", "SUBMITTED", "ACCEPTED"]);

interface Column<T> {
  key: string;
  header: string;
  align?: "right";
  sort?: (r: T) => number | string;
  csv: (r: T) => unknown;
  cell: (r: T) => React.ReactNode;
}

const sideTag = (side: string) => (
  <span className={`px-1.5 py-0.5 rounded text-2xs font-bold ${side === "BUY" ? "bg-emerald-950 text-emerald-400" : "bg-red-950 text-red-400"}`}>{side}</span>
);
const ts = (iso: string) => {
  const t = Date.parse(iso);
  return Number.isFinite(t) ? new Date(t).toISOString().replace("T", " ").slice(0, 19) : "—";
};

const ORDER_COLUMNS: Column<PaperOrder>[] = [
  { key: "time", header: "Time (UTC)", sort: (o) => Date.parse(o.created_at) || 0, csv: (o) => o.created_at, cell: (o) => <span className="text-zinc-400">{ts(o.created_at)}</span> },
  { key: "id", header: "Client Order ID", sort: (o) => o.client_order_id, csv: (o) => o.client_order_id, cell: (o) => <span className="text-zinc-400">{o.client_order_id}</span> },
  { key: "instrument", header: "Instrument", sort: (o) => o.instrument_id, csv: (o) => o.instrument_id, cell: (o) => <span className="font-bold text-white">{o.instrument_id}</span> },
  { key: "origin", header: "Origin", sort: (o) => o.instance_id, csv: (o) => o.instance_id, cell: (o) => <span className="text-indigo-300">{o.instance_id}</span> },
  { key: "side", header: "Side", sort: (o) => o.side, csv: (o) => o.side, cell: (o) => sideTag(o.side) },
  { key: "type", header: "Type", sort: (o) => o.order_type, csv: (o) => o.order_type, cell: (o) => o.order_type },
  { key: "qty", header: "Qty", align: "right", sort: (o) => Number(o.quantity), csv: (o) => o.quantity, cell: (o) => fmtLedgerQty(Number(o.quantity)) },
  { key: "price", header: "Limit", align: "right", sort: (o) => Number(o.price ?? 0), csv: (o) => o.price ?? "MKT", cell: (o) => (o.price ? fmtPrice(Number(o.price)) : "MKT") },
  { key: "filled", header: "Filled", align: "right", sort: (o) => Number(o.filled_qty), csv: (o) => o.filled_qty, cell: (o) => fmtLedgerQty(Number(o.filled_qty)) },
  { key: "status", header: "Status", sort: (o) => o.status, csv: (o) => o.status, cell: (o) => <span className="px-1.5 py-0.5 rounded text-2xs bg-zinc-800 text-zinc-300">{o.status}</span> },
];

const TRADE_COLUMNS: Column<PaperTrade>[] = [
  { key: "time", header: "Time (UTC)", sort: (t) => Date.parse(t.executed_at) || 0, csv: (t) => t.executed_at, cell: (t) => <span className="text-zinc-300">{ts(t.executed_at)}</span> },
  { key: "id", header: "Trade ID", sort: (t) => t.trade_id, csv: (t) => t.trade_id, cell: (t) => <span className="text-zinc-400">{t.trade_id}</span> },
  { key: "instrument", header: "Instrument", sort: (t) => t.instrument_id, csv: (t) => t.instrument_id, cell: (t) => <span className="font-bold text-white">{t.instrument_id}</span> },
  { key: "origin", header: "Origin", sort: (t) => t.instance_id ?? "", csv: (t) => t.instance_id, cell: (t) => <span className="text-indigo-300">{t.instance_id ?? "—"}</span> },
  { key: "side", header: "Side", sort: (t) => t.side, csv: (t) => t.side, cell: (t) => sideTag(t.side) },
  { key: "price", header: "Fill Price", align: "right", sort: (t) => Number(t.price), csv: (t) => t.price, cell: (t) => fmtPrice(Number(t.price)) },
  { key: "qty", header: "Qty", align: "right", sort: (t) => Number(t.quantity), csv: (t) => t.quantity, cell: (t) => fmtLedgerQty(Number(t.quantity)) },
  { key: "notional", header: "Notional", align: "right", sort: (t) => Number(t.price) * Number(t.quantity), csv: (t) => Number(t.price) * Number(t.quantity), cell: (t) => fmtPrice(Number(t.price) * Number(t.quantity)) },
  { key: "fee", header: "Fee (USDT)", align: "right", sort: (t) => Number(t.commission), csv: (t) => t.commission, cell: (t) => <span className="text-red-400">-{Number(t.commission).toFixed(4)}</span> },
];

const selectCls =
  "bg-zinc-950 border border-zinc-700 rounded px-2 py-1 text-2xs text-zinc-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-cyan-500/50";

function LedgerTable<T>({
  rows, columns, rowKey, action, emptyText, pageSize, csvName,
}: {
  rows: T[];
  columns: Column<T>[];
  rowKey: (r: T) => string;
  action?: (r: T) => React.ReactNode;
  emptyText: string;
  pageSize: number;
  csvName: string;
}) {
  const [sort, setSort] = useState<{ key: string; dir: 1 | -1 }>({ key: "time", dir: -1 });
  const [page, setPage] = useState(0);

  const sorted = useMemo(() => {
    const col = columns.find((c) => c.key === sort.key)?.sort;
    if (!col) return rows;
    return [...rows].sort((a, b) => {
      const x = col(a), y = col(b);
      return (x < y ? -1 : x > y ? 1 : 0) * sort.dir;
    });
  }, [rows, columns, sort]);

  const pages = Math.max(1, Math.ceil(sorted.length / pageSize));
  const current = Math.min(page, pages - 1);
  useEffect(() => { if (page > pages - 1) setPage(pages - 1); }, [page, pages]);
  const slice = sorted.slice(current * pageSize, current * pageSize + pageSize);

  const exportCsv = () => downloadCsv(`${csvName}-${new Date().toISOString().slice(0, 10)}.csv`, toCsv(sorted, columns.map((c) => ({ header: c.header, value: c.csv }))));

  return (
    <>
      {rows.length === 0 ? (
        <div className="text-center py-8 text-zinc-500 italic">{emptyText}</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-left">
            <thead>
              <tr className="text-zinc-500 border-b border-zinc-800 text-2xs uppercase tracking-wider">
                {columns.map((c) => {
                  const active = sort.key === c.key;
                  return (
                    <th key={c.key} className={`py-1 pr-3 font-bold ${c.align === "right" ? "text-right" : ""}`} aria-sort={active ? (sort.dir === 1 ? "ascending" : "descending") : "none"}>
                      <button
                        type="button"
                        onClick={() => setSort((s) => ({ key: c.key, dir: s.key === c.key ? (s.dir === 1 ? -1 : 1) : -1 }))}
                        className={`inline-flex items-center gap-0.5 uppercase hover:text-zinc-300 outline-none focus-visible:text-cyan-300 ${active ? "text-zinc-300" : ""}`}
                      >
                        {c.header}
                        {active && (sort.dir === 1 ? <ArrowUp className="w-2.5 h-2.5" /> : <ArrowDown className="w-2.5 h-2.5" />)}
                      </button>
                    </th>
                  );
                })}
                {action && <th className="text-right font-bold">Action</th>}
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-800/50">
              {slice.map((r) => (
                <tr key={rowKey(r)} className="hover:bg-zinc-800/30">
                  {columns.map((c) => (
                    <td key={c.key} className={`py-1.5 pr-3 tabular-nums whitespace-nowrap ${c.align === "right" ? "text-right" : ""}`}>{c.cell(r)}</td>
                  ))}
                  {action && <td className="text-right">{action(r)}</td>}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="flex items-center justify-between pt-2 mt-2 border-t border-zinc-800/60 text-2xs text-zinc-500">
        <button
          type="button"
          onClick={exportCsv}
          disabled={sorted.length === 0}
          className="inline-flex items-center gap-1 px-2 py-1 rounded border border-zinc-700 text-zinc-300 hover:bg-zinc-800 disabled:opacity-40 outline-none focus-visible:ring-2 focus-visible:ring-cyan-500/50"
        >
          <Download className="w-3 h-3" /> Export CSV
        </button>
        <div className="flex items-center gap-2 tabular-nums">
          <span>
            {sorted.length === 0 ? "0" : `${current * pageSize + 1}–${Math.min(sorted.length, (current + 1) * pageSize)}`} of {sorted.length}
          </span>
          <button type="button" aria-label="Previous page" disabled={current === 0} onClick={() => setPage(current - 1)} className="p-1 rounded border border-zinc-800 hover:bg-zinc-800 disabled:opacity-30">
            <ChevronLeft className="w-3 h-3" />
          </button>
          <span>{current + 1}/{pages}</span>
          <button type="button" aria-label="Next page" disabled={current >= pages - 1} onClick={() => setPage(current + 1)} className="p-1 rounded border border-zinc-800 hover:bg-zinc-800 disabled:opacity-30">
            <ChevronRight className="w-3 h-3" />
          </button>
        </div>
      </div>
    </>
  );
}

export function ExecutionLedger() {
  const orders = usePortfolioStore((s) => s.orders);
  const trades = usePortfolioStore((s) => s.trades);
  const prefs = usePortfolioStore((s) => s.ledger);
  const setLedger = usePortfolioStore((s) => s.setLedger);
  const [cancelling, setCancelling] = useState<string | null>(null);

  const { symbols, origins } = useMemo(() => {
    const sym = new Set<string>(), org = new Set<string>();
    for (const o of orders) { sym.add(o.instrument_id); if (o.instance_id) org.add(o.instance_id); }
    for (const t of trades) { sym.add(t.instrument_id); if (t.instance_id) org.add(t.instance_id); }
    return { symbols: [...sym].sort(), origins: [...org].sort() };
  }, [orders, trades]);

  const matches = (instrument: string, origin: string | undefined, side: string, iso: string) => {
    if (prefs.symbol && instrument !== prefs.symbol) return false;
    if (prefs.origin && origin !== prefs.origin) return false;
    if (prefs.side && side !== prefs.side) return false;
    if (prefs.range !== "all") {
      const t = Date.parse(iso);
      if (!Number.isFinite(t) || Date.now() - t > RANGE_MS[prefs.range]) return false;
    }
    return true;
  };
  const filteredOrders = orders.filter((o) => matches(o.instrument_id, o.instance_id, o.side, o.created_at));
  const filteredTrades = trades.filter((t) => matches(t.instrument_id, t.instance_id, t.side, t.executed_at));
  const filtersActive = prefs.range !== "all" || !!prefs.symbol || !!prefs.origin || !!prefs.side;

  const cancel = async (id: string) => {
    setCancelling(id);
    try { await postCommand(`/paper/orders/${id}/cancel`, undefined, `Cancel ${id} failed`); } finally { setCancelling(null); }
  };

  const tabCls = (on: boolean) => `pb-1 transition-colors ${on ? "text-cyan-400 border-b-2 border-cyan-400" : "text-zinc-500 hover:text-zinc-300"}`;

  return (
    <section className="bg-zinc-900/50 border border-zinc-800 rounded flex flex-col flex-1 min-h-[300px]" aria-label="Execution ledger">
      <div className="p-3 border-b border-zinc-800 flex flex-wrap items-center justify-between gap-3 font-bold text-xs uppercase tracking-wider text-zinc-300">
        <div className="flex space-x-6" role="tablist">
          <button role="tab" aria-selected={prefs.tab === "orders"} onClick={() => setLedger({ tab: "orders" })} className={tabCls(prefs.tab === "orders")}>
            Execution Orders Journal
          </button>
          <button role="tab" aria-selected={prefs.tab === "trades"} onClick={() => setLedger({ tab: "trades" })} className={tabCls(prefs.tab === "trades")}>
            Executed Trades Ledger
          </button>
        </div>
        <div className="flex flex-wrap items-center gap-2 normal-case tracking-normal font-normal">
          <select aria-label="Date range" value={prefs.range} onChange={(e) => setLedger({ range: e.target.value as LedgerRange })} className={selectCls}>
            <option value="24h">Last 24h</option>
            <option value="7d">Last 7 days</option>
            <option value="30d">Last 30 days</option>
            <option value="all">All time</option>
          </select>
          <select aria-label="Symbol" value={prefs.symbol} onChange={(e) => setLedger({ symbol: e.target.value })} className={selectCls}>
            <option value="">All symbols</option>
            {symbols.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
          <select aria-label="Strategy origin" value={prefs.origin} onChange={(e) => setLedger({ origin: e.target.value })} className={selectCls}>
            <option value="">All origins</option>
            {origins.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
          <select aria-label="Side" value={prefs.side} onChange={(e) => setLedger({ side: e.target.value as "" | "BUY" | "SELL" })} className={selectCls}>
            <option value="">Both sides</option>
            <option value="BUY">Buy</option>
            <option value="SELL">Sell</option>
          </select>
          {filtersActive && (
            <button
              type="button"
              onClick={() => setLedger({ range: "all", symbol: "", origin: "", side: "" })}
              className="inline-flex items-center gap-1 px-2 py-1 rounded text-2xs text-zinc-400 hover:text-white"
              title="Clear filters"
            >
              <FilterX className="w-3 h-3" /> Reset
            </button>
          )}
          <span className="text-2xs text-zinc-500 tabular-nums">
            {prefs.tab === "orders" ? filteredOrders.length : filteredTrades.length} Records
          </span>
        </div>
      </div>
      <div className="p-3">
        {prefs.tab === "orders" ? (
          <LedgerTable
            key="orders"
            rows={filteredOrders}
            columns={ORDER_COLUMNS}
            rowKey={(o) => o.client_order_id}
            emptyText={filtersActive ? "No orders match these filters." : "No orders executed yet."}
            pageSize={prefs.pageSize}
            csvName="paper-orders"
            action={(o) =>
              CANCELLABLE.has(o.status) ? (
                <button
                  onClick={() => cancel(o.client_order_id)}
                  disabled={cancelling === o.client_order_id}
                  className="px-2 py-0.5 bg-zinc-800 hover:bg-zinc-700 text-zinc-200 rounded text-2xs disabled:opacity-50"
                >
                  Cancel
                </button>
              ) : null
            }
          />
        ) : (
          <LedgerTable
            key="trades"
            rows={filteredTrades}
            columns={TRADE_COLUMNS}
            rowKey={(t) => t.trade_id}
            emptyText={filtersActive ? "No trades match these filters." : "No trades settled yet."}
            pageSize={prefs.pageSize}
            csvName="paper-trades"
          />
        )}
      </div>
    </section>
  );
}
