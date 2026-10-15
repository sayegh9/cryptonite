"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { AlertTriangle, BarChart3, CheckCircle2, FileText, GitBranch, Loader2, ScrollText, XCircle } from "lucide-react";
import { approveOrderCard, fetchCardOutcome, rejectOrderCard } from "@/lib/brain/client";
import { useBrainStore } from "@/lib/stores/useBrainStore";
import { dispatchBrainOpenTrace } from "@/lib/brain/events";
import { createMandate } from "@/lib/mission/client";
import type {
  BrainWidget,
  DebateSummaryWidget,
  OrderCardWidget,
  PriceChartWidget,
  TearsheetWidget,
} from "@/lib/brain/types";

const fmt = (n: number, digits = 2) => n.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
const cardInput = "rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-zinc-100 focus:border-amber-600 focus:outline-none";

/** Milliseconds for a bar timeframe string such as "1m", "15m", "1h", "4h", "1d". Defaults to 1m. */
function intervalMs(timeframe: string): number {
  const m = /^(\d+)m$/.exec(timeframe);
  if (m) return Number(m[1]) * 60_000;
  const h = /^(\d+)h$/.exec(timeframe);
  if (h) return Number(h[1]) * 3_600_000;
  const d = /^(\d+)d$/.exec(timeframe);
  if (d) return Number(d[1]) * 86_400_000;
  const s = /^(\d+)s$/.exec(timeframe);
  if (s) return Number(s[1]) * 1_000;
  return 60_000;
}

/** Compact human age like "3m", "12h", "1d 5h". */
function formatAge(ms: number): string {
  const mins = Math.max(0, Math.round(ms / 60_000));
  if (mins < 1) return "<1m";
  if (mins < 60) return `${mins}m`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h${mins % 60 >= 10 ? ` ${mins % 60}m` : ""}`;
  const days = Math.floor(hours / 24);
  return `${days}d${hours % 24 ? ` ${hours % 24}h` : ""}`;
}

function WidgetFrame({ icon, title, children, testId }: { icon: React.ReactNode; title: string; children: React.ReactNode; testId: string }) {
  return (
    <section data-testid={testId} className="mt-2 rounded-lg border border-zinc-800 bg-zinc-950/80 p-3 font-mono text-2xs text-zinc-300">
      <header className="mb-2 flex items-center gap-1.5 text-2xs font-bold uppercase tracking-wider text-zinc-400">
        {icon}
        <span>{title}</span>
      </header>
      {children}
    </section>
  );
}

/** Plain words for the usual reasons an approved order is not executed; the code stays visible beside them */
const NOT_EXECUTED_REASON: [prefix: string, text: string][] = [
  ["MANDATE_SIZING_CONFLICT", "the system could not size it inside the mandate (a system fault, not counted against the AI)"],
  ["MANDATE_POSITION_LIMIT_REACHED", "the mandate has no room left in this instrument"],
  ["MANDATE_POSITION_PCT_EXCEEDED", "it would go over the mandate's position limit"],
  ["MANDATE_BUDGET_EXCEEDED", "it would go over the mandate's budget"],
  ["MANDATE_HALTED", "the mandate is paused or halted"],
  ["MANDATE_INSTRUMENT_CONFLICT", "another mandate or a manual trade holds this instrument"],
  ["PRICE_COLLAR_REJECT", "the limit price is too far from the market"],
  ["NO_LIVE_QUOTE_FOR_SIZING", "there was no live price to size it"],
  ["KILL_SWITCH", "the kill switch is latched"],
];

export function notExecutedText(reason: string | null | undefined): string {
  if (!reason) return "the order was refused";
  const known = NOT_EXECUTED_REASON.find(([code]) => reason.startsWith(code));
  return known ? known[1] : reason;
}

/**
 * Follows an approved card's order until it is filled or refused: the server records the change in the conversation
 * (so the Brain knows), and the card shows it. Fast at first (an order is placed or refused within seconds), then
 * slower; a resting limit is checked every 30 s.
 */
function useCardOutcome(widget: OrderCardWidget, sessionId: string | null) {
  const updateOrderCard = useBrainStore((s) => s.updateOrderCard);
  // One loop per approved card: moving from APPROVED to WORKING continues it (slower), it does not restart it
  const followed = widget.status === "APPROVED" || widget.status === "WORKING";
  const working = useRef(widget.status === "WORKING");
  useEffect(() => {
    if (!followed || !sessionId || !widget.command_id) return;
    const ctrl = new AbortController();
    const started = Date.now();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      try {
        const out = await fetchCardOutcome(sessionId, widget.id, ctrl.signal);
        if (out.state === "WORKING" || out.state === "FILLED" || out.state === "NOT_EXECUTED") {
          working.current = out.state === "WORKING";
          updateOrderCard(widget.id, {
            status: out.state,
            execution_reason: out.reason ?? null,
            filled_qty: out.filled_qty ?? null,
            avg_fill_price: out.avg_fill_price ?? null,
          });
          if (out.state !== "WORKING") return;
        }
      } catch {
        if (ctrl.signal.aborted) return;
      }
      const wait = working.current ? 30_000 : Date.now() - started < 60_000 ? 3_000 : 15_000;
      timer = setTimeout(poll, wait);
    };
    void poll();
    return () => {
      ctrl.abort();
      if (timer) clearTimeout(timer);
    };
  }, [followed, sessionId, widget.id, widget.command_id, updateOrderCard]);
}

/** What happened after you approved: sending, working, filled, or not executed with the reason */
function OrderOutcome({ widget }: { widget: OrderCardWidget }) {
  const cmd = widget.command_id ? <span className="text-zinc-600"> · paper command {widget.command_id}</span> : null;
  switch (widget.status) {
    case "APPROVED":
      return (
        <p role="status" className="mt-2 flex items-center gap-1 text-zinc-300" data-testid="card-outcome">
          <Loader2 className="h-3 w-3 motion-safe:animate-spin" /> Approved · sending the order{cmd}
        </p>
      );
    case "WORKING":
      return (
        <p role="status" className="mt-2 flex items-center gap-1 text-cyan-300" data-testid="card-outcome">
          <Loader2 className="h-3 w-3 motion-safe:animate-spin" /> Working · limit {widget.side} at {widget.limit_price}, not
          filled yet{cmd}
        </p>
      );
    case "FILLED":
      return (
        <p role="status" className="mt-2 flex items-center gap-1 text-emerald-400" data-testid="card-outcome">
          <CheckCircle2 className="h-3 w-3" /> Filled · {widget.filled_qty} at {widget.avg_fill_price}
          {widget.execution_reason ? ` (${widget.execution_reason})` : ""}
          {cmd}
        </p>
      );
    case "NOT_EXECUTED":
      return (
        <div role="alert" className="mt-2 text-rose-300" data-testid="card-outcome">
          <p className="flex items-center gap-1">
            <AlertTriangle className="h-3 w-3 flex-none" /> Not executed: {notExecutedText(widget.execution_reason)}. No
            position was opened.
          </p>
          <p className="mt-0.5 font-mono text-zinc-500">
            {widget.execution_reason}
            {cmd}
          </p>
        </div>
      );
    default:
      return null;
  }
}

function PriceChartCard({ widget }: { widget: PriceChartWidget }) {
  const [bars, setBars] = useState<any[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const symbol = widget.symbol.split(".")[0];
    fetch(`/api/v1/bars?symbol=${encodeURIComponent(symbol)}&timeframe=${encodeURIComponent(widget.timeframe)}&limit=120`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((body) => {
        if (cancelled) return;
        const rows: any[] = Array.isArray(body) ? body : body.bars ?? [];
        setBars(rows);
      })
      .catch((e) => !cancelled && setError(e.message));
    return () => {
      cancelled = true;
    };
  }, [widget.symbol, widget.timeframe]);

  const symbol = widget.symbol.split(".")[0];
  const closes = (bars ?? []).map((b) => Number(b.close)).filter((v) => Number.isFinite(v));

  // Staleness: this card shows STORED bars from the analytics lake (/api/v1/bars). A chart is stale
  // when its last bar closed more than two full timeframes ago; live REST prices discussed by the
  // Brain live in the conversation, not in this stored chart.
  const interval = intervalMs(widget.timeframe);
  const lastBar = bars && bars.length > 0 ? bars[bars.length - 1] : null;
  const lastCloseMs = lastBar
    ? Number(lastBar.bar_close_inclusive_ns) > 0
      ? Number(lastBar.bar_close_inclusive_ns) / 1_000_000
      : (Number(lastBar.time ?? 0) > 0 ? Number(lastBar.time) : Date.now() / 1000) * 1000 + interval
    : null;
  const ageMs = lastCloseMs == null ? null : Date.now() - lastCloseMs;
  const stale = ageMs != null && ageMs > 2 * interval;

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
      {bars === null && !error && (
        <div className="flex h-[72px] items-center justify-center text-zinc-500">
          <Loader2 className="mr-1 h-3 w-3 animate-spin" /> Loading bars…
        </div>
      )}
      {error && <div className="text-zinc-500">No bars available ({error}).</div>}
      {bars !== null && !error && bars.length === 0 && (
        <div className="text-zinc-500" data-testid="brain-widget-price-chart-empty">
          No stored bars for {symbol}.
        </div>
      )}
      {bars !== null && !error && bars.length > 0 && closes.length <= 1 && <div className="text-zinc-500">Not enough bars to draw.</div>}
      {bars !== null && !error && closes.length > 1 && (
        <div>
          <svg viewBox={`0 0 ${W} ${H}`} className="h-[72px] w-full" role="img" aria-label={`${widget.symbol} price, ${closes.length} bars`}>
            <path d={path} fill="none" strokeWidth="1.5" className={up ? "stroke-emerald-400" : "stroke-rose-400"} />
          </svg>
          <div className="mt-1 flex justify-between text-2xs text-zinc-400">
            <span>
              Last <span className="text-zinc-100">{fmt(closes[closes.length - 1])}</span>
            </span>
            <span className={up ? "text-emerald-400" : "text-rose-400"}>
              {up ? "+" : ""}
              {fmt(change ?? 0)}% over {closes.length} bars
            </span>
          </div>
          {stale && (
            <div className="mt-1 flex items-start gap-1 text-amber-400" data-testid="brain-widget-price-chart-stale" role="note">
              <AlertTriangle className="mt-px h-3 w-3 flex-none" aria-hidden />
              <span>
                Stored bars are stale — last bar {formatAge(ageMs!)} ago (&gt;2×{widget.timeframe}); live REST prices are in the
                discussion.
              </span>
            </div>
          )}
        </div>
      )}
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
              <dt className="text-2xs uppercase text-zinc-500">{METRIC_LABELS[k] ?? k}</dt>
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
        <span className={`rounded border px-1.5 py-0.5 text-2xs font-bold ${widget.status === "QUEUED" ? "border-purple-800 text-purple-300" : "border-rose-800 text-rose-300"}`}>
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

/** How long an onboarded symbol gets to start streaming, how often the live feed is checked, and each check's deadline */
const ONBOARD_WAIT_MS = 180_000;
const ONBOARD_POLL_MS = 5_000;
const FEED_REQUEST_MS = 10_000;

/** Instruments the live feed streams right now (e.g. "SOLUSDT.BINANCE"); null while ingest has not published its universe */
async function fetchFedInstruments(signal?: AbortSignal): Promise<string[] | null> {
  // Each check has its own deadline, so one hung request cannot stretch the onboarding window
  const ctrl = new AbortController();
  const deadline = setTimeout(() => ctrl.abort(), FEED_REQUEST_MS);
  const cancel = () => ctrl.abort();
  signal?.addEventListener("abort", cancel);
  try {
    const res = await fetch("/api/v1/market/universe", { credentials: "include", cache: "no-store", signal: ctrl.signal });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const body = await res.json();
    return body.available && Array.isArray(body.instruments) ? body.instruments : null;
  } finally {
    clearTimeout(deadline);
    signal?.removeEventListener("abort", cancel);
  }
}

/** Whether the feed streams this exact instrument: the venue counts (BTCUSDT.BYBIT is not BTCUSDT.BINANCE) */
const isFed = (fed: string[] | null, instrumentId: string) => fed?.includes(instrumentId.toUpperCase()) ?? false;

/** The reason the API gives for a refused request (its `detail`), or the status */
async function failureText(res: Response): Promise<string> {
  const body = await res.json().catch(() => null);
  return typeof body?.detail === "string" ? body.detail : `HTTP ${res.status}`;
}

/**
 * Shown on an order card the live feed does not allow, instead of a form that cannot succeed. Only SYMBOL_NOT_INGESTED
 * has a fix here (Onboard); other refusals (venue, perp, timeframe) stay locked and show their reason. For a symbol the
 * feed is checked once when shown (it may have been onboarded since), then every 5 s for up to 3 minutes after Onboard;
 * onStreaming runs once this exact instrument is in the feed.
 */
function FeedGate({ instrumentId, reason, onStreaming }: { instrumentId: string; reason: string; onStreaming: () => void }) {
  const symbol = instrumentId.split(".")[0];
  const onboardable = reason.startsWith("SYMBOL_NOT_INGESTED");
  const [phase, setPhase] = useState<"idle" | "onboarding" | "timeout">("idle");
  const [error, setError] = useState<string | null>(null);
  const polling = useRef<AbortController | null>(null);

  useEffect(() => {
    const ctrl = new AbortController();
    if (onboardable) {
      fetchFedInstruments(ctrl.signal)
        .then((fed) => {
          if (isFed(fed, instrumentId)) onStreaming();
        })
        .catch(() => {});
    }
    return () => ctrl.abort();
  }, [instrumentId, onboardable, onStreaming]);

  // Stop polling on unmount
  useEffect(() => () => polling.current?.abort(), []);

  const onboard = async () => {
    polling.current?.abort();
    const ctrl = new AbortController();
    polling.current = ctrl;
    setError(null);
    setPhase("onboarding");
    try {
      const res = await fetch("/api/v1/instruments/onboard", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": localStorage.getItem("csrf_token") ?? "" },
        body: JSON.stringify({ symbol }),
        signal: ctrl.signal,
      });
      if (!res.ok) throw new Error(await failureText(res));
    } catch (e: any) {
      if (ctrl.signal.aborted) return;
      setError(e?.message || "Onboarding failed");
      setPhase("idle");
      return;
    }
    const started = Date.now();
    while (!ctrl.signal.aborted) {
      await new Promise((resolve) => setTimeout(resolve, ONBOARD_POLL_MS));
      if (ctrl.signal.aborted) return;
      try {
        const fed = await fetchFedInstruments(ctrl.signal);
        if (isFed(fed, instrumentId)) {
          onStreaming();
          return;
        }
      } catch {
        if (ctrl.signal.aborted) return;
      }
      if (Date.now() - started >= ONBOARD_WAIT_MS) {
        setPhase("timeout");
        return;
      }
    }
  };

  const busy = phase === "onboarding";
  return (
    <div className="mt-2 rounded border border-amber-700 bg-zinc-950 p-2" data-testid="brain-order-feed-gate">
      {onboardable ? (
        <p role="note" className="text-amber-300">
          {symbol} is not in the live data feed, so this order cannot be approved yet. Onboard it first; the card unlocks once{" "}
          {symbol} is streaming.
        </p>
      ) : (
        <p role="note" className="text-amber-300">
          {reason}. This order cannot be approved.
        </p>
      )}
      {onboardable && (
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <button
            type="button"
            disabled={busy}
            onClick={() => void onboard()}
            className="rounded border border-amber-600 bg-amber-600/20 px-2 py-1 text-2xs font-bold text-amber-200 hover:bg-amber-600/30 disabled:opacity-50"
          >
            {busy ? `Onboarding ${symbol}…` : `Onboard ${symbol}`}
          </button>
          {phase === "timeout" && <p role="status" className="text-zinc-400">Still not streaming; try again or check the Watchlist.</p>}
        </div>
      )}
      {error && (
        <p role="alert" className="mt-1 text-rose-400">
          {error}
        </p>
      )}
    </div>
  );
}

function OrderCard({ widget }: { widget: OrderCardWidget }) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // An entry on an instrument no ACTIVE mandate allows: the card creates the mandate inline (TOTP, checked by the API)
  const [needsMandate, setNeedsMandate] = useState(false);
  const [mandateName, setMandateName] = useState(`Brain ${widget.instrument_id}`);
  const [mandateBudget, setMandateBudget] = useState("10000");
  const [totp, setTotp] = useState("");
  const pending = widget.status === "PENDING_APPROVAL";
  const buy = widget.side === "BUY";
  // An instrument the live feed does not allow cannot be approved: no mandate form, no TOTP, Approve disabled until it streams
  const [fed, setFed] = useState(false);
  const unlockFeed = useCallback(() => setFed(true), []);
  const feedRefusal = pending ? widget.feed_refusal ?? null : null;
  const feedLocked = !!feedRefusal && !fed;
  const sessionId = useBrainStore((s) => s.sessionId);
  useCardOutcome(widget, sessionId);

  const approve = async () => {
    setBusy(true);
    setError(null);
    try {
      let mandateId: string | undefined;
      if (needsMandate) {
        // A mandate gives the AI room, so creating one needs the current TOTP code (the same rule as the Mandate editor).
        // The API also refuses an instrument a paused or halted mandate holds (MANDATE_PAUSED / MANDATE_HALTED).
        mandateId = (await createMandate({
          name: mandateName.trim(),
          capital_budget_usd: mandateBudget,
          instruments: [widget.instrument_id],
          max_position_pct: 10,
          max_drawdown_pct: 20,
          veto_window_s: 600,
          allow_perps: false,
          totp_code: totp,
        })).id;
        setNeedsMandate(false);
      }
      await approveOrderCard(widget, mandateId);
      setConfirming(false);
    } catch (e: any) {
      const msg: string = e?.message || "Order rejected";
      if (buy && msg.startsWith("NO_MANDATE")) setNeedsMandate(true);
      else setError(msg);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section
      data-testid="brain-widget-order-card"
      aria-label={buy
        ? `Order proposal: BUY ${widget.allocation_pct}% of mandate ${widget.instrument_id}`
        : `Order proposal: SELL the whole holding of ${widget.instrument_id}`}
      className="mt-2 rounded-lg border border-amber-700/60 bg-amber-950/10 p-3 font-mono text-2xs text-zinc-300"
    >
      <header className="mb-2 flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5">
          <AlertTriangle className="h-3 w-3 text-amber-400" aria-hidden />
          <span className="rounded border border-amber-600 bg-amber-500/10 px-1.5 py-0.5 text-2xs font-black tracking-wider text-amber-300">
            {buy ? "ADDS RISK · PAPER" : "CLOSES HOLDING · PAPER"}
          </span>
          <span className="text-2xs font-bold uppercase text-zinc-400">Order proposal</span>
        </div>
        {widget.trace_id ? (
          <button
            type="button"
            onClick={() => dispatchBrainOpenTrace(widget.trace_id!)}
            className="flex items-center gap-1 text-2xs text-cyan-400 hover:text-cyan-300 hover:underline"
            aria-label="Why? Open the decision trace for this order card"
          >
            <ScrollText className="h-3 w-3" /> Why? (trace)
          </button>
        ) : (
          <span className="text-2xs text-zinc-600">no trace</span>
        )}
      </header>

      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className={`text-sm font-black ${buy ? "text-emerald-400" : "text-rose-400"}`}>{widget.side}</span>
        <span className="text-sm text-zinc-100">{widget.instrument_id}</span>
        <span className="text-zinc-400">
          {widget.order_type}
          {widget.order_type === "LIMIT" && widget.limit_price ? ` @ ${fmt(widget.limit_price)}` : ""}
        </span>
      </div>
      <dl className="mt-1 grid grid-cols-3 gap-2 text-2xs">
        <div>
          <dt className="text-zinc-500">Size</dt>
          {buy ? (
            <dd className="text-zinc-100" title="Sized at approval from the live price; capped at the room left under the mandate's position limit">
              {widget.allocation_pct}% of mandate
            </dd>
          ) : (
            <dd className="text-zinc-100" title="A spot sell closes the mandate's whole holding of this instrument">Whole holding</dd>
          )}
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

      {feedLocked && feedRefusal ? (
        <FeedGate instrumentId={widget.instrument_id} reason={feedRefusal} onStreaming={unlockFeed} />
      ) : null}
      {pending && fed && (
        <p role="status" className="mt-2 flex items-center gap-1 text-emerald-400">
          <CheckCircle2 className="h-3 w-3" aria-hidden /> {widget.instrument_id.split(".")[0]} is streaming; you can approve now.
        </p>
      )}

      {pending && !confirming && (
        <div className="mt-3 flex gap-2">
          <button
            type="button"
            disabled={feedLocked}
            onClick={() => setConfirming(true)}
            className="flex-1 rounded border border-amber-600 bg-amber-600/20 px-2 py-1.5 text-2xs font-bold text-amber-200 hover:bg-amber-600/30 disabled:cursor-not-allowed disabled:opacity-50"
          >
            Approve…
          </button>
          <button
            type="button"
            onClick={() => rejectOrderCard(widget).catch((e) => setError(e?.message || "Reject failed"))}
            className="flex-1 rounded border border-zinc-700 bg-zinc-900 px-2 py-1.5 text-2xs font-bold text-zinc-300 hover:bg-zinc-800"
          >
            Reject
          </button>
        </div>
      )}
      {pending && confirming && !feedLocked && (
        <div className="mt-3 rounded border border-amber-700 bg-zinc-950 p-2" role="group" aria-label="Confirm paper order">
          {needsMandate ? (
            <>
              <p className="mb-2 text-zinc-300">
                No ACTIVE mandate allows {widget.instrument_id} yet. Confirming creates one with the budget below, at L1 Copilot
                (every order still needs your approval), then queues this order.
              </p>
              <div className="mb-2 grid grid-cols-2 gap-2 text-2xs">
                <label className="col-span-2 flex flex-col gap-1 text-zinc-400">
                  Mandate name
                  <input className={cardInput} value={mandateName} maxLength={80} onChange={(e) => setMandateName(e.target.value)} />
                </label>
                <label className="flex flex-col gap-1 text-zinc-400">
                  Budget (USD)
                  <input className={cardInput} type="number" min="1" step="any" value={mandateBudget} onChange={(e) => setMandateBudget(e.target.value)} />
                </label>
                <label className="flex flex-col gap-1 text-zinc-400">
                  TOTP code
                  <input
                    className={cardInput}
                    aria-label="TOTP code"
                    inputMode="numeric"
                    autoComplete="one-time-code"
                    maxLength={6}
                    value={totp}
                    onChange={(e) => setTotp(e.target.value.replace(/\D/g, ""))}
                  />
                </label>
              </div>
            </>
          ) : !buy ? (
            <p className="mb-2 text-zinc-300">
              Send a PAPER SELL {widget.instrument_id} {widget.order_type} order for the whole holding the mandate has in it? It only
              closes a position the mandate already holds; spot cannot be shorted.
            </p>
          ) : (
            <p className="mb-2 text-zinc-300">
              Send a PAPER {widget.side} {widget.instrument_id} {widget.order_type} order sized at {widget.allocation_pct}% of the mandate
              budget? It trades inside the ACTIVE mandate that allows this instrument; the mandate check, the 1–25% equity clamp and
              the kill switch still apply. The position is stopped out by the exit
              monitor&apos;s hard and trailing stops, not by the proposed stop price; approval is refused while the exit monitor is down.
            </p>
          )}
          <div className="flex gap-2">
            <button
              type="button"
              disabled={busy || (needsMandate && (totp.length !== 6 || !mandateName.trim() || !(Number(mandateBudget) > 0)))}
              onClick={approve}
              className="flex-1 rounded bg-amber-600 px-2 py-1.5 text-2xs font-bold text-zinc-950 hover:bg-amber-500 disabled:opacity-50"
            >
              {busy ? "Sending…" : needsMandate ? "Create mandate and confirm" : "Confirm paper order"}
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => setConfirming(false)}
              className="rounded border border-zinc-700 px-2 py-1.5 text-2xs text-zinc-300 hover:bg-zinc-800"
            >
              Cancel
            </button>
          </div>
        </div>
      )}
      <OrderOutcome widget={widget} />
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
      {!buy && error?.startsWith("NO_MANDATE") && (
        <p className="mt-1 text-zinc-500">A spot sell can only sell what a mandate already holds; spot shorts are refused. Reject this card.</p>
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
