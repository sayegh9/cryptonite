"use client";

import { useEffect, useRef } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { BrainCircuit, Loader2, Send, Square, User as UserIcon, Wrench, ShieldAlert, Info } from "lucide-react";
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
};

function ToolChips({ message }: { message: BrainMessage }) {
  if (message.toolCalls.length === 0) return null;
  return (
    <ul className="mb-1.5 flex flex-wrap gap-1" aria-label="Tools used">
      {message.toolCalls.map((c) => {
        const held = c.status === "DENIED";
        return (
          <li
            key={c.id}
            className={`flex items-center gap-1 rounded border px-1.5 py-0.5 text-2xs font-mono ${
              held
                ? "border-amber-700 text-amber-300"
                : c.status === "RUNNING"
                  ? "border-cyan-800 text-cyan-300"
                  : "border-zinc-800 text-zinc-400"
            }`}
          >
            {c.status === "RUNNING" ? <Loader2 className="h-2.5 w-2.5 animate-spin" /> : <Wrench className="h-2.5 w-2.5" />}
            <span>{TOOL_LABELS[c.tool] ?? c.tool}</span>
            <span className="text-zinc-500">{held ? "held for approval" : c.status === "RUNNING" ? "running" : "done"}</span>
          </li>
        );
      })}
    </ul>
  );
}

function MessageView({ message }: { message: BrainMessage }) {
  if (message.role === "system") {
    return (
      <div className="flex items-start gap-1.5 px-2 text-2xs font-mono text-zinc-500" data-testid="brain-system-note">
        <Info className="mt-0.5 h-3 w-3 flex-none" />
        <span>{message.content}</span>
      </div>
    );
  }
  const user = message.role === "user";
  return (
    <div className={`flex ${user ? "justify-end" : "justify-start"}`} data-testid={user ? "brain-user-message" : "brain-assistant-message"}>
      <div
        className={`max-w-[92%] rounded-xl border p-3 ${
          user ? "border-cyan-800/50 bg-cyan-950/40 text-cyan-100" : "border-zinc-800/80 bg-zinc-900/90 text-zinc-200"
        }`}
      >
        <div className="mb-1 flex items-center justify-between gap-3 text-2xs font-mono uppercase tracking-wider text-zinc-400">
          <span className="flex items-center gap-1">
            {user ? <UserIcon className="h-3 w-3 text-cyan-400" /> : <BrainCircuit className="h-3 w-3 text-cyan-400" />}
            {user ? "You" : "Brain"}
          </span>
          {!user && message.traceId && message.status !== "streaming" && (
            <button
              type="button"
              onClick={() => dispatchBrainOpenTrace(message.traceId!)}
              className="normal-case tracking-normal text-cyan-500 hover:text-cyan-300 hover:underline"
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
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
            ) : message.status === "streaming" ? (
              <span className="flex items-center gap-1 text-zinc-500" role="status">
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
      <div className={`flex-1 space-y-3 overflow-y-auto ${compact ? "p-3" : "p-4"}`} aria-live="polite" data-testid="brain-conversation">
        {messages.length === 0 && (
          <div className="mx-auto max-w-md py-10 text-center font-mono">
            <BrainCircuit className="mx-auto mb-3 h-10 w-10 text-cyan-400/40" />
            <p className="text-sm font-semibold text-zinc-200">One conversation with the Brain</p>
            <p className="mt-1 text-2xs leading-relaxed text-zinc-400">
              It reads markets, research data and backtests, and can stage debates. It can only <em>propose</em> paper orders:
              every order card waits for your approval.
            </p>
            <div className="mt-4 flex flex-col gap-1.5">
              {STARTERS.map((s) => (
                <button
                  key={s}
                  type="button"
                  onClick={() => submit(s)}
                  className="rounded border border-zinc-800 bg-zinc-900 px-3 py-1.5 text-left text-2xs text-zinc-300 hover:border-cyan-800 hover:text-cyan-200"
                >
                  {s}
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
        className="flex-none border-t border-zinc-800 bg-zinc-900/80 p-3"
        onSubmit={(e) => {
          e.preventDefault();
          submit(draft);
        }}
      >
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
            placeholder="Ask the Brain… (/why opens the last trace, /new starts over, /postmortem [SYMBOL] reviews a trade)"
            className="min-h-[36px] flex-1 resize-none rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 text-xs text-zinc-200 focus:border-cyan-600 focus:outline-none"
          />
          {streaming ? (
            <button
              type="button"
              onClick={stopBrain}
              className="flex items-center gap-1 rounded-lg border border-rose-800 bg-rose-950/60 px-3 py-2 text-2xs font-bold text-rose-300 hover:bg-rose-900"
              aria-label="Stop the Brain"
            >
              <Square className="h-3 w-3" /> Stop
            </button>
          ) : (
            <button
              type="submit"
              disabled={!draft.trim()}
              className="flex items-center gap-1 rounded-lg bg-cyan-600 px-3 py-2 text-2xs font-bold text-zinc-950 hover:bg-cyan-500 disabled:opacity-40"
              aria-label="Send to the Brain"
            >
              <Send className="h-3 w-3" /> Send
            </button>
          )}
        </div>
      </form>
    </div>
  );
}
