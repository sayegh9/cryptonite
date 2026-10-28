"use client";

import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { BrainCircuit, CheckCircle2, ChevronDown, ChevronRight, Loader2, Send, Square, User as UserIcon, Wrench, ShieldAlert, Info } from "lucide-react";
import { sendBrainMessage, stopBrain } from "@/lib/brain/client";
import { dispatchBrainOpenTrace } from "@/lib/brain/events";
import type { BrainMessage } from "@/lib/brain/types";
import { useBrainStore } from "@/lib/stores/useBrainStore";
import { BrainRecentConversations } from "./BrainRecentConversations";
import { BrainWidgetView } from "./BrainWidgets";

const STARTERS = [
  "Analyze BTCUSDT on the 4h and tell me if there is a trade",
  "Explain my latest backtest",
  "What has the scanner flagged for ETHUSDT today?",
];

/** /postmortem [SYMBOL]: a trade review question. The symbol is normalised as the watchlist does (XRP -> XRPUSDT). */
const POSTMORTEM = /^\/postmortem(?:\s+([\s\S]*))?$/i;
const QUOTE_ASSET = /(USDT|BTC|FDUSD)$/;

/** The question to send for `/postmortem <arg>`, or null when the argument is not a symbol (then nothing is sent). */
function postmortemPrompt(arg: string): string | null {
  if (!arg) return "Reconstruct our most recent closed AI trade: entry, verdict, how it closed, P&L and the lesson.";
  // A venue form such as XRPUSDT.BINANCE names the same symbol
  const base = arg.trim().toUpperCase().replace(/\.[A-Z]+$/, "");
  const symbol = QUOTE_ASSET.test(base) ? base : `${base}USDT`;
  if (!/^[A-Z0-9]{2,20}$/.test(symbol)) return null;
  return `Why did we win or lose on ${symbol}? Reconstruct the last trade: entry, verdict, how it closed, P&L and the lesson.`;
}

const TOOL_LABELS: Record<string, string> = {
  get_live_market_data: "live prices",
  show_price_chart: "chart",
  get_scanner_context: "scanner",
  query_postgres_readonly: "SQL",
  query_parquet_duckdb: "Parquet",
  explain_backtest: "tearsheet",
  compare_live_vs_backtest: "live vs backtest",
  stage_debate_proposal: "stage debate",
  propose_order: "propose order",
  scan_swing_setups: "swing radar",
};

function ToolChips({ message }: { message: BrainMessage }) {
  const [expanded, setExpanded] = useState(true);
  if (message.toolCalls.length === 0) return null;
  const anyRunning = message.toolCalls.some((c) => c.status === "RUNNING");
  const stepCount = message.toolCalls.length;

  return (
    <div className="mb-2 overflow-hidden rounded-lg border border-zinc-800/80 bg-zinc-950/60">
      <button
        type="button"
        onClick={() => setExpanded((prev) => !prev)}
        aria-expanded={expanded}
        className="flex w-full items-center justify-between gap-2 px-2.5 py-1 text-2xs font-mono text-zinc-400 hover:bg-zinc-900/60 transition-colors"
      >
        <span className="flex items-center gap-1.5">
          {anyRunning ? (
            <Loader2 className="h-3 w-3 animate-spin text-cyan-400" aria-hidden="true" />
          ) : (
            <CheckCircle2 className="h-3 w-3 text-cyan-400" aria-hidden="true" />
          )}
          <span>
            {anyRunning ? "Working" : "Worked"} · {stepCount} {stepCount === 1 ? "step" : "steps"}
          </span>
        </span>
        {expanded ? (
          <ChevronDown className="h-3 w-3 text-zinc-400" aria-hidden="true" />
        ) : (
          <ChevronRight className="h-3 w-3 text-zinc-400" aria-hidden="true" />
        )}
      </button>
      {expanded && (
        <ul className="flex flex-wrap gap-1 border-t border-zinc-800/60 px-2.5 py-1.5" aria-label="Tools used">
          {message.toolCalls.map((c) => {
            const held = c.status === "DENIED";
            return (
              <li
                key={c.id}
                className={`flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-2xs font-mono ${
                  held
                    ? "border-amber-700 bg-amber-950/30 text-amber-300"
                    : c.status === "RUNNING"
                      ? "border-cyan-800 bg-cyan-950/30 text-cyan-300"
                      : "border-zinc-800 bg-zinc-900/60 text-zinc-300"
                }`}
              >
                {c.status === "RUNNING" ? <Loader2 className="h-2.5 w-2.5 animate-spin" /> : <Wrench className="h-2.5 w-2.5" />}
                <span>{TOOL_LABELS[c.tool] ?? c.tool}</span>
                <span className="text-zinc-400">{held ? "held for approval" : c.status === "RUNNING" ? "running" : "done"}</span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

function MessageView({ message }: { message: BrainMessage }) {
  if (message.role === "system") {
    return (
      <div className="msg-enter flex items-start gap-1.5 px-2 text-2xs font-mono text-zinc-500" data-testid="brain-system-note">
        <Info className="mt-0.5 h-3 w-3 flex-none" />
        <span>{message.content}</span>
      </div>
    );
  }
  const user = message.role === "user";
  return (
    <div className={`msg-enter flex ${user ? "justify-end" : "justify-start"}`} data-testid={user ? "brain-user-message" : "brain-assistant-message"}>
      <div
        className={`max-w-[92%] rounded-2xl border p-3.5 shadow-xs ${
          user ? "border-cyan-800/60 bg-cyan-950/50 text-cyan-100" : "border-zinc-800/90 bg-zinc-900/95 text-zinc-200"
        }`}
      >
        <div className="mb-1.5 flex items-center justify-between gap-3 text-2xs font-mono uppercase tracking-wider text-zinc-400">
          <span className="flex items-center gap-1.5">
            {user ? <UserIcon className="h-3 w-3 text-cyan-400" /> : <BrainCircuit className="h-3 w-3 text-cyan-400" />}
            {user ? "You" : "Brain"}
          </span>
          {!user && message.traceId && message.status !== "streaming" && (
            <button
              type="button"
              onClick={() => dispatchBrainOpenTrace(message.traceId!)}
              className="normal-case tracking-normal text-cyan-400 hover:text-cyan-300 hover:underline"
            >
              trace
            </button>
          )}
        </div>
        {!user && <ToolChips message={message} />}
        {user ? (
          <p className="whitespace-pre-wrap text-xs">{message.content}</p>
        ) : (
          <div className="text-xs leading-relaxed [&_p]:my-1 [&_ul]:my-1 [&_table]:text-2xs">
            {message.content ? (
              <ReactMarkdown remarkPlugins={[remarkGfm]} components={{ img: () => null }}>
                {message.content}
              </ReactMarkdown>
            ) : message.status === "streaming" ? (
              <span className="flex items-center gap-1 text-zinc-400" role="status">
                <Loader2 className="h-3 w-3 animate-spin" /> Thinking…
              </span>
            ) : null}
            {message.status === "streaming" && message.content && <span className="ml-0.5 inline-block h-3 w-1.5 animate-pulse bg-cyan-400 align-middle" aria-hidden />}
          </div>
        )}
        {message.widgets.map((w) => (
          <BrainWidgetView key={w.id} widget={w} />
        ))}
        {message.status === "stopped" && <p className="mt-1 text-2xs font-mono text-zinc-500">Stopped.</p>}
        {message.status === "error" && message.error && (
          <p role="alert" className="mt-1 flex items-center gap-1 text-2xs font-mono text-rose-400">
            <ShieldAlert className="h-3 w-3" />
            {message.error.code === "BUDGET"
              ? `Daily AI budget reached: ${message.error.message}`
              : message.error.code === "AUTH"
                ? "Sign in as operator to talk to the Brain."
                : message.error.message}
          </p>
        )}
      </div>
    </div>
  );
}

export function BrainConversation({ compact = false, onShowHistory }: { compact?: boolean; onShowHistory?: () => void }) {
  const messages = useBrainStore((s) => s.messages);
  const streaming = useBrainStore((s) => s.streaming);
  const draft = useBrainStore((s) => s.draft);
  const setDraft = useBrainStore((s) => s.setDraft);
  const newConversation = useBrainStore((s) => s.newConversation);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [messages]);

  const submit = (text: string) => {
    const t = text.trim();
    if (!t) return;
    if (t === "/new") {
      newConversation();
      setDraft("");
      return;
    }
    if (t === "/why") {
      const last = [...useBrainStore.getState().messages].reverse().find((m) => m.traceId);
      if (last?.traceId) dispatchBrainOpenTrace(last.traceId);
      setDraft("");
      return;
    }
    const swingMatch = t.match(/^\/swing(?:\s+([A-Za-z0-9._/-]+))?\s*$/i);
    if (swingMatch) {
      setDraft("");
      if (swingMatch[1]) {
        let sym = swingMatch[1].trim().toUpperCase().replace(/[/\-_]/g, ".");
        if (sym.endsWith(".BINANCE")) {
          sym = sym.slice(0, -".BINANCE".length);
        }
        const quoteSuffixes = ["USDT", "USDC", "FDUSD", "BUSD", "TUSD", "DAI"];
        if (sym.includes(".")) {
          const parts = sym.split(".");
          if (parts.length >= 2 && quoteSuffixes.includes(parts[1]) && !quoteSuffixes.some((q) => parts[0].endsWith(q))) {
            sym = `${parts[0]}${parts[1]}.BINANCE`;
          } else {
            sym = `${parts[0]}.BINANCE`;
          }
        } else if (!quoteSuffixes.some((q) => sym.endsWith(q))) {
          sym = `${sym}USDT.BINANCE`;
        } else {
          sym = `${sym}.BINANCE`;
        }
        void sendBrainMessage(
          `Run scan_swing_setups with symbol="${sym}" and evaluate a multi-timeframe (4h/1h/15m) swing trade on ${sym}. If R:R >= 1.8, propose an order card with structural stop_loss and take_profit.`
        );
      } else {
        void sendBrainMessage(
          `Run scan_swing_setups across the liquid universe and show me the top multi-timeframe swing trade setups with Entry, Structural Stop, TP1, and R:R.`
        );
      }
      return;
    }
    const pm = POSTMORTEM.exec(t);
    if (pm) {
      // An argument that is not a symbol leaves the draft in place, so the operator can correct it
      const prompt = postmortemPrompt(pm[1] ?? "");
      if (prompt) void sendBrainMessage(prompt);
      return;
    }
    void sendBrainMessage(t);
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className={`flex-1 space-y-3 overflow-y-auto ${compact ? "p-3" : "p-4 sm:p-6"}`} aria-live="polite" data-testid="brain-conversation">
        {messages.length === 0 && (
          <div className="mx-auto max-w-xl py-8 sm:py-12 text-center">
            <div className="mx-auto mb-4 inline-flex h-12 w-12 items-center justify-center rounded-2xl border border-cyan-500/30 bg-cyan-950/60 shadow-sm">
              <BrainCircuit className="h-6 w-6 text-cyan-400" />
            </div>
            <p className="font-serif text-xl sm:text-2xl font-semibold tracking-tight text-zinc-100">
              One conversation with the Brain
            </p>
            <p className="mx-auto mt-2 max-w-md font-mono text-2xs leading-relaxed text-zinc-400">
              It reads markets, research data and backtests, and can stage debates. It can only <em>propose</em> paper orders:
              every order card waits for your approval.
            </p>
            <div className="mt-6 grid grid-cols-1 gap-2 text-left font-mono">
              {STARTERS.map((s) => (
                <button
                  key={s}
                  type="button"
                  onClick={() => submit(s)}
                  className="group flex items-center justify-between gap-3 rounded-xl border border-zinc-800/90 bg-zinc-900/80 px-4 py-3 text-left text-xs text-zinc-300 transition-all hover:border-cyan-500/50 hover:bg-zinc-900 hover:text-cyan-200 shadow-xs"
                >
                  <span>{s}</span>
                  <ChevronRight className="h-3.5 w-3.5 flex-none text-zinc-400 transition-transform group-hover:translate-x-0.5 group-hover:text-cyan-400" aria-hidden="true" />
                </button>
              ))}
            </div>
            <BrainRecentConversations onShowAll={onShowHistory} />
          </div>
        )}
        {messages.map((m) => (
          <MessageView key={m.id} message={m} />
        ))}
        <div ref={endRef} />
      </div>

      <form
        className="flex-none border-t border-zinc-800/80 bg-zinc-950/90 p-3 sm:px-4"
        onSubmit={(e) => {
          e.preventDefault();
          submit(draft);
        }}
      >
        <div className="rounded-2xl border border-zinc-800/90 bg-zinc-900/90 p-2.5 shadow-sm transition-colors focus-within:border-cyan-500/60">
          <div className="mb-2 flex flex-wrap items-center gap-1.5 font-mono text-2xs text-zinc-400">
            <span className="text-zinc-400">Quick:</span>
            <button
              type="button"
              onClick={() => submit("/swing")}
              className="rounded-md border border-cyan-800/60 bg-cyan-950/50 px-2 py-0.5 text-2xs font-semibold text-cyan-300 hover:bg-cyan-900/60 transition-colors"
            >
              /swing
            </button>
            <button
              type="button"
              onClick={() => submit("/postmortem")}
              className="rounded-md border border-zinc-800 bg-zinc-950/60 px-2 py-0.5 text-2xs text-zinc-300 hover:border-zinc-700 hover:text-zinc-100 transition-colors"
            >
              /postmortem
            </button>
          </div>
          <div className="flex items-end gap-2">
            <label htmlFor={compact ? "brain-input-compact" : "brain-input"} className="sr-only">
              Message the Brain
            </label>
            <textarea
              id={compact ? "brain-input-compact" : "brain-input"}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  submit(draft);
                }
              }}
              rows={compact ? 1 : 2}
              placeholder="Ask the Brain… (/swing scans MTF setups, /why opens the last trace, /new starts over, /postmortem [SYMBOL] reviews a trade)"
              className="min-h-[36px] flex-1 resize-none rounded-xl border border-zinc-800/80 bg-zinc-950 px-3 py-2 text-xs text-zinc-200 focus:border-cyan-600 focus:outline-none"
            />
            {streaming ? (
              <button
                type="button"
                onClick={stopBrain}
                className="flex items-center gap-1 rounded-xl border border-rose-800 bg-rose-950/60 px-3.5 py-2 text-2xs font-bold text-rose-300 hover:bg-rose-900"
                aria-label="Stop the Brain"
              >
                <Square className="h-3 w-3" /> Stop
              </button>
            ) : (
              <button
                type="submit"
                disabled={!draft.trim()}
                className="flex items-center gap-1 rounded-xl bg-cyan-600 px-3.5 py-2 text-2xs font-bold text-zinc-950 hover:bg-cyan-500 disabled:opacity-40 transition-colors"
                aria-label="Send to the Brain"
              >
                <Send className="h-3 w-3" /> Send
              </button>
            )}
          </div>
        </div>
      </form>
    </div>
  );
}
