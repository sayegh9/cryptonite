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
import { approveOrderCard, fetchCardDebate, fetchCardOutcome, fetchCardPreflight, rejectOrderCard, sendBrainMessage } from "@/lib/brain/client";
import { useBrainStore } from "@/lib/stores/useBrainStore";
import { dispatchBrainOpenTrace } from "@/lib/brain/events";
import { createMandate, editMandate, pauseMandate, resumeMandate } from "@/lib/mission/client";
import { buildQuery, dispatchSwitchWorkspace } from "@/lib/navigation";
import { HIGH_LEVERAGE_CONFIRM, usd } from "@/lib/leverage";
import { PerpPlanPanel } from "@/components/brain/PerpPlanPanel";
import { isPerpInstrument } from "@/lib/brain/types";
import type {
  BrainWidget,
  CardEvidence,
  CardLesson,
  CardPreflight,
  CardRemedy,
  DebateCardStatus,
  DebateOutcome,
  DebateSummaryWidget,
  DebateVerdict,
  OrderCardWidget,
  PriceChartWidget,
  SwingCandidateItem,
  SwingRadarWidget,
  TearsheetWidget,
} from "@/lib/brain/types";

const fmt = (n: number, digits = 2) => n.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
const fmtPrice = (n: number) =>
  Math.abs(n) > 0 && Math.abs(n) < 0.1
    ? n.toLocaleString("en-US", { minimumFractionDigits: 4, maximumFractionDigits: 6 })
    : Math.abs(n) > 0 && Math.abs(n) < 1
      ? fmt(n, 4)
      : Math.abs(n) < 10
        ? fmt(n, 3)
        : fmt(n, 2);
const QUOTE_SUFFIXES = new Set(["USDT", "USDC", "FDUSD", "BUSD", "TUSD", "DAI"]);
function stripVenuePreserveQuote(raw: string): string {
  const parts = raw.trim().toUpperCase().split(".");
  if (parts.length >= 2 && QUOTE_SUFFIXES.has(parts[1]) && ![...QUOTE_SUFFIXES].some((q) => parts[0].endsWith(q))) {
    return `${parts[0]}${parts[1]}`;
  }
  return parts[0];
}
/** The hard ceiling of a mandate's position limit (MAX_POSITION_PCT_CAP on the server; the API refuses more) */
const MAX_POSITION_PCT = 25;

/** What a wider mandate means at its autonomy level: at L1 every order still waits for the operator; above, it does not */
function levelNote(level: number): string {
  if (level <= 1) return "At L1 Copilot every order still needs your approval.";
  if (level === 2) return "At L2 Supervised the AI can use the extra room after a veto window, without asking first.";
  return `At L${level} Autonomous the AI can use the extra room without asking.`;
}

/** BTCUSDT / btcusdt.binance -> BTCUSDT.BINANCE, as the API stores a mandate's instruments */
function canonicalInstrument(raw: string): string {
  const s = raw.trim().toUpperCase();
  return s.includes(".") ? s : `${s}.BINANCE`;
}

type CardFix = "create" | "add" | "edit" | "resume" | "choose" | "allow" | "raise";

function fixLabel(fix: CardFix | null, field?: string): string {
  if (fix === "create") return "Create mandate and confirm";
  if (fix === "add") return "Add to mandate and confirm";
  if (fix === "edit") return field === "capital_budget_usd" ? "Raise budget and confirm" : "Raise limit and confirm";
  if (fix === "resume") return "Resume mandate and confirm";
  if (fix === "allow") return "Allow perps";
  if (fix === "raise") return "Raise max leverage";
  return "Confirm paper order";
}

/** A perp fix spends the TOTP code on the mandate change alone (approve then takes the next code) */
function perpFixLabel(fix: CardFix, field?: string): string {
  if (fix === "create") return "Create mandate";
  if (fix === "add") return "Add to mandate";
  if (fix === "edit") return field === "capital_budget_usd" ? "Raise budget" : "Raise limit";
  if (fix === "resume") return "Resume mandate";
  return fixLabel(fix, field);
}

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
    const symbol = stripVenuePreserveQuote(widget.symbol);
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

  const symbol = stripVenuePreserveQuote(widget.symbol);
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
  let lo = 0;
  let hi = 0;
  let span = 1;

  const levels = [widget.entry, widget.stop, widget.target].filter(
    (v): v is number => typeof v === "number" && Number.isFinite(v)
  );

  if (closes && closes.length > 1) {
    const barLo = Math.min(...closes);
    const barHi = Math.max(...closes);
    const mid = (barLo + barHi) / 2 || 1;
    const nearbyLevels = levels.filter((v) => Math.abs(v - mid) / mid <= 0.08);
    const allVals = [...closes, ...nearbyLevels];
    lo = Math.min(...allVals);
    hi = Math.max(...allVals);
    span = hi - lo || 1;
    path = closes
      .map((c, i) => `${i === 0 ? "M" : "L"}${((i / (closes.length - 1)) * W).toFixed(1)},${(H - ((c - lo) / span) * H).toFixed(1)}`)
      .join(" ");
    change = ((closes[closes.length - 1] - closes[0]) / closes[0]) * 100;
  }
  const up = (change ?? 0) >= 0;

  const yOf = (val: number) => Math.max(2, Math.min(H - 2, H - ((val - lo) / span) * H));

  return (
    <WidgetFrame testId="brain-widget-price-chart" icon={<BarChart3 className="h-3 w-3 text-cyan-400" />} title={`${symbol} · ${widget.timeframe}`}>
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
          <svg viewBox={`0 0 ${W} ${H}`} className="h-[72px] w-full" role="img" aria-label={`${symbol} price, ${closes.length} bars`}>
            <path d={path} fill="none" strokeWidth="1.5" className={up ? "stroke-emerald-400" : "stroke-rose-400"} />
            {typeof widget.entry === "number" && Number.isFinite(widget.entry) && (
              <line x1="0" y1={yOf(widget.entry)} x2={W} y2={yOf(widget.entry)} stroke="#38bdf8" strokeDasharray="4 3" strokeWidth="1" />
            )}
            {typeof widget.stop === "number" && Number.isFinite(widget.stop) && (
              <line x1="0" y1={yOf(widget.stop)} x2={W} y2={yOf(widget.stop)} stroke="#f43f5e" strokeDasharray="3 3" strokeWidth="1" />
            )}
            {typeof widget.target === "number" && Number.isFinite(widget.target) && (
              <line x1="0" y1={yOf(widget.target)} x2={W} y2={yOf(widget.target)} stroke="#10b981" strokeDasharray="4 2" strokeWidth="1" />
            )}
          </svg>
          <div className="mt-1 flex justify-between text-2xs text-zinc-400">
            <span>
              Last <span className="text-zinc-100">{fmtPrice(closes[closes.length - 1])}</span>
            </span>
            <span className={up ? "text-emerald-400" : "text-rose-400"}>
              {up ? "+" : ""}
              {fmt(change ?? 0)}% over {closes.length} bars
            </span>
          </div>
          {levels.length > 0 && (
            <div data-testid="swing-levels-legend" className="mt-2 flex flex-wrap items-center justify-between gap-x-3 gap-y-1 border-t border-zinc-800/80 pt-1.5 text-2xs font-mono">
              {typeof widget.entry === "number" && (
                <span className="flex items-center gap-1">
                  <span className="inline-block h-1.5 w-1.5 rounded-full bg-[#38bdf8]" />
                  <span className="text-zinc-400">Entry</span>
                  <span className="text-zinc-100">{fmtPrice(widget.entry)}</span>
                </span>
              )}
              {typeof widget.stop === "number" && (
                <span className="flex items-center gap-1">
                  <span className="inline-block h-1.5 w-1.5 rounded-full bg-[#f43f5e]" />
                  <span className="text-zinc-400">Stop</span>
                  <span className="text-rose-300">{fmtPrice(widget.stop)}</span>
                  {typeof widget.entry === "number" && widget.entry > 0 && (
                    <span className="text-rose-400/80">
                      (-{((Math.abs(widget.entry - widget.stop) / widget.entry) * 100).toFixed(1)}%)
                    </span>
                  )}
                </span>
              )}
              {typeof widget.target === "number" && (
                <span className="flex items-center gap-1">
                  <span className="inline-block h-1.5 w-1.5 rounded-full bg-[#10b981]" />
                  <span className="text-zinc-400">TP1</span>
                  <span className="text-emerald-300">{fmtPrice(widget.target)}</span>
                  {typeof widget.entry === "number" && widget.entry > 0 && (
                    <span className="text-emerald-400/80">
                      (+{((Math.abs(widget.target - widget.entry) / widget.entry) * 100).toFixed(1)}%)
                    </span>
                  )}
                </span>
              )}
              {typeof widget.entry === "number" && typeof widget.stop === "number" && typeof widget.target === "number" && Math.abs(widget.entry - widget.stop) > 0 && (() => {
                const rr = Math.abs(widget.target - widget.entry) / Math.abs(widget.entry - widget.stop);
                return (
                  <span className="flex items-center gap-1">
                    <span className="text-zinc-500">R:R</span>
                    <span className={`font-bold ${rr >= 1.8 ? "text-emerald-400" : rr >= 1.2 ? "text-amber-400" : "text-rose-400"}`}>
                      {rr.toFixed(2)}R
                    </span>
                  </span>
                );
              })()}
            </div>
          )}
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
function useCardPreflight(widget: OrderCardWidget, sessionId: string | null, live: boolean, leverage: number | null = null) {
  const [preflight, setPreflight] = useState<CardPreflight | null>(null);
  const inflight = useRef<AbortController | null>(null);
  const lastRead = useRef(0);

  const check = useCallback(() => {
    if (!sessionId) return;
    inflight.current?.abort();
    const ctrl = new AbortController();
    inflight.current = ctrl;
    lastRead.current = Date.now();
    fetchCardPreflight(sessionId, widget.id, ctrl.signal, leverage)
      .then((p) => {
        // Decided elsewhere: the local card may be stale, so there is no refusal to show and the normal label stays
        const decided = !p.ok && p.code === "NOT_PENDING";
        if (!ctrl.signal.aborted) setPreflight(decided ? null : p);
      })
      .catch(() => {
        if (!ctrl.signal.aborted) setPreflight(null);
      });
  }, [sessionId, widget.id, leverage]);

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

/** The fix the card offers, in a few words, for the dry-run line */
function remedyWords(remedy: CardRemedy): string {
  if (remedy.kind === "EDIT_MANDATE") {
    return remedy.field === "max_position_pct"
      ? `to raise the position limit ${remedy.from}% → ${remedy.to}%`
      : `to raise the budget $${fmt(remedy.from, 0)} → $${fmt(remedy.to, 0)}`;
  }
  if (remedy.kind === "RESUME_MANDATE") return `to resume ${remedy.mandate_name}`;
  if (remedy.kind === "CHOOSE_MANDATE") return "a choice of mandate";
  if (remedy.kind === "ALLOW_PERPS") return `to allow perps in ${remedy.mandate_name} (at ${remedy.max_leverage_after}x)`;
  return "a mandate for it";
}

/** The dry run in plain words, under the size row: the sizing, the sell, the mandate Approve would create, or the refusal */
function PreflightLine({ preflight, instrumentId }: { preflight: CardPreflight; instrumentId: string }) {
  const symbol = instrumentId.split(".")[0];
  if (!preflight.ok) {
    if (preflight.needs_mandate) {
      return (
        <p role="note" data-testid="brain-card-preflight" className="mt-2 text-zinc-300">
          No ACTIVE mandate allows {symbol} yet: approving creates one
          {preflight.remedy?.kind === "CREATE_MANDATE" && preflight.remedy.add_to.length > 0 ? " or adds it to an existing one" : ""} (TOTP)
        </p>
      );
    }
    return (
      <p role="note" data-testid="brain-card-preflight" className="mt-2 flex items-start gap-1 text-amber-300">
        <AlertTriangle className="mt-px h-3 w-3 flex-none" aria-hidden />
        <span>
          Would be refused: {preflight.detail}
          {preflight.remedy && (
            <span className="text-zinc-300">
              {" "}· Fix and approve offers {remedyWords(preflight.remedy)}{preflight.remedy.kind === "CHOOSE_MANDATE" ? "" : " (TOTP)"}
            </span>
          )}
        </span>
      </p>
    );
  }
  if (preflight.leverage_plan) {
    const lp = preflight.leverage_plan;
    return (
      <p role="note" data-testid="brain-card-preflight" className="mt-2 text-zinc-300">
        Would open {lp.side === "BUY" ? "a long" : "a short"} at {lp.leverage}x ISOLATED in {preflight.mandate.name}: {usd(lp.margin_usd)} margin
        {preflight.clipped_from_pct != null && <> (cut from {preflight.clipped_from_pct}% of the budget)</>}
        {preflight.leverage_remedy && (
          <span className="text-amber-300">
            {" "}· {preflight.leverage_remedy.mandate_name} allows at most {preflight.leverage_remedy.from}x
          </span>
        )}
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
  // A mandate made for one instrument spends its budget on it alone, so its limit starts at the cap (the server's
  // default), not at a share that would leave most of the budget unusable; the budget is the dollar control
  const [mandatePct, setMandatePct] = useState(String(MAX_POSITION_PCT));
  // "new" creates a mandate; a mandate id adds the instrument to that ACTIVE mandate instead
  const [mandateTarget, setMandateTarget] = useState("new");
  // The edited value of an EDIT_MANDATE fix (the position limit or the budget), prefilled with the server's
  const [editValue, setEditValue] = useState("");
  // What the fix changed, kept in view: if approve is refused after it, the change is not hidden
  const [fixed, setFixed] = useState<string | null>(null);
  // AMBIGUOUS_MANDATE: the mandate chosen to trade this order, and whether the others are taken off the instrument
  const [chosenId, setChosenId] = useState("");
  const [tidyOthers, setTidyOthers] = useState(false);
  const [totp, setTotp] = useState("");
  // A perp card: the leverage pill the operator picked (null: the card's own), the 8x second confirmation, and
  // whether the confirm step also raises the mandate's maximum leverage (RAISE_MAX_LEVERAGE)
  const [pickedLeverage, setPickedLeverage] = useState<number | null>(null);
  const [confirmHigh, setConfirmHigh] = useState(false);
  const [raiseLeverage, setRaiseLeverage] = useState(false);
  const pending = widget.status === "PENDING_APPROVAL";
  const buy = widget.side === "BUY";
  const perp = isPerpInstrument(widget.instrument_id);
  // A perp card adds risk either way (a SELL opens or adds to a short) unless it reduces a held position
  const perpEntry = perp && !widget.reduce_pct;
  const addsRisk = buy || perpEntry;
  // An instrument the live feed does not allow cannot be approved: no mandate form, no TOTP, Approve disabled until it streams
  const [fed, setFed] = useState(false);
  const unlockFeed = useCallback(() => setFed(true), []);
  // Only an entry adds risk; a spot SELL closes a holding and is never blocked by the feed
  const feedRefusal = pending && addsRisk ? widget.feed_refusal ?? null : null;
  const feedLocked = !!feedRefusal && !fed;
  const sessionId = useBrainStore((s) => s.sessionId);
  useCardOutcome(widget, sessionId);
  // Only a card that can still be approved is checked: not decided, and not locked by the feed
  const { preflight, check: checkPreflight } = useCardPreflight(widget, sessionId, pending && !feedLocked, pickedLeverage);
  // The mandate change the dry run says would let this entry queue (create, add to, raise, resume, allow perps), or none
  const remedy = addsRisk && preflight && !preflight.ok ? preflight.remedy ?? null : null;
  const createRemedy = remedy?.kind === "CREATE_MANDATE" ? remedy : null;
  const editRemedy = remedy?.kind === "EDIT_MANDATE" ? remedy : null;
  const resumeRemedy = remedy?.kind === "RESUME_MANDATE" ? remedy : null;
  const chooseRemedy = remedy?.kind === "CHOOSE_MANDATE" ? remedy : null;
  const allowRemedy = remedy?.kind === "ALLOW_PERPS" ? remedy : null;
  // A perp entry's plan (also on a refusal, to show which ceiling refused it) and the optional leverage raise
  const leveragePlan = perpEntry && preflight ? preflight.leverage_plan ?? null : null;
  const leverageRemedy = perpEntry && preflight?.ok ? preflight.leverage_remedy ?? null : null;
  const highLeverage = (leveragePlan?.leverage ?? 0) >= HIGH_LEVERAGE_CONFIRM;
  // The first mandate that can take the whole order, else the first that can take any of it
  const firstChoice = chooseRemedy
    ? (chooseRemedy.candidates.find((c) => !c.blocked && c.allocation_pct >= widget.allocation_pct)
      ?? chooseRemedy.candidates.find((c) => !c.blocked))?.id ?? ""
    : "";
  useEffect(() => setChosenId(firstChoice), [firstChoice]);
  const chosen = chooseRemedy?.candidates.find((c) => c.id === chosenId && !c.blocked) ?? null;
  const others = chooseRemedy?.candidates.filter((c) => c.id !== chosen?.id && c.tidy) ?? [];
  // Approve stays enabled when the dry run says it would be refused (the server decides); the label says so
  const refused = preflight !== null && !preflight.ok && !preflight.needs_mandate && !remedy;
  // A dry run that finds no ACTIVE mandate for a BUY readies the create form, so the first Approve already shows it
  useEffect(() => {
    if (addsRisk && preflight) setNeedsMandate(!preflight.ok && preflight.needs_mandate);
  }, [addsRisk, preflight]);
  // A new leverage is a new decision: its second confirmation is asked again
  useEffect(() => setConfirmHigh(false), [leveragePlan?.leverage]);
  // A perp cannot get a new (spot-only) mandate: the first mandate it can join is preselected
  const firstTarget = createRemedy && !createRemedy.can_create ? createRemedy.add_to[0]?.id ?? "new" : "new";
  useEffect(() => setMandateTarget(firstTarget), [firstTarget]);
  // The server's suggestion fills the field again only when the suggestion itself changes, not on every re-read
  const editKey = editRemedy ? `${editRemedy.mandate_id}:${editRemedy.field}:${editRemedy.to}` : "";
  useEffect(() => {
    if (editRemedy) setEditValue(String(editRemedy.to));
  }, [editKey]); // keyed on the suggestion, not on the object, which every re-read replaces
  const addTo = createRemedy?.add_to.find((m) => m.id === mandateTarget) ?? null;
  const canCreate = createRemedy?.can_create ?? true; // without a remedy (an older read), the create form as before
  const fix = needsMandate
    ? (addTo ? "add" : canCreate ? "create" : null)
    : editRemedy ? "edit" : resumeRemedy ? "resume" : chooseRemedy ? "choose" : allowRemedy ? "allow"
    : leverageRemedy && raiseLeverage ? "raise" : null;
  // Choosing a mandate changes none (and tidying the others only takes room away): only the other fixes need TOTP.
  // A perp entry needs it to approve too (ADR 0009 D3); a code works once, so a perp fix is its own step: the mandate
  // change takes this code, and approve takes the next one
  const needsTotp = (fix !== null && fix !== "choose") || perpEntry;
  const perpFixStep = perpEntry && fix !== null && fix !== "choose";
  const symbol = widget.instrument_id.split(".")[0];

  const fixReady = (() => {
    if (fix === "choose") return chosen !== null;
    if (totp.length !== 6) return false;
    if (fix === "create") {
      const pct = Number(mandatePct);
      return !!mandateName.trim() && Number(mandateBudget) > 0 && pct >= 1 && pct <= MAX_POSITION_PCT;
    }
    if (fix === "edit" && editRemedy) {
      const v = Number(editValue);
      return v > editRemedy.from && (editRemedy.field !== "max_position_pct" || v <= MAX_POSITION_PCT);
    }
    return fix === "add" || fix === "resume" || fix === "allow" || fix === "raise";
  })();
  // A perp approval without a fix: the code, a plan the server accepts, and the second confirmation at 8x and above
  const perpReady = totp.length === 6 && (!highLeverage || confirmHigh);

  const approve = async () => {
    setBusy(true);
    setError(null);
    setMandateBlock(null);
    // Every mandate change from here gives the AI room, so each needs the current TOTP code (the Mandate editor's
    // rule, checked by the API with its caps), and its audit event names this card
    const source = sessionId ? `brain_card:${sessionId}:${widget.id}` : undefined;
    try {
      let mandateId: string | undefined;
      if (fix === "create") {
        // The API also refuses an instrument a paused or halted mandate holds (MANDATE_PAUSED / MANDATE_HALTED)
        mandateId = (await createMandate({
          name: mandateName.trim(),
          capital_budget_usd: mandateBudget,
          instruments: [widget.instrument_id],
          max_position_pct: Number(mandatePct),
          max_drawdown_pct: 20,
          veto_window_s: 600,
          // A perp needs a mandate that allows perps: it starts at L1 with the 3x default (ADR 0009)
          allow_perps: createRemedy?.allow_perps ?? false,
          totp_code: totp,
          source,
        })).id;
        setNeedsMandate(false);
        setFixed(`Created mandate ${mandateName.trim()} ($${fmt(Number(mandateBudget), 0)}, ${mandatePct}% limit in ${symbol}).`);
      } else if (fix === "add" && addTo) {
        mandateId = (await editMandate(addTo.id, { instruments: [...addTo.instruments, widget.instrument_id], totp_code: totp, source })).id;
        setNeedsMandate(false);
        setFixed(`Added ${symbol} to mandate ${addTo.name}.`);
      } else if (fix === "edit" && editRemedy) {
        mandateId = (await editMandate(editRemedy.mandate_id, { [editRemedy.field]: Number(editValue), totp_code: totp, source })).id;
        setFixed(
          editRemedy.field === "max_position_pct"
            ? `Mandate ${editRemedy.mandate_name}: position limit raised ${editRemedy.from}% → ${editValue}%.`
            : `Mandate ${editRemedy.mandate_name}: budget raised $${fmt(editRemedy.from, 0)} → $${fmt(Number(editValue), 0)}.`,
        );
      } else if (fix === "choose" && chosen) {
        // The others first, so the instrument has one ACTIVE owner again; a refusal here stops before any order is sent
        const done: string[] = [];
        if (tidyOthers) {
          for (const c of others) {
            if (c.tidy === "PAUSE") {
              await pauseMandate(c.id, `paused from Brain order card ${widget.id}: ${chosen.name} trades ${symbol}`);
              done.push(`paused ${c.name}`);
            } else {
              const instrument = canonicalInstrument(widget.instrument_id);
              await editMandate(c.id, { instruments: c.instruments.filter((i) => i !== instrument), source });
              done.push(`removed ${symbol} from ${c.name}`);
            }
          }
        }
        mandateId = chosen.id;
        setFixed(`Trading in mandate ${chosen.name}${done.length ? `; ${done.join(", ")}` : ""}.`);
      } else if (fix === "resume" && resumeRemedy) {
        mandateId = (await resumeMandate(resumeRemedy.mandate_id, totp, `resumed from Brain order card ${widget.id}`)).id;
        setFixed(`Resumed mandate ${resumeRemedy.mandate_name}.`);
      } else if (fix === "allow" && allowRemedy) {
        mandateId = (await editMandate(allowRemedy.mandate_id, { allow_perps: true, totp_code: totp, source })).id;
        setFixed(`Mandate ${allowRemedy.mandate_name}: perps allowed (at most ${allowRemedy.max_leverage_after}x, ISOLATED).`);
      } else if (fix === "raise" && leverageRemedy) {
        mandateId = (await editMandate(leverageRemedy.mandate_id, { max_leverage: leverageRemedy.to, totp_code: totp, source })).id;
        setRaiseLeverage(false);
        setFixed(`Mandate ${leverageRemedy.mandate_name}: max leverage raised ${leverageRemedy.from}x → ${leverageRemedy.to}x.`);
      }
      if (perpFixStep) {
        // The code is spent on the mandate change: the dry run is read again and approve waits for the next code
        checkPreflight();
        setFixed((f) => `${f ?? "Mandate changed."} Enter the next TOTP code to approve the order.`);
        return;
      }
      await approveOrderCard(
        widget,
        mandateId,
        perpEntry ? { totp_code: totp, leverage: pickedLeverage, confirm_high_leverage: highLeverage && confirmHigh } : undefined,
      );
      setConfirming(false);
    } catch (e: any) {
      // The server refused: the dry run is read again so an "ok" line never sits next to that refusal
      checkPreflight();
      const msg: string = e?.message || "Order rejected";
      if (addsRisk && msg.startsWith("NO_MANDATE")) {
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
      aria-label={perpEntry
        ? `Order proposal: ${buy ? "LONG" : "SHORT"} ${widget.instrument_id}, ${widget.allocation_pct}% of mandate as margin`
        : buy
          ? `Order proposal: BUY ${widget.allocation_pct}% of mandate ${widget.instrument_id}`
          : `Order proposal: SELL the whole holding of ${widget.instrument_id}`}
      className="mt-2 rounded-lg border border-amber-700/60 bg-amber-950/10 p-3 font-mono text-2xs text-zinc-300"
    >
      <header className="mb-2 flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5">
          <AlertTriangle className="h-3 w-3 text-amber-400" aria-hidden />
          <span className="rounded border border-amber-600 bg-amber-500/10 px-1.5 py-0.5 text-2xs font-black tracking-wider text-amber-300">
            {addsRisk ? "ADDS RISK · PAPER" : perp ? "REDUCES POSITION · PAPER" : "CLOSES HOLDING · PAPER"}
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
        <span className={`text-sm font-black ${buy ? "text-emerald-400" : "text-rose-400"}`}>
          {perpEntry ? (buy ? "LONG" : "SHORT") : widget.side}
        </span>
        {!buy && widget.reduce_pct && widget.reduce_pct < 100 ? (
          <span
            data-testid="order-card-reduce-pct"
            className="rounded border border-amber-600/80 bg-amber-500/20 px-1.5 py-0.5 text-2xs font-black tracking-wider text-amber-300"
          >
            SCALE-OUT {widget.reduce_pct}%
          </span>
        ) : null}
        <span className="text-sm text-zinc-100">{widget.instrument_id}</span>
        <span className="text-zinc-400">
          {widget.order_type}
          {widget.order_type === "LIMIT" && widget.limit_price ? ` @ ${fmt(widget.limit_price)}` : ""}
        </span>
      </div>
      <dl className="mt-1 grid grid-cols-3 gap-2 text-2xs">
        <div>
          <dt className="text-zinc-500">{perpEntry ? "Margin" : "Size"}</dt>
          {perpEntry ? (
            <dd className="text-zinc-100" title="The margin posted, a share of the mandate budget; the leverage sets the notional">
              {widget.allocation_pct}% of mandate
            </dd>
          ) : buy ? (
            <dd className="text-zinc-100" title="Sized at approval from the live price; capped at the room left under the mandate's position limit">
              {widget.allocation_pct}% of mandate
            </dd>
          ) : widget.reduce_pct && widget.reduce_pct < 100 ? (
            <dd className="text-zinc-100" title={`A partial spot sell closes ${widget.reduce_pct}% of the mandate's holding of this instrument`}>
              Scale out {widget.reduce_pct}% of holding
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
      {widget.stop_loss != null && widget.take_profit != null && (() => {
        const refPrice = widget.limit_price ?? null;
        const riskDist = refPrice != null ? Math.abs(refPrice - widget.stop_loss) : null;
        const rewardDist = refPrice != null ? Math.abs(widget.take_profit - refPrice) : null;
        const rrRatio = riskDist != null && rewardDist != null && riskDist > 0 ? rewardDist / riskDist : null;
        const totalDist = (riskDist ?? 1) + (rewardDist ?? 2);
        const riskPct = riskDist != null && rewardDist != null && totalDist > 0 ? (riskDist / totalDist) * 100 : 33.3;
        const rewardPct = 100 - riskPct;
        return (
          <div className="mt-2 rounded border border-zinc-800 bg-zinc-950/70 p-2 font-mono text-2xs" data-testid="order-card-rr-bar">
            <div className="flex items-center justify-between mb-1.5">
              <span className="text-rose-400 font-semibold">
                Stop: {fmt(widget.stop_loss)}{refPrice ? ` (-${((Math.abs(refPrice - widget.stop_loss) / refPrice) * 100).toFixed(1)}%)` : ""}
              </span>
              {rrRatio != null ? (
                <span className={`px-1.5 py-0.5 rounded text-2xs font-bold border ${
                  rrRatio >= 1.8
                    ? "border-emerald-800 bg-emerald-950/60 text-emerald-300"
                    : rrRatio >= 1.2
                      ? "border-amber-800 bg-amber-950/60 text-amber-300"
                      : "border-rose-800 bg-rose-950/60 text-rose-300"
                }`}>
                  {rrRatio.toFixed(2)}R
                </span>
              ) : (
                <span className="text-zinc-500">R:R Geometry</span>
              )}
              <span className="text-emerald-400 font-semibold">
                TP1: {fmt(widget.take_profit)}{refPrice ? ` (+${((Math.abs(widget.take_profit - refPrice) / refPrice) * 100).toFixed(1)}%)` : ""}
              </span>
            </div>
            <div className="h-1.5 w-full flex rounded-full overflow-hidden bg-zinc-800">
              <div style={{ width: `${riskPct.toFixed(1)}%` }} className="bg-rose-500" title={`Risk: ${riskPct.toFixed(1)}%`} />
              <div style={{ width: `${rewardPct.toFixed(1)}%` }} className="bg-emerald-500" title={`Reward: ${rewardPct.toFixed(1)}%`} />
            </div>
          </div>
        );
      })()}
      {/* The paused-mandate notice already says this refusal: the dry-run line does not repeat it */}
      {preflight && !(mandateBlock && !preflight.ok && /^MANDATE_(PAUSED|HALTED)/.test(preflight.code)) && (
        <PreflightLine preflight={preflight} instrumentId={widget.instrument_id} />
      )}
      {perpEntry && (pending ? leveragePlan !== null : widget.leverage != null) && (
        <PerpPlanPanel
          plan={pending ? leveragePlan : null}
          grade={widget.setup_grade}
          gradeReasons={widget.grade_reasons ?? []}
          picked={pickedLeverage}
          onPick={setPickedLeverage}
          locked={!pending}
          lockedLeverage={widget.leverage}
          target={widget.take_profit}
        />
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
            {refused ? "Approve anyway…" : remedy && !needsMandate ? "Fix and approve…" : "Approve…"}
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
            <div data-testid="card-fix-create">
              {createRemedy && createRemedy.add_to.length > 0 && (
                <fieldset className="mb-2 flex flex-col gap-1 text-zinc-300">
                  <legend className="mb-1 text-zinc-400">No ACTIVE mandate allows {symbol} yet. Confirming:</legend>
                  {canCreate && (
                    <label className="flex items-center gap-1.5">
                      <input type="radio" name={`target-${widget.id}`} checked={mandateTarget === "new"} onChange={() => setMandateTarget("new")} />
                      creates a new mandate for {symbol}
                    </label>
                  )}
                  {createRemedy.add_to.map((m) => (
                    <label key={m.id} className="flex items-center gap-1.5">
                      <input type="radio" name={`target-${widget.id}`} checked={mandateTarget === m.id} onChange={() => setMandateTarget(m.id)} />
                      adds {symbol} to {m.name} (L{m.autonomy_level}, {m.room_pct}% room)
                    </label>
                  ))}
                </fieldset>
              )}
              {fix === "add" && addTo ? (
                <p className="mb-2 text-zinc-300">
                  {addTo.name} can trade {symbol} from then on, up to its {addTo.max_position_pct}% position limit of $
                  {fmt(addTo.capital_budget_usd, 0)}; then this order is queued.{" "}
                  {levelNote(addTo.autonomy_level)}
                </p>
              ) : fix === "create" ? (
                <>
                  {!(createRemedy && createRemedy.add_to.length > 0) && (
                    <p className="mb-2 text-zinc-300">
                      No ACTIVE mandate allows {widget.instrument_id} yet. Confirming creates one with the budget below, at L1 Copilot
                      (every order still needs your approval), then queues this order.
                    </p>
                  )}
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
                      Position limit (%)
                      <input
                        className={cardInput}
                        type="number"
                        min="1"
                        max={MAX_POSITION_PCT}
                        step="any"
                        value={mandatePct}
                        onChange={(e) => setMandatePct(e.target.value)}
                      />
                    </label>
                  </div>
                  <p className="mb-2 text-zinc-500">
                    Up to ${fmt((Number(mandateBudget) || 0) * (Number(mandatePct) || 0) / 100, 0)} in {symbol} ({MAX_POSITION_PCT}% at
                    most); this order uses {widget.allocation_pct}% of the budget.
                  </p>
                </>
              ) : (
                <p className="mb-2 text-zinc-300">
                  No mandate can take {symbol} from here: it needs an ACTIVE L1 mandate that allows perps (Mission Control → Mandates).
                </p>
              )}
            </div>
          ) : editRemedy ? (
            <div data-testid="card-fix-edit">
              <p className="mb-2 text-zinc-300">
                No room left in {symbol}: mandate {editRemedy.mandate_name} holds {editRemedy.held_pct}% of its $
                {fmt(editRemedy.capital_budget_usd, 0)} budget in it, at its {editRemedy.max_position_pct}% limit.{" "}
                {editRemedy.field === "max_position_pct"
                  ? <>Raising the limit to {editRemedy.to}% fits this order at {editRemedy.allocation_after_pct}%.</>
                  : <>The limit is already at its {MAX_POSITION_PCT}% cap (or the budget is full), so raising the budget to $
                    {fmt(editRemedy.to, 0)} fits this order at {editRemedy.allocation_after_pct}%.</>}
              </p>
              <label className="mb-2 flex flex-col gap-1 text-2xs text-zinc-400">
                {editRemedy.field === "max_position_pct" ? `Position limit (%), now ${editRemedy.from}%, at most ${MAX_POSITION_PCT}%` : `Budget (USD), now $${fmt(editRemedy.from, 0)}`}
                <input
                  className={cardInput}
                  type="number"
                  min={editRemedy.from}
                  max={editRemedy.field === "max_position_pct" ? MAX_POSITION_PCT : undefined}
                  step="any"
                  value={editValue}
                  onChange={(e) => setEditValue(e.target.value)}
                />
              </label>
              <p className="mb-2 flex items-start gap-1 text-amber-300">
                <AlertTriangle className="mt-px h-3 w-3 flex-none" aria-hidden />
                <span>
                  This changes the mandate for all its future AI orders, not only this one. {levelNote(editRemedy.autonomy_level)}
                  {editRemedy.field === "capital_budget_usd" && " A new budget also restarts the mandate's drawdown baseline."}
                </span>
              </p>
            </div>
          ) : chooseRemedy ? (
            <div data-testid="card-fix-choose">
              <fieldset className="mb-2 flex flex-col gap-1 text-zinc-300">
                <legend className="mb-1 text-zinc-400">
                  {chooseRemedy.candidates.length} ACTIVE mandates allow {symbol}. Trade this order in:
                </legend>
                {chooseRemedy.candidates.map((c) => (
                  <label key={c.id} className={`flex items-start gap-1.5 ${c.blocked ? "text-zinc-500" : ""}`}>
                    <input
                      type="radio"
                      className="mt-0.5"
                      name={`choose-${widget.id}`}
                      disabled={!!c.blocked}
                      checked={chosenId === c.id}
                      onChange={() => setChosenId(c.id)}
                    />
                    <span>
                      {c.name} (L{c.autonomy_level}, {c.max_position_pct}% limit of ${fmt(c.capital_budget_usd, 0)})
                      {c.blocked
                        ? ` · ${c.blocked}`
                        : ` · sized to ${c.allocation_pct}% ($${fmt(c.capital_budget_usd * c.allocation_pct / 100, 0)})${c.holds ? `, adds to its ${symbol}` : ""}`}
                    </span>
                  </label>
                ))}
              </fieldset>
              {chosen && others.length > 0 && (
                <label className="mb-2 flex items-start gap-1.5 text-zinc-300">
                  <input type="checkbox" className="mt-0.5" checked={tidyOthers} onChange={(e) => setTidyOthers(e.target.checked)} />
                  <span>
                    Also stop the overlap, so {chosen.name} is the only ACTIVE mandate on {symbol}:{" "}
                    {others.map((c) => (c.tidy === "PAUSE" ? `pause ${c.name} (${symbol} is all it covers)` : `remove ${symbol} from ${c.name}`)).join("; ")}.
                    This takes room away, so it needs no TOTP.
                  </span>
                </label>
              )}
            </div>
          ) : resumeRemedy ? (
            <p className="mb-2 text-zinc-300" data-testid="card-fix-resume">
              Mandate {resumeRemedy.mandate_name} covers {symbol} and is PAUSED. Confirming resumes it (it can take new risk again;
              its high-water mark restarts from now), then queues this order.
            </p>
          ) : allowRemedy ? (
            <div data-testid="card-fix-allow-perps">
              <p className="mb-2 text-zinc-300">
                Mandate {allowRemedy.mandate_name} does not allow perps. Confirming turns perps on in it: ISOLATED margin, at most{" "}
                {allowRemedy.max_leverage_after}x, and every order that adds risk still needs your approval with TOTP (L1 only).
              </p>
              <p className="mb-2 flex items-start gap-1 text-amber-300">
                <AlertTriangle className="mt-px h-3 w-3 flex-none" aria-hidden />
                <span>This changes the mandate for all its future AI orders. Approving this order then takes the next TOTP code.</span>
              </p>
            </div>
          ) : perpEntry ? (
            <div data-testid="card-confirm-perp">
              {leveragePlan && leveragePlan.ok ? (
                <p className="mb-2 text-zinc-300">
                  Send a PAPER {buy ? "LONG" : "SHORT"} {widget.instrument_id} {widget.order_type} at {leveragePlan.leverage}x ISOLATED:{" "}
                  {usd(leveragePlan.margin_usd)} margin, {usd(leveragePlan.notional_usd)} notional. If the stop at{" "}
                  {widget.stop_loss != null ? fmtPrice(widget.stop_loss) : "—"} fills you lose about{" "}
                  <strong className="text-rose-300" data-testid="card-confirm-loss-stop">{usd(leveragePlan.loss_at_stop_usd)}</strong>; if the
                  position is liquidated at {leveragePlan.liquidation_price ? fmtPrice(Number(leveragePlan.liquidation_price)) : "—"} you lose{" "}
                  <strong className="text-leverage-liquidation" data-testid="card-confirm-loss-liq">{usd(leveragePlan.loss_if_liquidated_usd)}</strong>, the
                  whole margin. The stop rests on the mark price; the dispatcher sizes the order again and may lower the leverage, never raise it.
                </p>
              ) : (
                <p className="mb-2 text-amber-300">The dry run has no plan for this order yet: approve asks the server, which may refuse it.</p>
              )}
              {leverageRemedy && (
                <label className="mb-2 flex items-start gap-1.5 text-zinc-300" data-testid="card-fix-raise-leverage">
                  <input type="checkbox" className="mt-0.5" checked={raiseLeverage} onChange={(e) => setRaiseLeverage(e.target.checked)} />
                  <span>
                    First raise {leverageRemedy.mandate_name}&apos;s max leverage {leverageRemedy.from}x → {leverageRemedy.to}x (TOTP; this
                    applies to all its future orders, and approving then takes the next code)
                  </span>
                </label>
              )}
              {highLeverage && !perpFixStep && (
                <label className="mb-2 flex items-start gap-1.5 text-leverage-high" data-testid="card-confirm-high-leverage">
                  <input type="checkbox" className="mt-0.5" checked={confirmHigh} onChange={(e) => setConfirmHigh(e.target.checked)} />
                  <span>
                    I confirm {leveragePlan?.leverage}x: liquidation is {leveragePlan?.liquidation_distance_pct ? `${Number(leveragePlan.liquidation_distance_pct).toFixed(1)}%` : "a short way"} away.
                  </span>
                </label>
              )}
            </div>
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
          {needsTotp && (
            <label className="mb-2 flex w-1/2 flex-col gap-1 text-2xs text-zinc-400">
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
          )}
          <div className="flex gap-2">
            <button
              type="button"
              disabled={busy || (needsMandate && !fix) || (!!fix && !fixReady) || (perpEntry && !fix && !perpReady)}
              onClick={approve}
              className="flex-1 rounded bg-amber-600 px-2 py-1.5 text-2xs font-bold text-zinc-950 hover:bg-amber-500 disabled:opacity-50"
            >
              {busy
                ? "Sending…"
                : fix === "choose" && chosen
                  ? `Confirm in ${chosen.name}`
                  : perpFixStep && fix
                    ? perpFixLabel(fix, editRemedy?.field)
                    : fixLabel(fix, editRemedy?.field)}
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
      {fixed && (
        <p role="status" data-testid="card-fix-done" className="mt-2 flex items-center gap-1 text-cyan-300">
          <CheckCircle2 className="h-3 w-3" aria-hidden /> {fixed}
        </p>
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
      {!buy && !perp && error?.startsWith("NO_MANDATE") && (
        <p className="mt-1 text-zinc-500">A spot sell can only sell what a mandate already holds; spot shorts are refused. Reject this card.</p>
      )}
    </section>
  );
}

export function SwingRadarCard({ w }: { w: SwingRadarWidget }) {
  const trendColor = (trend?: string) => {
    const t = (trend || "").toUpperCase();
    if (t.includes("BULL") || t.includes("UP")) return "border-emerald-800/80 bg-emerald-950/60 text-emerald-300";
    if (t.includes("BEAR") || t.includes("DOWN")) return "border-rose-800/80 bg-rose-950/60 text-rose-300";
    return "border-zinc-800 bg-zinc-900/60 text-zinc-400";
  };

  return (
    <section
      data-testid="swing-radar-card"
      aria-label={`Swing Radar: ${w.preset_slug} ${w.timeframe}`}
      className="mt-2 rounded-lg border border-zinc-800 bg-zinc-950/90 p-3.5 text-xs font-mono text-zinc-300"
    >
      <header className="mb-3 flex flex-wrap items-center justify-between gap-2 border-b border-zinc-800/80 pb-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="rounded border border-cyan-800 bg-cyan-950/70 px-1.5 py-0.5 text-2xs font-bold uppercase tracking-wider text-cyan-300">
            SWING RADAR
          </span>
          <span className="text-2xs font-bold text-zinc-200">{w.preset_slug}</span>
          <span className="rounded bg-zinc-800/90 px-1.5 py-0.5 text-2xs text-zinc-400">{w.timeframe}</span>
        </div>
        <span className="text-2xs text-zinc-500">
          Scanned {w.scanned_count} liquid symbols
        </span>
      </header>

      {w.candidates.length === 0 ? (
        <div className="py-3 text-center" data-testid="swing-radar-empty">
          <p className="text-zinc-400">No swing trade candidates matched the filters.</p>
          <button
            type="button"
            onClick={() => void sendBrainMessage("Inspect 15m and 1h swing setups across liquid symbols and highlight any developing structures")}
            className="mt-2 rounded border border-cyan-800/60 bg-cyan-950/40 px-2.5 py-1 text-2xs font-semibold text-cyan-300 hover:bg-cyan-900/60 transition-colors"
          >
            Inspect 15m/1h setups
          </button>
        </div>
      ) : (
        <div className="space-y-3">
          {w.candidates.map((c, idx) => {
            const isLong = c.direction === "LONG";
            const rrHigh = c.rr_ratio >= 1.8;
            return (
              <div
                key={`${c.instrument_id}-${idx}`}
                data-testid="swing-radar-candidate"
                className="rounded-md border border-zinc-800/80 bg-zinc-900/40 p-2.5 space-y-2"
              >
                {/* Symbol, Direction, Score, R:R */}
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <span className="text-xs font-bold text-zinc-100">{c.instrument_id}</span>
                    <span
                      className={`rounded px-1.5 py-0.5 text-2xs font-bold ${
                        isLong
                          ? "border border-emerald-800 bg-emerald-950/60 text-emerald-300"
                          : "border border-rose-800 bg-rose-950/60 text-rose-300"
                      }`}
                    >
                      {c.direction}
                    </span>
                    <span className="rounded border border-zinc-700 bg-zinc-800/70 px-1.5 py-0.5 text-2xs text-zinc-300">
                      Score {c.score.toFixed(0)}
                    </span>
                  </div>
                  <span
                    className={`rounded border px-1.5 py-0.5 text-2xs font-bold ${
                      rrHigh
                        ? "border-emerald-700 bg-emerald-950/60 text-emerald-300"
                        : "border-amber-700 bg-amber-950/60 text-amber-300"
                    }`}
                  >
                    {c.rr_ratio.toFixed(2)}R
                  </span>
                </div>

                {/* MTF Alignment Strip */}
                <div className="flex items-center gap-1.5 text-2xs">
                  <span className="text-zinc-500">MTF:</span>
                  <span className={`rounded border px-1.5 py-0.5 font-bold ${trendColor(c.trend_4h)}`}>
                    4H: {c.trend_4h || "FLAT"}
                  </span>
                  <span className={`rounded border px-1.5 py-0.5 font-bold ${trendColor(c.trend_1h)}`}>
                    1H: {c.trend_1h || "FLAT"}
                  </span>
                  <span className={`rounded border px-1.5 py-0.5 font-bold ${trendColor(c.trend_15m)}`}>
                    15M: {c.trend_15m || "FLAT"}
                  </span>
                </div>

                {/* Trade Geometry Row */}
                <div className="text-2xs text-zinc-300">
                  Entry <span className="font-semibold text-sky-400">{fmtPrice(c.entry)}</span> · Stop{" "}
                  <span className="font-semibold text-rose-400">{fmtPrice(c.stop)}</span> · TP1{" "}
                  <span className="font-semibold text-emerald-400">{fmtPrice(c.target)}</span>
                </div>

                {/* 1-Click Chat Action Buttons */}
                <div className="flex flex-wrap items-center gap-1.5 pt-1">
                  <button
                    type="button"
                    onClick={() =>
                      void sendBrainMessage(
                        `Show the ${c.timeframe} price chart for ${c.instrument_id.split(".")[0]} with entry ${c.entry}, stop ${c.stop}, and target ${c.target}, and summarize the setup in 1-2 sentences.`
                      )
                    }
                    className="flex items-center gap-1 rounded border border-zinc-700 bg-zinc-800/80 px-2 py-1 text-2xs text-zinc-200 hover:border-cyan-700 hover:text-cyan-300 transition-colors"
                  >
                    <BarChart3 className="h-3 w-3 text-cyan-400" />
                    <span>Chart</span>
                  </button>
                  <button
                    type="button"
                    onClick={() =>
                      void sendBrainMessage(
                        `Stage a multi-agent debate for a ${c.direction} swing trade on ${c.instrument_id} (${c.timeframe}) with entry ${c.entry}, structural stop ${c.stop}, and TP1 ${c.target}`
                      )
                    }
                    className="flex items-center gap-1 rounded border border-zinc-700 bg-zinc-800/80 px-2 py-1 text-2xs text-zinc-200 hover:border-purple-700 hover:text-purple-300 transition-colors"
                  >
                    <GitBranch className="h-3 w-3 text-purple-400" />
                    <span>Debate</span>
                  </button>
                  <button
                    type="button"
                    data-testid="swing-radar-propose-btn"
                    onClick={() =>
                      void sendBrainMessage(
                        `Propose a ${c.direction === "LONG" ? "BUY" : "SELL"} swing order for ${c.instrument_id} with stop_loss ${c.stop} and take_profit ${c.target}`
                      )
                    }
                    className="flex items-center gap-1 rounded border border-emerald-500/40 bg-emerald-500/15 px-2.5 py-1 text-2xs font-semibold text-emerald-300 hover:border-emerald-400 hover:bg-emerald-500/25 transition-colors"
                  >
                    <CheckCircle2 className="h-3 w-3 text-emerald-400" />
                    <span>🎯 Propose Order</span>
                  </button>
                </div>
              </div>
            );
          })}
        </div>
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
    case "swing_radar":
      return <SwingRadarCard w={widget} />;
    default:
      return null;
  }
}

export function BrainWidgetList({ widgets }: { widgets: BrainWidget[] }) {
  return (
    <>
      {widgets.map((w, i) => {
        switch (w.kind) {
          case "swing_radar":
            return <SwingRadarCard key={w.id ?? i} w={w} />;
          default:
            return <BrainWidgetView key={w.id ?? i} widget={w} />;
        }
      })}
    </>
  );
}
