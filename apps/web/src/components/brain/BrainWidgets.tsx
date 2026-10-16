"use client";

import { useCallback, useEffect, useId, useRef, useState } from "react";
import {
  AlertTriangle,
  BarChart3,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  FileText,
  GitBranch,
  Loader2,
  ScrollText,
  XCircle,
} from "lucide-react";
import { approveOrderCard, fetchCardDebate, fetchCardOutcome, fetchCardPreflight, rejectOrderCard } from "@/lib/brain/client";
import { useBrainStore } from "@/lib/stores/useBrainStore";
import { dispatchBrainOpenTrace } from "@/lib/brain/events";
import { createMandate } from "@/lib/mission/client";
import { buildQuery, dispatchSwitchWorkspace } from "@/lib/navigation";
import type {
  BrainWidget,
  CardEvidence,
  CardLesson,
  CardPreflight,
  DebateCardStatus,
  DebateOutcome,
  DebateSummaryWidget,
  DebateVerdict,
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

/** A debate card asks for its debate every 10 s while the debate is QUEUED or RUNNING, and for at most 30 min */
const DEBATE_POLL_MS = 10_000;
const DEBATE_FOLLOW_MS = 30 * 60_000;
const DEBATE_FINAL: readonly DebateCardStatus[] = ["VERDICT", "NO_TRADE", "GATE_REFUSED", "FAILED"];

/** Plain words for the gate reasons that refuse a Brain debate; an unknown reason is shown as its code */
const GATE_REFUSED_REASON: [prefix: string, text: string][] = [
  ["KILL_SWITCH_LATCHED", "the kill switch is latched"],
  ["DAILY_LOSS_LATCHED", "the daily-loss latch is on"],
  ["POSITION_AT_CAP", "the position is at its cap"],
];

/** Plain words for a verdict's approval state (the ai_approvals statuses); the approval is always the operator's */
const APPROVAL_WORDS: Record<string, string> = {
  PENDING: "awaiting approval",
  APPROVED: "approved",
  VETOED: "vetoed",
  EXPIRED: "expired (no order)",
  EXPIRED_PROCEEDED: "expired and proceeded",
  CANCELLED: "cancelled",
};

const DEBATE_BADGE: Record<DebateCardStatus, { label: string; tone: string }> = {
  QUEUED: { label: "QUEUED", tone: "border-purple-800 text-purple-300" },
  RUNNING: { label: "RUNNING", tone: "border-purple-800 text-purple-300" },
  VERDICT: { label: "VERDICT", tone: "border-amber-800 text-amber-300" },
  NO_TRADE: { label: "NO TRADE", tone: "border-sky-800 text-sky-300" },
  GATE_REFUSED: { label: "NOT RUN", tone: "border-zinc-700 text-zinc-300" },
  FAILED: { label: "FAILED", tone: "border-rose-800 text-rose-300" },
  UNTRACKED: { label: "QUEUED EARLIER", tone: "border-zinc-700 text-zinc-300" },
};

/**
 * A final card the store wrote without its answer: the server stores only the status, so it has no run or no verdict or
 * reason. Such a card is linked by its message_ref and asked once. A final card that already carries its answer is not.
 */
function needsAnswer(widget: DebateSummaryWidget): boolean {
  if (!DEBATE_FINAL.includes(widget.status) || widget.message_ref == null) return false;
  if (widget.run_id == null) return true;
  if (widget.status === "VERDICT") return widget.verdict == null;
  if (widget.status === "NO_TRADE" || widget.status === "GATE_REFUSED") return widget.reason == null;
  return false;
}

/**
 * Follows a debate card to its answer. A QUEUED or RUNNING card is asked on mount, then every 10 s until its debate is
 * final (at most 30 min). A final card that needs its answer (see needsAnswer) is asked once, on mount. A live card with
 * no message_ref (queued before Wave 3) has no run to follow: it is never asked, and reads as UNTRACKED. Read-only: it
 * queues and approves nothing.
 */
function useDebateOutcome(widget: DebateSummaryWidget, sessionId: string | null): DebateOutcome {
  const [polled, setPolled] = useState<DebateOutcome | null>(null);
  const liveStatus = widget.status === "QUEUED" || widget.status === "RUNNING";
  const untracked = liveStatus && widget.message_ref == null;
  const outcome: DebateOutcome = polled ?? (untracked
    ? { status: "UNTRACKED", run_id: null, verdict: null, reason: null }
    : {
      status: widget.status,
      run_id: widget.run_id ?? null,
      verdict: widget.verdict ?? null,
      reason: widget.reason ?? null,
    });
  const live = liveStatus && !untracked;
  const lookup = !live && needsAnswer(widget);
  useEffect(() => {
    if (!(live || lookup) || !sessionId) return;
    const ctrl = new AbortController();
    const started = Date.now();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const ask = async () => {
      try {
        const out = await fetchCardDebate(sessionId, widget.id, ctrl.signal);
        if (ctrl.signal.aborted) return;
        setPolled(out);
        if (lookup || DEBATE_FINAL.includes(out.status) || out.status === "UNTRACKED") return;
      } catch {
        if (ctrl.signal.aborted || lookup) return;
      }
      if (Date.now() - started < DEBATE_FOLLOW_MS) timer = setTimeout(ask, DEBATE_POLL_MS);
    };
    void ask();
    return () => {
      ctrl.abort();
      if (timer) clearTimeout(timer);
    };
  }, [live, lookup, sessionId, widget.id]);
  return outcome;
}

/**
 * The verdict line: "Verdict: BUY 5% · conviction 62% · awaiting approval". With no approval yet (approval_status null)
 * the line says nothing about approval: the card does not know whether the run needs one.
 */
function verdictText(v: DebateVerdict | null): string {
  if (!v) return "Verdict recorded";
  const size = v.allocation_pct != null ? ` ${Number.isInteger(v.allocation_pct) ? v.allocation_pct : fmt(v.allocation_pct, 2)}%` : "";
  const conviction = v.conviction_score != null ? `conviction ${fmt(v.conviction_score * 100, 0)}%` : null;
  const approval = v.approval_status ? (APPROVAL_WORDS[v.approval_status] ?? v.approval_status) : null;
  return [`Verdict: ${v.recommendation}${size}`, conviction, approval].filter(Boolean).join(" · ");
}

/** NO_PRICE means the debate ended without a usable quote; NO_TRADE (the verdict was HOLD) is a plain no trade */
function noTradeText(reason: string | null): string {
  if (reason === "NO_PRICE") return "No trade (no usable price)";
  if (!reason || reason === "NO_TRADE") return "No trade";
  return `No trade (${reason})`;
}

function gateRefusedText(reason: string | null): string {
  if (!reason) return "Not run: the safety gate refused it";
  const known = GATE_REFUSED_REASON.find(([code]) => reason.startsWith(code));
  return `Not run: ${known ? known[1] : reason}`;
}

function outcomeLine(status: DebateCardStatus, verdict: DebateVerdict | null, reason: string | null): string {
  switch (status) {
    case "QUEUED":
      return "Debate queued…";
    case "RUNNING":
      return "Debate running…";
    case "VERDICT":
      return verdictText(verdict);
    case "NO_TRADE":
      return noTradeText(reason);
    case "GATE_REFUSED":
      return gateRefusedText(reason);
    case "FAILED":
      return "Debate failed";
    case "UNTRACKED":
      return "Debate queued earlier — see the Debates page";
  }
}

function DebateSummaryCard({ widget }: { widget: DebateSummaryWidget }) {
  const sessionId = useBrainStore((s) => s.sessionId);
  const { status, verdict, reason } = useDebateOutcome(widget, sessionId);
  const badge = DEBATE_BADGE[status];
  return (
    <WidgetFrame testId="brain-widget-debate" icon={<GitBranch className="h-3 w-3 text-purple-400" />} title={`Debate · ${widget.instrument_id}`}>
      <div className="flex flex-wrap items-center gap-2">
        <span className={`rounded border px-1.5 py-0.5 text-2xs font-bold ${badge.tone}`}>{badge.label}</span>
        <span className="text-zinc-500">{widget.timeframe}</span>
      </div>
      <p role="status" data-testid="debate-outcome" className="mt-1 text-zinc-200">
        {outcomeLine(status, verdict, reason)}
      </p>
      {(status === "QUEUED" || status === "FAILED") && widget.message && <p className="mt-1 text-zinc-500">{widget.message}</p>}
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

/** The dry run is read again at most this often when the operator only switches back to the window */
const PREFLIGHT_FOCUS_MS = 5_000;

/**
 * What Approve would do right now: read on mount (while the card can be approved), again by `check` when the operator
 * opens the confirm step, and on window focus (at most every 5 s). It fails quiet: an unavailable dry run shows nothing
 * and never blocks Approve, the server stays the authority.
 */
function useCardPreflight(widget: OrderCardWidget, sessionId: string | null, live: boolean) {
  const [preflight, setPreflight] = useState<CardPreflight | null>(null);
  const inflight = useRef<AbortController | null>(null);
  const lastRead = useRef(0);

  const check = useCallback(() => {
    if (!sessionId) return;
    inflight.current?.abort();
    const ctrl = new AbortController();
    inflight.current = ctrl;
    lastRead.current = Date.now();
    fetchCardPreflight(sessionId, widget.id, ctrl.signal)
      .then((p) => {
        // Decided elsewhere: the local card may be stale, so there is no refusal to show and the normal label stays
        const decided = !p.ok && p.code === "NOT_PENDING";
        if (!ctrl.signal.aborted) setPreflight(decided ? null : p);
      })
      .catch(() => {
        if (!ctrl.signal.aborted) setPreflight(null);
      });
  }, [sessionId, widget.id]);

  useEffect(() => {
    if (!live) return;
    // A read from before the card was locked (the feed gate) is stale: it goes at once, and the read below replaces it
    setPreflight(null);
    check();
    const onFocus = () => {
      if (Date.now() - lastRead.current >= PREFLIGHT_FOCUS_MS) check();
    };
    window.addEventListener("focus", onFocus);
    return () => {
      window.removeEventListener("focus", onFocus);
      inflight.current?.abort();
    };
  }, [live, check]);

  return { preflight: live ? preflight : null, check };
}

/** The dry run in plain words, under the size row: the sizing, the sell, the mandate Approve would create, or the refusal */
function PreflightLine({ preflight, instrumentId }: { preflight: CardPreflight; instrumentId: string }) {
  const symbol = instrumentId.split(".")[0];
  if (!preflight.ok) {
    if (preflight.needs_mandate) {
      return (
        <p role="note" data-testid="brain-card-preflight" className="mt-2 text-zinc-300">
          No ACTIVE mandate allows {symbol} yet: approving creates one (TOTP)
        </p>
      );
    }
    return (
      <p role="note" data-testid="brain-card-preflight" className="mt-2 flex items-start gap-1 text-amber-300">
        <AlertTriangle className="mt-px h-3 w-3 flex-none" aria-hidden />
        <span>Would be refused: {preflight.detail}</span>
      </p>
    );
  }
  if (preflight.side === "SELL") {
    return (
      <p role="note" data-testid="brain-card-preflight" className="mt-2 text-zinc-300">
        Would sell {preflight.sell_quantity ?? "the whole holding"} from {preflight.mandate.name}
      </p>
    );
  }
  return (
    <p role="note" data-testid="brain-card-preflight" className="mt-2 text-zinc-300">
      Would size to {preflight.allocation_pct}% of {preflight.mandate.name} (${fmt(Number(preflight.budget_usd), 0)})
      {preflight.clipped_from_pct != null && <> (clipped from {preflight.clipped_from_pct}% — room left)</>}
    </p>
  );
}

/** Alpha as a signed percent in plain words ("+1.2%", "−0.8%"; a true minus sign), with its colour */
function alphaLabel(alpha: number): { text: string; className: string } {
  const rounded = Number(alpha.toFixed(1));
  if (rounded < 0) return { text: `−${fmt(-rounded, 1)}%`, className: "text-rose-400" };
  return { text: `+${fmt(rounded, 1)}%`, className: rounded > 0 ? "text-emerald-400" : "text-zinc-400" };
}

/** The lesson's calendar date (YYYY-MM-DD) from the server timestamp; "—" when it is not an ISO date */
function lessonDate(createdAt: string): string {
  return /^\d{4}-\d{2}-\d{2}/.exec(createdAt)?.[0] ?? "—";
}

/**
 * Past lessons the learner wrote on earlier decisions for this instrument, collapsed under the rationale. The lesson text
 * was written by a model: it is shown as plain text in quotes (React escapes it), never as HTML.
 */
function PastLessons({ lessons }: { lessons: CardLesson[] }) {
  const [open, setOpen] = useState(false);
  const listId = useId();
  if (lessons.length === 0) return null;
  return (
    <div className="mt-2" data-testid="brain-card-lessons">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={listId}
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1 text-2xs text-cyan-400 hover:text-cyan-300 hover:underline"
      >
        {open ? <ChevronUp className="h-3 w-3" aria-hidden /> : <ChevronDown className="h-3 w-3" aria-hidden />}
        Past lessons ({lessons.length})
      </button>
      <ul id={listId} hidden={!open} className="mt-1 space-y-2 border-l border-zinc-800 pl-2">
        {lessons.map((lesson, i) => {
          const alpha = lesson.alpha_pct != null && Number.isFinite(lesson.alpha_pct) ? alphaLabel(lesson.alpha_pct) : null;
          const outcomeClass =
            lesson.outcome === "WIN"
              ? "border-emerald-800 text-emerald-300"
              : lesson.outcome === "LOSS"
                ? "border-rose-800 text-rose-300"
                : "border-zinc-700 text-zinc-300";
          return (
            <li key={i} className="text-zinc-400">
              <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-2xs">
                <span className={`rounded border px-1 font-bold ${outcomeClass}`}>{lesson.outcome ?? "—"}</span>
                {alpha && <span className={alpha.className}>{alpha.text}</span>}
                <span className="text-zinc-500">{lessonDate(lesson.created_at)}</span>
              </div>
              <p className="mt-0.5 text-zinc-300">{`“${lesson.lesson_text}”`}</p>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

/** One evidence line in plain form: "field = value · source_tool · as_of" (the as_of part is dropped when absent) */
function evidenceText(item: CardEvidence): string {
  return [`${item.field} = ${item.value}`, item.source_tool, item.as_of].filter(Boolean).join(" · ");
}

/**
 * The structured rationale under the card's text: thesis, evidence, invalidation, and a horizon and confidence line.
 * Each part is hidden when absent, so a card from before them shows nothing extra. The model wrote every field: they
 * are plain text (React escapes them), never markup.
 */
function StructuredRationale({ widget }: { widget: OrderCardWidget }) {
  const evidence = widget.evidence ?? [];
  const confidence = typeof widget.confidence === "number" && Number.isFinite(widget.confidence) ? widget.confidence : null;
  const meta = [
    widget.horizon ? `Horizon ${widget.horizon}` : null,
    confidence !== null ? `Confidence ${Math.round(confidence * 100)}%` : null,
  ].filter((part): part is string => part !== null);
  if (!widget.thesis && evidence.length === 0 && !widget.invalidation && meta.length === 0) return null;
  return (
    <div className="mt-2 space-y-1.5 text-2xs" data-testid="brain-card-structured">
      {widget.thesis ? (
        <div>
          <p className="text-zinc-500">Thesis</p>
          <p className="text-zinc-300">{widget.thesis}</p>
        </div>
      ) : null}
      {evidence.length > 0 ? (
        <div>
          <p className="text-zinc-500">Evidence</p>
          <ul className="space-y-0.5">
            {evidence.map((item, i) => (
              <li key={i} className="text-zinc-300">
                {evidenceText(item)}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {widget.invalidation ? (
        <div>
          <p className="text-zinc-500">Invalidation</p>
          <p className="text-zinc-300">{widget.invalidation}</p>
        </div>
      ) : null}
      {meta.length > 0 ? <p className="text-zinc-400">{meta.join(" · ")}</p> : null}
    </div>
  );
}

function OrderCard({ widget }: { widget: OrderCardWidget }) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // An entry on an instrument no ACTIVE mandate allows: the card creates the mandate inline (TOTP, checked by the API)
  const [needsMandate, setNeedsMandate] = useState(false);
  // A paused or halted mandate already covers the instrument: the server's reason, and resuming it is the fix (no TOTP form)
  const [mandateBlock, setMandateBlock] = useState<string | null>(null);
  const [mandateName, setMandateName] = useState(`Brain ${widget.instrument_id}`);
  const [mandateBudget, setMandateBudget] = useState("10000");
  const [totp, setTotp] = useState("");
  const pending = widget.status === "PENDING_APPROVAL";
  const buy = widget.side === "BUY";
  // An instrument the live feed does not allow cannot be approved: no mandate form, no TOTP, Approve disabled until it streams
  const [fed, setFed] = useState(false);
  const unlockFeed = useCallback(() => setFed(true), []);
  // Only a BUY adds risk; a SELL closes a holding and is never blocked by the feed
  const feedRefusal = pending && widget.side === "BUY" ? widget.feed_refusal ?? null : null;
  const feedLocked = !!feedRefusal && !fed;
  const sessionId = useBrainStore((s) => s.sessionId);
  useCardOutcome(widget, sessionId);
  // Only a card that can still be approved is checked: not decided, and not locked by the feed
  const { preflight, check: checkPreflight } = useCardPreflight(widget, sessionId, pending && !feedLocked);
  // Approve stays enabled when the dry run says it would be refused (the server decides); the label says so
  const refused = preflight !== null && !preflight.ok && !preflight.needs_mandate;
  // A dry run that finds no ACTIVE mandate for a BUY readies the create form, so the first Approve already shows it
  useEffect(() => {
    if (buy && preflight) setNeedsMandate(!preflight.ok && preflight.needs_mandate);
  }, [buy, preflight]);

  const approve = async () => {
    setBusy(true);
    setError(null);
    setMandateBlock(null);
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
      // The server refused: the dry run is read again so an "ok" line never sits next to that refusal
      checkPreflight();
      const msg: string = e?.message || "Order rejected";
      if (buy && msg.startsWith("NO_MANDATE")) {
        setNeedsMandate(true);
      } else if (msg.startsWith("MANDATE_PAUSED") || msg.startsWith("MANDATE_HALTED")) {
        // Nothing was queued. Resuming the mandate is the fix, so no mandate form; Approve stays for a retry after resuming
        setNeedsMandate(false);
        setConfirming(false);
        setMandateBlock(msg);
      } else {
        setError(msg);
      }
    } finally {
      // A used code is never re-sent: the TOTP box is emptied after every submit, success or failure
      setTotp("");
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
      {/* The paused-mandate notice already says this refusal: the dry-run line does not repeat it */}
      {preflight && !(mandateBlock && !preflight.ok && /^MANDATE_(PAUSED|HALTED)/.test(preflight.code)) && (
        <PreflightLine preflight={preflight} instrumentId={widget.instrument_id} />
      )}
      {widget.rationale && <p className="mt-2 text-zinc-400">{widget.rationale}</p>}
      <StructuredRationale widget={widget} />
      <PastLessons lessons={widget.lessons ?? []} />

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
            onClick={() => {
              setConfirming(true);
              checkPreflight();
            }}
            className="flex-1 rounded border border-amber-600 bg-amber-600/20 px-2 py-1.5 text-2xs font-bold text-amber-200 hover:bg-amber-600/30 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {refused ? "Approve anyway…" : "Approve…"}
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
      {pending && mandateBlock && (
        <p role="alert" className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-amber-300">
          <AlertTriangle className="h-3 w-3" aria-hidden /> Order not queued: {mandateBlock}
          <a
            href={`/${buildQuery("mission", "overview")}`}
            onClick={(e) => {
              // Modified clicks keep the browser's behaviour (new tab, new window, download)
              if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
              e.preventDefault();
              dispatchSwitchWorkspace({ mode: "mission", view: "overview" });
            }}
            className="font-bold text-cyan-400 underline hover:text-cyan-300"
          >
            Open Mission Control
          </a>
        </p>
      )}
      {(widget.status === "FAILED" || error) && (
        <p role="alert" className="mt-2 flex items-center gap-1 text-rose-400">
          <XCircle className="h-3 w-3" />{" "}
          {/* A reused code says what to do next (wait for the next code), so it is shown exactly as the API wrote it */}
          {error?.startsWith("TOTP_REUSED") ? error : `Order not queued${error ? `: ${error}` : ""}`}
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
