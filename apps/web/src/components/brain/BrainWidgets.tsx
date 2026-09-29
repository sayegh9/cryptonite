"use client";

import { useEffect, useState } from "react";
import { AlertTriangle, BarChart3, CheckCircle2, FileText, GitBranch, Loader2, ScrollText, XCircle } from "lucide-react";
import { approveOrderCard, rejectOrderCard } from "@/lib/brain/client";
import { dispatchBrainOpenTrace } from "@/lib/brain/events";
import type {
  BrainWidget,
  DebateSummaryWidget,
  OrderCardWidget,
  PriceChartWidget,
  TearsheetWidget,
} from "@/lib/brain/types";

const fmt = (n: number, digits = 2) => n.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });

function WidgetFrame({ icon, title, children, testId }: { icon: React.ReactNode; title: string; children: React.ReactNode; testId: string }) {
  return (
    <section data-testid={testId} className="mt-2 rounded-lg border border-zinc-800 bg-zinc-950/80 p-3 font-mono text-[11px] text-zinc-300">
      <header className="mb-2 flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-wider text-zinc-400">
        {icon}
        <span>{title}</span>
      </header>
      {children}
    </section>
  );
}

function PriceChartCard({ widget }: { widget: PriceChartWidget }) {
  const [closes, setCloses] = useState<number[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const symbol = widget.symbol.split(".")[0];
    fetch(`/api/v1/bars?symbol=${encodeURIComponent(symbol)}&timeframe=${encodeURIComponent(widget.timeframe)}&limit=120`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((body) => {
        if (cancelled) return;
        const bars: any[] = Array.isArray(body) ? body : body.bars ?? [];
        setCloses(bars.map((b) => Number(b.close)).filter((v) => Number.isFinite(v)));
      })
      .catch((e) => !cancelled && setError(e.message));
    return () => {
      cancelled = true;
    };
  }, [widget.symbol, widget.timeframe]);

  const W = 320;
  const H = 72;
  let path = "";
  let change: number | null = null;
  if (closes && closes.length > 1) {
    const lo = Math.min(...closes);
    const hi = Math.max(...closes);
    const span = hi - lo || 1;
    path = closes
      .map((c, i) => `${i === 0 ? "M" : "L"}${((i / (closes.length - 1)) * W).toFixed(1)},${(H - ((c - lo) / span) * H).toFixed(1)}`)
      .join(" ");
    change = ((closes[closes.length - 1] - closes[0]) / closes[0]) * 100;
  }
  const up = (change ?? 0) >= 0;

  return (
    <WidgetFrame testId="brain-widget-price-chart" icon={<BarChart3 className="h-3 w-3 text-cyan-400" />} title={`${widget.symbol} · ${widget.timeframe}`}>
      {closes === null && !error && (
        <div className="flex h-[72px] items-center justify-center text-zinc-500">
          <Loader2 className="mr-1 h-3 w-3 animate-spin" /> Loading bars…
        </div>
      )}
      {error && <div className="text-zinc-500">No bars available ({error}).</div>}
      {closes && closes.length > 1 && (
        <div>
          <svg viewBox={`0 0 ${W} ${H}`} className="h-[72px] w-full" role="img" aria-label={`${widget.symbol} price, ${closes.length} bars`}>
            <path d={path} fill="none" strokeWidth="1.5" className={up ? "stroke-emerald-400" : "stroke-rose-400"} />
          </svg>
          <div className="mt-1 flex justify-between text-[10px] text-zinc-400">
            <span>
              Last <span className="text-zinc-100">{fmt(closes[closes.length - 1])}</span>
            </span>
            <span className={up ? "text-emerald-400" : "text-rose-400"}>
              {up ? "+" : ""}
              {fmt(change ?? 0)}% over {closes.length} bars
            </span>
          </div>
        </div>
      )}
      {closes && closes.length <= 1 && !error && <div className="text-zinc-500">Not enough bars to draw.</div>}
    </WidgetFrame>
  );
}

const METRIC_LABELS: Record<string, string> = {
  total_return_pct: "Return %",
  max_drawdown_pct: "Max DD %",
  total_trades: "Trades",
  win_rate_pct: "Win rate %",
  sharpe_ratio: "Sharpe",
  total_realized_pnl: "Realized PnL",
  final_equity: "Final equity",
  strategy: "Strategy",
  instrument_id: "Instrument",
  timeframe: "Timeframe",
};

function TearsheetCard({ widget }: { widget: TearsheetWidget }) {
  const entries = Object.entries(widget.metrics).filter(([k, v]) => k !== "backtest_id" && (typeof v === "number" || typeof v === "string"));
  return (
    <WidgetFrame testId="brain-widget-tearsheet" icon={<FileText className="h-3 w-3 text-amber-400" />} title={`Tearsheet · ${widget.backtest_id}`}>
      {entries.length === 0 ? (
        <div className="text-zinc-500">The backtest returned no metrics.</div>
      ) : (
        <dl className="grid grid-cols-2 gap-x-4 gap-y-1 sm:grid-cols-3">
          {entries.slice(0, 9).map(([k, v]) => (
            <div key={k} className="flex flex-col">
              <dt className="text-[9px] uppercase text-zinc-500">{METRIC_LABELS[k] ?? k}</dt>
              <dd className="text-zinc-100">{typeof v === "number" ? fmt(v, Number.isInteger(v) ? 0 : 2) : String(v)}</dd>
            </div>
          ))}
        </dl>
      )}
    </WidgetFrame>
  );
}

function DebateSummaryCard({ widget }: { widget: DebateSummaryWidget }) {
  return (
    <WidgetFrame testId="brain-widget-debate" icon={<GitBranch className="h-3 w-3 text-purple-400" />} title={`Debate · ${widget.instrument_id}`}>
      <div className="flex flex-wrap items-center gap-2">
        <span className={`rounded border px-1.5 py-0.5 text-[10px] font-bold ${widget.status === "QUEUED" ? "border-purple-800 text-purple-300" : "border-rose-800 text-rose-300"}`}>
          {widget.status === "QUEUED" ? "QUEUED" : "NOT QUEUED"}
        </span>
        {widget.recommendation && <span>Thesis: {widget.recommendation}</span>}
        {widget.conviction_score != null && <span className="text-zinc-400">conviction {fmt(widget.conviction_score * 100, 0)}%</span>}
        <span className="text-zinc-500">{widget.timeframe}</span>
      </div>
      {widget.message && <p className="mt-1 text-zinc-500">{widget.message}</p>}
      <p className="mt-1 text-zinc-500">The debate's verdict lands in the Debates view and still needs your approval.</p>
    </WidgetFrame>
  );
}

function OrderCard({ widget }: { widget: OrderCardWidget }) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pending = widget.status === "PENDING_APPROVAL";
  const buy = widget.side === "BUY";

  const approve = async () => {
    setBusy(true);
    setError(null);
    try {
      await approveOrderCard(widget);
      setConfirming(false);
    } catch (e: any) {
      setError(e?.message || "Order rejected");
    } finally {
      setBusy(false);
    }
  };

  return (
    <section
      data-testid="brain-widget-order-card"
      aria-label={`Order proposal: ${widget.side} ${widget.quantity} ${widget.instrument_id}`}
      className="mt-2 rounded-lg border border-amber-700/60 bg-amber-950/10 p-3 font-mono text-[11px] text-zinc-300"
    >
      <header className="mb-2 flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5">
          <AlertTriangle className="h-3 w-3 text-amber-400" aria-hidden />
          <span className="rounded border border-amber-600 bg-amber-500/10 px-1.5 py-0.5 text-[9px] font-black tracking-wider text-amber-300">
            ADDS RISK · PAPER
          </span>
          <span className="text-[10px] font-bold uppercase text-zinc-400">Order proposal</span>
        </div>
        {widget.trace_id ? (
          <button
            type="button"
            onClick={() => dispatchBrainOpenTrace(widget.trace_id!)}
            className="flex items-center gap-1 text-[10px] text-cyan-400 hover:text-cyan-300 hover:underline"
            aria-label="Why? Open the decision trace for this order card"
          >
            <ScrollText className="h-3 w-3" /> Why? (trace)
          </button>
        ) : (
          <span className="text-[10px] text-zinc-600">no trace</span>
        )}
      </header>

      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className={`text-sm font-black ${buy ? "text-emerald-400" : "text-rose-400"}`}>{widget.side}</span>
        <span className="text-sm text-zinc-100">
          {widget.quantity} {widget.instrument_id}
        </span>
        <span className="text-zinc-400">
          {widget.order_type}
          {widget.order_type === "LIMIT" && widget.limit_price ? ` @ ${fmt(widget.limit_price)}` : ""}
        </span>
      </div>
      <dl className="mt-1 grid grid-cols-3 gap-2 text-[10px]">
        <div>
          <dt className="text-zinc-500">Allocation</dt>
          <dd className="text-zinc-100">{widget.allocation_pct}% of equity</dd>
        </div>
        <div>
          <dt className="text-zinc-500">Proposed stop</dt>
          <dd className="text-zinc-100">{widget.stop_loss ? fmt(widget.stop_loss) : "—"}</dd>
        </div>
        <div>
          <dt className="text-zinc-500">Target</dt>
          <dd className="text-zinc-100">{widget.take_profit ? fmt(widget.take_profit) : "—"}</dd>
        </div>
      </dl>
      {widget.rationale && <p className="mt-2 text-zinc-400">{widget.rationale}</p>}

      {pending && !confirming && (
        <div className="mt-3 flex gap-2">
          <button
            type="button"
            onClick={() => setConfirming(true)}
            className="flex-1 rounded border border-amber-600 bg-amber-600/20 px-2 py-1.5 text-[11px] font-bold text-amber-200 hover:bg-amber-600/30"
          >
            Approve…
          </button>
          <button
            type="button"
            onClick={() => rejectOrderCard(widget).catch((e) => setError(e?.message || "Reject failed"))}
            className="flex-1 rounded border border-zinc-700 bg-zinc-900 px-2 py-1.5 text-[11px] font-bold text-zinc-300 hover:bg-zinc-800"
          >
            Reject
          </button>
        </div>
      )}
      {pending && confirming && (
        <div className="mt-3 rounded border border-amber-700 bg-zinc-950 p-2" role="group" aria-label="Confirm paper order">
          <p className="mb-2 text-zinc-300">
            Send a PAPER {widget.side} {widget.instrument_id} {widget.order_type} order sized at {widget.allocation_pct}% of equity?
            The risk engine still applies the 1–25% equity clamp and the kill switch. The position is stopped out by the exit
            monitor&apos;s hard and trailing stops, not by the proposed stop price; approval is refused while the exit monitor is down.
          </p>
          <div className="flex gap-2">
            <button
              type="button"
              disabled={busy}
              onClick={approve}
              className="flex-1 rounded bg-amber-600 px-2 py-1.5 text-[11px] font-bold text-zinc-950 hover:bg-amber-500 disabled:opacity-50"
            >
              {busy ? "Sending…" : "Confirm paper order"}
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => setConfirming(false)}
              className="rounded border border-zinc-700 px-2 py-1.5 text-[11px] text-zinc-300 hover:bg-zinc-800"
            >
              Cancel
            </button>
          </div>
        </div>
      )}
      {widget.status === "APPROVED" && (
        <p role="status" className="mt-2 flex items-center gap-1 text-emerald-400">
          <CheckCircle2 className="h-3 w-3" /> Approved · paper command {widget.command_id}
        </p>
      )}
      {widget.status === "REJECTED" && (
        <p role="status" className="mt-2 flex items-center gap-1 text-zinc-400">
          <XCircle className="h-3 w-3" /> Rejected by you
        </p>
      )}
      {(widget.status === "FAILED" || error) && (
        <p role="alert" className="mt-2 flex items-center gap-1 text-rose-400">
          <XCircle className="h-3 w-3" /> Order not queued{error ? `: ${error}` : ""}
        </p>
      )}
    </section>
  );
}

export function BrainWidgetView({ widget }: { widget: BrainWidget }) {
  switch (widget.kind) {
    case "price_chart":
      return <PriceChartCard widget={widget} />;
    case "tearsheet":
      return <TearsheetCard widget={widget} />;
    case "order_card":
      return <OrderCard widget={widget} />;
    case "debate_summary":
      return <DebateSummaryCard widget={widget} />;
    default:
      return null;
  }
}
