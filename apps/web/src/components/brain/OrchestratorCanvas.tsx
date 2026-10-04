"use client";

import React, { useState, useRef, useEffect } from "react";
import {
  BrainCircuit,
  Send,
  Sparkles,
  Loader2,
  Trash2,
  TrendingUp,
  ShieldAlert,
  Search,
  Zap,
  Tag,
  CheckCircle2,
  ShieldCheck,
  Cpu,
  Layers,
  Sliders,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { fetchApi } from "@/lib/api";
import { toast } from "sonner";
import { usePaperEquity, EQUITY_MAX_AGE_MS } from "@/lib/portfolio/usePaperEquity";
import { Fresh } from "@/components/Fresh";

interface Dispatch {
  agent: string;
  /** Not reported by the heartbeat stream: null renders nothing rather than a made-up latency. */
  latencyMs: number | null;
  status: string;
}

interface Message {
  id: string;
  /** "system" = produced by this UI (errors), never model text */
  role: "user" | "assistant" | "system";
  agentId: string;
  agentName: string;
  content: string;
  dispatches?: Dispatch[];
  /** On a system error: the operator prompt to send again */
  retryPrompt?: string;
  timestamp: number;
}

const newId = (prefix: string) => `${prefix}-${crypto.randomUUID()}`;

interface OrchestratorCanvasProps {
  selectedAgentId?: string;
  onSelectAgent?: (agentId: string) => void;
  activeSymbol: string;
  activeTimeframe?: string;
  onTriggerDebate?: (symbol: string) => void;
}

const AGENT_CONFIGS: Record<
  string,
  {
    id: string;
    name: string;
    shortName: string;
    roleTitle: string;
    icon: any;
    color: string;
    bgBorder: string;
    badgeColor: string;
    endpoint: string;
    placeholder: string;
    description: string;
  }
> = {
  orchestrator: {
    id: "orchestrator",
    name: "Lead Orchestrator",
    shortName: "Lead Copilot",
    roleTitle: "Chief AI Copilot & Dispatcher",
    icon: BrainCircuit,
    color: "text-cyan-400",
    bgBorder: "border-cyan-500/30 bg-cyan-950/20",
    badgeColor: "bg-cyan-950/80 border-cyan-700/60 text-cyan-300",
    endpoint: "/agent-api/chat",
    placeholder: "Ask the Lead Orchestrator to coordinate fleet, synthesize multi-asset briefs, or audit trades...",
    description: "Understands natural language, coordinates specialist fleet, and synthesizes action briefs.",
  },
  scout: {
    id: "scout",
    name: "Market Data Scout",
    shortName: "Market Scout",
    roleTitle: "Technical & Orderbook Analyst",
    icon: Search,
    color: "text-blue-400",
    bgBorder: "border-blue-500/30 bg-blue-950/20",
    badgeColor: "bg-blue-950/80 border-blue-700/60 text-blue-300",
    endpoint: "/agent-api/chat",
    placeholder: "Ask Market Data Scout about multi-timeframe technical structure, orderbook depth, funding rate...",
    description: "Gathers live L2 orderbook depth, funding rate spreads, and Polars technical indicators.",
  },
  debater: {
    id: "debater",
    name: "Bull / Bear Debaters",
    shortName: "Bull/Bear Arena",
    roleTitle: "Adversarial Setup Stress-Testing",
    icon: Zap,
    color: "text-purple-400",
    bgBorder: "border-purple-500/30 bg-purple-950/20",
    badgeColor: "bg-purple-950/80 border-purple-700/60 text-purple-300",
    endpoint: "/agent-api/strategist",
    placeholder: "Prompt Bull/Bear Debaters to challenge trade setups, expose traps, and stress-test conviction...",
    description: "Autonomous LLMs debating opposing theses to expose traps and validate trade conviction.",
  },
  cro: {
    id: "cro",
    name: "Risk Guardian (CRO)",
    shortName: "Risk (CRO)",
    roleTitle: "Chief Risk & Margin Officer",
    icon: ShieldAlert,
    color: "text-amber-400",
    bgBorder: "border-amber-500/30 bg-amber-950/20",
    badgeColor: "bg-amber-950/80 border-amber-700/60 text-amber-300",
    endpoint: "/agent-api/cro",
    placeholder: "Ask Chief Risk Officer about position sizing, 25% max concentration, drawdown stress-tests...",
    description: "Enforces max 25% concentration limit, computes stop-loss levels, and guards margin bounds.",
  },
  outbox: {
    id: "outbox",
    name: "Execution Outbox Relay",
    shortName: "Execution Outbox",
    roleTitle: "Nautilus Execution Bridge",
    icon: CheckCircle2,
    color: "text-emerald-400",
    bgBorder: "border-emerald-500/30 bg-emerald-950/20",
    badgeColor: "bg-emerald-950/80 border-emerald-700/60 text-emerald-300",
    endpoint: "/agent-api/chat",
    placeholder: "Ask Execution Relay about order staging, execution latency, Nautilus routing parameters...",
    description: "Stages approved proposals into PostgreSQL transactional outbox for live/testnet execution.",
  },
};

/** Symbol named by a manifest JSON (instrument_id / symbol), else null. */
function manifestSymbol(codeStr: string): string | null {
  try {
    const j = JSON.parse(codeStr);
    const raw = j?.instrument_id ?? j?.symbol;
    return typeof raw === "string" && raw ? raw.split(".")[0].toUpperCase() : null;
  } catch {
    const m = codeStr.match(/"(?:instrument_id|symbol)"\s*:\s*"([A-Z0-9]+)/i);
    return m ? m[1].toUpperCase() : null;
  }
}

function InlineActionCard({ codeStr, onTriggerDebate, fallbackSymbol }: { codeStr: string; onTriggerDebate?: (symbol: string) => void; fallbackSymbol: string }) {
  // Allocation in % of the paper account's equity; USD only when that equity is known
  const [pct, setPct] = useState<number>(5);
  const equity = usePaperEquity();
  const symbol = manifestSymbol(codeStr) ?? fallbackSymbol;

  return (
    <div className="my-4 p-4 rounded-xl border border-cyan-500/30 bg-cyan-950/20 font-sans shadow-lg">
      <div className="flex items-center justify-between mb-3 border-b border-cyan-800/50 pb-2">
        <div className="font-bold text-cyan-300 flex items-center gap-2">
          <Sparkles className="w-4 h-4" /> Strategy Manifest
        </div>
      </div>
      <pre className="text-[10px] text-zinc-300 bg-zinc-900/80 p-3 rounded-lg overflow-x-auto mb-4 border border-zinc-800">
        <code>{codeStr}</code>
      </pre>

      {/* Sizing preview: % of paper equity, disabled while equity is unknown */}
      <div className="p-3 bg-zinc-950/50 border border-zinc-800/60 rounded-lg space-y-2 mb-4 font-mono text-[11px]" data-testid="manifest-sizing">
        <div className="flex items-center justify-between text-zinc-400">
          <span className="flex items-center space-x-1">
            <Sliders className="w-3 h-3 text-cyan-400" aria-hidden="true" />
            <span>Allocation: {pct}% of equity (1–25%)</span>
          </span>
          <span className="text-cyan-300 font-bold tabular-nums">
            <Fresh
              value={equity.usable ? equity.equity : null}
              asOf={equity.asOf}
              maxAgeMs={EQUITY_MAX_AGE_MS}
              format={(e) => `≈ $${((e * pct) / 100).toLocaleString(undefined, { maximumFractionDigits: 0 })}`}
              unknownText="equity unavailable"
            />
          </span>
        </div>
        <input
          type="range"
          min={1}
          max={25}
          step={1}
          value={pct}
          disabled={!equity.usable}
          aria-label="Allocation in percent of equity"
          onChange={(e) => setPct(Number(e.target.value))}
          className="w-full accent-cyan-400 h-1.5 bg-zinc-800 rounded-full cursor-pointer appearance-none disabled:cursor-not-allowed disabled:opacity-40"
        />
      </div>

      <div className="flex flex-wrap gap-2 mt-2">
        <button onClick={() => {
          window.dispatchEvent(new CustomEvent("cryptonite:load-strategy", {
            detail: { name: "Generated", code: codeStr }
          }));
          toast.success("Strategy pushed to Research Lab");
        }} className="px-3 py-1.5 bg-blue-600/20 hover:bg-blue-600/40 text-blue-300 border border-blue-500/50 rounded flex items-center gap-1.5 text-xs font-semibold transition-colors">
          <BrainCircuit className="w-3.5 h-3.5" /> Backtest in Lab
        </button>
        {onTriggerDebate && (
          <button onClick={() => onTriggerDebate(symbol)} className="px-3 py-1.5 bg-purple-600/20 hover:bg-purple-600/40 text-purple-300 border border-purple-500/50 rounded flex items-center gap-1.5 text-xs font-semibold transition-colors">
            <Zap className="w-3.5 h-3.5" /> Trigger {symbol} Debate
          </button>
        )}
      </div>
    </div>
  );
}

export function OrchestratorCanvas({
  selectedAgentId = "orchestrator",
  onSelectAgent,
  activeSymbol,
  activeTimeframe = "1m",
  onTriggerDebate,
}: OrchestratorCanvasProps) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [liveTelemetry, setLiveTelemetry] = useState<Dispatch[]>([]);
  const scrollRef = useRef<HTMLDivElement>(null);

  const currentAgent = AGENT_CONFIGS[selectedAgentId] || AGENT_CONFIGS.orchestrator;

  // Auto-scroll on new messages
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages, loading, liveTelemetry]);

  // Live Telemetry Subscription
  useEffect(() => {
    let unsub: () => void;
    import("@/lib/ws").then(({ getStreamClient }) => {
      const client = getStreamClient();
      unsub = client.subscribe((data) => {
        if (data.type === "HEARTBEAT") {
          setLiveTelemetry((prev) => {
            const existing = prev.find((t) => t.agent === data.agentId);
            if (existing && existing.status === data.msg) return prev; // Avoid duplicates
            return [...prev, { agent: data.agentId, latencyMs: null, status: data.msg }].slice(-5);
          });
        }
      });
    });
    return () => unsub?.();
  }, []);

  // Extract symbol from user text, or default to activeSymbol
  const parseTargetSymbol = (text: string): string => {
    const match = text.match(/\b(BTC|ETH|SOL|XRP|BNB|DOGE|ADA|AVAX|SUI|LINK|DOT|NEAR|MATIC|ATOM|APT|OP|ARB|UNI)\b/i);
    if (match) return match[1].toUpperCase() + "USDT";
    return activeSymbol || "BTCUSDT";
  };

  // Dynamic starter chips per agent
  const getStarterChips = () => {
    const sym = activeSymbol || "BTCUSDT";
    switch (selectedAgentId) {
      case "scout":
        return [
          {
            icon: Search,
            color: "text-blue-400 border-blue-500/30 hover:border-blue-500 bg-blue-950/20",
            title: `Analyze ${sym} Multi-Timeframe Structure`,
            prompt: `Analyze the technical market structure of ${sym} across 15m, 1h, 4h, and 1D timeframes.`,
          },
          {
            icon: Layers,
            color: "text-cyan-400 border-cyan-500/30 hover:border-cyan-500 bg-cyan-950/20",
            title: `Check ${sym} Orderbook Depth`,
            prompt: `Examine L2 orderbook depth, bid/ask imbalances, and institutional liquidity walls for ${sym}.`,
          },
          {
            icon: TrendingUp,
            color: "text-emerald-400 border-emerald-500/30 hover:border-emerald-500 bg-emerald-950/20",
            title: "Scan Volume Delta & RSI",
            prompt: `Scan top crypto pairs for RSI divergence, funding rate anomalies, and volume surges.`,
          },
          {
            icon: Tag,
            color: "text-purple-400 border-purple-500/30 hover:border-purple-500 bg-purple-950/20",
            title: `Calculate ${sym} Pivot Levels`,
            prompt: `Calculate key support, resistance, and Fibonacci pivot levels for ${sym}.`,
          },
        ];
      case "cro":
        return [
          {
            icon: ShieldAlert,
            color: "text-amber-400 border-amber-500/30 hover:border-amber-500 bg-amber-950/20",
            title: `Audit Margin for ${sym}`,
            prompt: `Audit portfolio margin utilization, leverage bounds, and 25% max concentration for a ${sym} trade.`,
          },
          {
            icon: ShieldCheck,
            color: "text-emerald-400 border-emerald-500/30 hover:border-emerald-500 bg-emerald-950/20",
            title: "Compute Risk-Adjusted Sizing",
            prompt: "Calculate optimal position size for a 1.5% stop-loss risking no more than 1% of total equity.",
          },
          {
            icon: TrendingUp,
            color: "text-rose-400 border-rose-500/30 hover:border-rose-500 bg-rose-950/20",
            title: "Stress-Test -10% Market Shock",
            prompt: "Stress-test our current open portfolio positions against a sudden -10% market-wide flash crash.",
          },
          {
            icon: BrainCircuit,
            color: "text-cyan-400 border-cyan-500/30 hover:border-cyan-500 bg-cyan-950/20",
            title: "Review Risk Limits Policy",
            prompt: "Review daily loss limits, kill-switch thresholds, and drawdowns.",
          },
        ];
      case "debater":
        return [
          {
            icon: Zap,
            color: "text-purple-400 border-purple-500/30 hover:border-purple-500 bg-purple-950/20",
            title: `Run ${sym} Consensus Debate`,
            prompt: `Debate the current momentum breakout and orderbook liquidity for ${sym}.`,
          },
          {
            icon: ShieldAlert,
            color: "text-amber-400 border-amber-500/30 hover:border-amber-500 bg-amber-950/20",
            title: `Expose ${sym} Liquidity Traps`,
            prompt: `Identify potential bear traps, liquidity sweeps, and invalidation risks for ${sym}.`,
          },
          {
            icon: TrendingUp,
            color: "text-cyan-400 border-cyan-500/30 hover:border-cyan-500 bg-cyan-950/20",
            title: `Stress-test Long Thesis for ${sym}`,
            prompt: `Aggressively debate the bullish thesis for ${sym} against macro headwinds.`,
          },
          {
            icon: CheckCircle2,
            color: "text-emerald-400 border-emerald-500/30 hover:border-emerald-500 bg-emerald-950/20",
            title: "Synthesize Trade Conviction",
            prompt: `Synthesize the multi-agent consensus and compute the conviction percentage for ${sym}.`,
          },
        ];
      case "outbox":
        return [
          {
            icon: CheckCircle2,
            color: "text-emerald-400 border-emerald-500/30 hover:border-emerald-500 bg-emerald-950/20",
            title: `Stage ${sym} Order to Outbox`,
            prompt: `Stage a limit buy order for ${sym} with a 5 bps slippage bound into the Transactional Outbox.`,
          },
          {
            icon: Cpu,
            color: "text-cyan-400 border-cyan-500/30 hover:border-cyan-500 bg-cyan-950/20",
            title: "Verify Nautilus Engine Latency",
            prompt: "Check Nautilus Trader order routing latency and paper engine heartbeat status.",
          },
          {
            icon: Layers,
            color: "text-blue-400 border-blue-500/30 hover:border-blue-500 bg-blue-950/20",
            title: "Audit Reconciliation Ledger",
            prompt: "Inspect the PostgreSQL Transactional Outbox state machine for any pending or orphaned orders.",
          },
          {
            icon: ShieldCheck,
            color: "text-purple-400 border-purple-500/30 hover:border-purple-500 bg-purple-950/20",
            title: "Explain Outbox State Machine",
            prompt: "Explain how the PostgreSQL Transactional Outbox guarantees zero double-executions.",
          },
        ];
      default: // orchestrator
        return [
          {
            icon: Zap,
            color: "text-cyan-400 border-cyan-500/30 hover:border-cyan-500 bg-cyan-950/20",
            title: `Run ${sym.replace("USDT", "") || "BTC"} Consensus Debate`,
            prompt: `Debate the current momentum breakout and orderbook liquidity for ${sym}.`,
          },
          {
            icon: ShieldAlert,
            color: "text-amber-400 border-amber-500/30 hover:border-amber-500 bg-amber-950/20",
            title: "Audit Capital & Risk",
            prompt: "Audit active exposures, margin utilization, and verify we comply with the 25% max concentration rule.",
          },
          {
            icon: Search,
            color: "text-blue-400 border-blue-500/30 hover:border-blue-500 bg-blue-950/20",
            title: "Scan Momentum Breakouts",
            prompt: "Scan top crypto instruments across 15m and 1h for volume spikes, RSI divergence, and orderbook bid walls.",
          },
          {
            icon: TrendingUp,
            color: "text-purple-400 border-purple-500/30 hover:border-purple-500 bg-purple-950/20",
            title: "Explain Strategy Alpha",
            prompt: "Summarize the key alpha drivers and slippage risks of our active quantitative momentum strategies.",
          },
        ];
    }
  };

  const handleSend = async (customPrompt?: string, retryOf?: string) => {
    const textToSend = customPrompt || input;
    if (!textToSend.trim() || loading) return;

    const targetSym = parseTargetSymbol(textToSend);

    if (retryOf) {
      // Retry replaces its error notice; the operator's message is already on the canvas
      setMessages((prev) => prev.filter((m) => m.id !== retryOf));
    } else {
      const userMsg: Message = {
        id: newId("user"),
        role: "user",
        agentId: selectedAgentId,
        agentName: "Operator",
        content: textToSend,
        timestamp: Date.now(),
      };
      setMessages((prev) => [...prev, userMsg]);
    }
    if (!customPrompt) setInput("");
    setLoading(true);
    setLiveTelemetry([]); // Clear trace for new request

    try {
      // If user asks to debate, trigger background debate event (not again on retry)
      if (!retryOf && textToSend.toLowerCase().includes("debate") && onTriggerDebate) {
        onTriggerDebate(targetSym);
      }

      let finalPrompt = textToSend;
      if (finalPrompt.includes("@Backtest")) {
        finalPrompt += "\n\n[System Auto-Inject: The user referenced a backtest. Please use the read_nautilus_tearsheet tool to pull its metrics and evaluate the Sharpe Ratio and Drawdown.]";
      }
      if (finalPrompt.includes("/delta") || finalPrompt.includes("@PaperPosition")) {
        finalPrompt += "\n\n[System Auto-Inject: The user requested a performance delta diagnostic. Use the compare_live_vs_backtest tool to analyze slippage between live fills and the backtest.]";
      }

      // Call the corresponding agent endpoint
      const endpoint = currentAgent.endpoint || "/agent-api/chat";
      const res = await fetchApi(endpoint, {
        method: "POST",
        headers: { "X-CSRF-Token": localStorage.getItem("csrf_token") || "" },
        body: JSON.stringify({
          message: finalPrompt,
          symbol: targetSym,
          timeframe: activeTimeframe,
        }),
      });

      const responseText = typeof res?.response === "string" && res.response ? res.response
        : typeof res?.message === "string" && res.message ? res.message : "";
      if (!responseText) throw Object.assign(new Error("empty response"), { status: "empty response" });

      // Capture current liveTelemetry before it gets cleared or updated by next turn
      const capturedTelemetry = [...liveTelemetry];

      const assistantMsg: Message = {
        id: newId("asst"),
        role: "assistant",
        agentId: selectedAgentId,
        agentName: currentAgent.name,
        content: responseText,
        dispatches: capturedTelemetry.length > 0 ? capturedTelemetry : undefined,
        timestamp: Date.now(),
      };

      setMessages((prev) => [...prev, assistantMsg]);
    } catch (err: any) {
      // Never invent an answer: the canvas shows that the agent did not respond, with a Retry
      const status = err?.status ?? (err instanceof TypeError ? "network error" : err?.message || "unknown error");
      const errorMsg: Message = {
        id: newId("sys"),
        role: "system",
        agentId: selectedAgentId,
        agentName: currentAgent.name,
        content: `agent unavailable: ${status}`,
        retryPrompt: textToSend,
        timestamp: Date.now(),
      };
      setMessages((prev) => [...prev, errorMsg]);
    } finally {
      setLoading(false);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const insertTag = (tag: string) => {
    // If user clicked an agent tag like @scout, also switch active agent
    if (tag === "@scout" && onSelectAgent) onSelectAgent("scout");
    else if (tag === "@cro" && onSelectAgent) onSelectAgent("cro");
    else if (tag === "@debater" && onSelectAgent) onSelectAgent("debater");
    else if (tag === "@outbox" && onSelectAgent) onSelectAgent("outbox");
    else if (tag === "@lead" && onSelectAgent) onSelectAgent("orchestrator");

    setInput((prev) => prev + (prev.length > 0 && !prev.endsWith(" ") ? " " : "") + tag + " ");
  };

  const IconComponent = currentAgent.icon;

  return (
    <div className="flex flex-col h-full w-full bg-zinc-950 overflow-hidden">
      {/* Top Agent Switcher & Status Bar */}
      <div className="flex-none bg-zinc-900/60 border-b border-zinc-800 px-3 sm:px-4 py-2 flex flex-wrap items-center justify-between gap-2 z-10">
        {/* Active Agent Info & Tabs */}
        <div className="flex items-center space-x-2 overflow-x-auto min-w-0 py-0.5">
          <div className="flex items-center space-x-1.5 mr-2 flex-none">
            <div className={`p-1 rounded bg-zinc-900 border border-zinc-700 ${currentAgent.color}`}>
              <IconComponent className="w-3.5 h-3.5" />
            </div>
            <div className="hidden md:block">
              <div className="font-bold text-xs text-zinc-100 leading-none">{currentAgent.name} Canvas</div>
              <div className="text-[9.5px] text-zinc-400 font-mono mt-0.5">{currentAgent.roleTitle}</div>
            </div>
          </div>

          {/* Quick Agent Switching Tabs */}
          <div className="flex items-center space-x-1 bg-zinc-950/80 p-0.5 rounded-lg border border-zinc-800 flex-none">
            {Object.values(AGENT_CONFIGS).map((ag) => {
              const isAgSelected = selectedAgentId === ag.id;
              const AgIcon = ag.icon;
              return (
                <button
                  key={ag.id}
                  onClick={() => onSelectAgent && onSelectAgent(ag.id)}
                  className={`px-2 py-1 rounded text-[10px] font-mono transition-all flex items-center space-x-1 ${
                    isAgSelected
                      ? `${ag.badgeColor} font-bold shadow-sm`
                      : "text-zinc-400 hover:text-zinc-200 hover:bg-zinc-900"
                  }`}
                  title={`Switch chat to ${ag.name}`}
                >
                  <AgIcon className={`w-3 h-3 ${isAgSelected ? ag.color : "text-zinc-500"}`} />
                  <span className="hidden sm:inline">{ag.shortName}</span>
                </button>
              );
            })}
          </div>
        </div>

        {/* Right Info & Actions */}
        <div className="flex items-center space-x-2 flex-none">
          <span className="text-[10px] font-mono px-2 py-0.5 rounded bg-zinc-800 text-cyan-300 border border-zinc-700 hidden lg:inline">
            Symbol Context: {activeSymbol} ({activeTimeframe})
          </span>

          {messages.length > 0 && (
            <button
              onClick={() => setMessages([])}
              className="flex items-center space-x-1 text-[10px] font-mono text-zinc-500 hover:text-zinc-300 px-2 py-0.5 rounded hover:bg-zinc-900 transition-colors"
              title="Clear canvas history"
            >
              <Trash2 className="w-3 h-3" />
              <span>Clear</span>
            </button>
          )}
        </div>
      </div>

      {/* Messages Scroll Area */}
      <div ref={scrollRef} className="flex-1 min-h-0 overflow-y-auto p-3 sm:p-5 space-y-4">
        <div className="max-w-4xl mx-auto w-full space-y-4">
          {messages.length === 0 ? (
            <div className="py-6 px-4 flex flex-col items-center justify-center max-w-xl mx-auto text-center space-y-4">
              <div className={`w-12 h-12 rounded-2xl ${currentAgent.bgBorder} border flex items-center justify-center ${currentAgent.color} shadow-lg`}>
                <IconComponent className="w-6 h-6" />
              </div>

              <div className="space-y-1">
                <h3 className="text-base font-bold text-zinc-100 tracking-tight flex items-center justify-center space-x-2">
                  <span>Welcome to the Cryptonite AI Brain</span>
                </h3>
                <p className="text-xs text-zinc-400 leading-relaxed max-w-md mx-auto">
                  {currentAgent.description}
                </p>
              </div>

              {/* 1-Touch Starter Prompt Chips tailored to Active Agent */}
              <div className="w-full grid grid-cols-1 sm:grid-cols-2 gap-2 text-left pt-2">
                {getStarterChips().map((chip, i) => {
                  const Icon = chip.icon;
                  return (
                    <button
                      key={i}
                      onClick={() => handleSend(chip.prompt)}
                      className={`p-3 rounded-xl border text-xs font-mono transition-all text-left flex items-start space-x-2.5 ${chip.color}`}
                    >
                      <Icon className="w-4 h-4 flex-none mt-0.5" />
                      <div className="min-w-0 flex-1">
                        <div className="font-bold text-zinc-200 truncate">{chip.title}</div>
                        <div className="text-[10px] text-zinc-400 line-clamp-1 mt-0.5">
                          {chip.prompt}
                        </div>
                      </div>
                    </button>
                  );
                })}
              </div>
            </div>
          ) : (
            messages.map((msg) => {
              const msgAgentConfig = AGENT_CONFIGS[msg.agentId] || AGENT_CONFIGS.orchestrator;
              const MsgIcon = msgAgentConfig.icon;
              if (msg.role === "system") {
                return (
                  <div
                    key={msg.id}
                    role="alert"
                    data-testid="canvas-agent-error"
                    className="flex items-center justify-between gap-3 rounded-lg border border-rose-900/60 bg-rose-950/30 px-3 py-2 font-mono text-[11px] text-rose-300"
                  >
                    <span>
                      <span className="text-zinc-500">{msg.agentName} · {new Date(msg.timestamp).toLocaleTimeString()} · </span>
                      {msg.content}
                    </span>
                    {msg.retryPrompt && (
                      <button
                        type="button"
                        onClick={() => handleSend(msg.retryPrompt, msg.id)}
                        disabled={loading}
                        className="flex-none rounded border border-rose-800 px-2 py-0.5 text-rose-200 hover:bg-rose-900/40 disabled:opacity-50"
                      >
                        Retry
                      </button>
                    )}
                  </div>
                );
              }
              return (
                <div
                  key={msg.id}
                  className={`flex flex-col space-y-1 ${
                    msg.role === "user" ? "items-end" : "items-start"
                  }`}
                >
                  <div className="flex items-center space-x-1.5 text-[10px] font-mono text-zinc-500 px-1">
                    {msg.role === "assistant" && (
                      <span className={`flex items-center space-x-1 font-semibold ${msgAgentConfig.color}`}>
                        <MsgIcon className="w-3 h-3" />
                        <span>{msg.agentName}</span>
                      </span>
                    )}
                    {msg.role === "user" && <span>You (Operator)</span>}
                    <span>•</span>
                    <span>{new Date(msg.timestamp).toLocaleTimeString()}</span>
                  </div>

                  <div
                    className={`text-xs leading-relaxed ${
                      msg.role === "user"
                        ? "max-w-[85%] sm:max-w-lg rounded-2xl rounded-tr-none px-4 py-3 bg-cyan-950/70 border border-cyan-800/80 text-cyan-100 shadow-sm"
                        : `max-w-[95%] sm:max-w-2xl rounded-2xl rounded-tl-none p-4 bg-zinc-900/95 border ${msgAgentConfig.bgBorder} text-zinc-200 shadow-md space-y-2`
                    }`}
                  >
                    {/* Visual Micro-Dispatch Trace if assistant */}
                    {msg.dispatches && msg.dispatches.length > 0 && (
                      <div className="mb-2 p-2.5 bg-zinc-950/90 border border-zinc-800/80 rounded-xl font-mono text-[10px] space-y-1">
                        <div className="text-zinc-500 uppercase tracking-wider text-[9px] font-bold">
                          Specialist Execution Trace:
                        </div>
                        {msg.dispatches.map((d, idx) => (
                          <div key={idx} className="flex items-center justify-between text-zinc-400 gap-2">
                            <span className="flex items-center space-x-1 truncate">
                              <span className={msgAgentConfig.color}>↳</span>
                              <span className="text-zinc-300 font-semibold">{d.agent}:</span>
                              <span className="truncate">{d.status}</span>
                            </span>
                            {d.latencyMs !== null && <span className="text-zinc-500 text-[10px] flex-none">{d.latencyMs}ms</span>}
                          </div>
                        ))}
                      </div>
                    )}

                    {msg.role === "assistant" ? (
                      <div className="prose prose-invert prose-xs max-w-none prose-p:my-1 prose-headings:text-zinc-100 prose-headings:my-2 prose-strong:text-cyan-300">
                        <ReactMarkdown 
                          remarkPlugins={[remarkGfm]}
                          components={{
                            code(props) {
                              const {children, className, node, ...rest} = props;
                              const match = /language-(\w+)/.exec(className || '');
                              const codeStr = String(children).replace(/\n$/, '');
                              
                              // Check if this looks like a strategy manifest JSON block
                              if (match && match[1].includes('json') && (codeStr.includes('allocation_usd') || codeStr.includes('instrument_id') || codeStr.includes('max_position_notional_usd'))) {
                                return <InlineActionCard codeStr={codeStr} onTriggerDebate={onTriggerDebate} fallbackSymbol={activeSymbol || "BTCUSDT"} />;
                              }
                              
                              const isInline = !match && !codeStr.includes('\n');
                              
                              return !isInline ? (
                                <pre className="bg-zinc-900/80 p-3 rounded-lg overflow-x-auto border border-zinc-800 my-2 text-[11px]">
                                  <code className={className} {...rest}>{children}</code>
                                </pre>
                              ) : (
                                <code className="bg-zinc-800/80 text-cyan-200 px-1.5 py-0.5 rounded text-[11px]" {...rest}>{children}</code>
                              );
                            }
                          }}
                        >
                          {msg.content}
                        </ReactMarkdown>
                      </div>

                    ) : (
                      <p className="whitespace-pre-wrap">{msg.content}</p>
                    )}
                  </div>
                </div>
              );
            })
          )}

          {loading && (
            <div className="flex flex-col space-y-3 p-4 bg-zinc-900/50 border border-zinc-800/80 rounded-2xl max-w-2xl">
              <div className="flex items-start space-x-2 text-xs font-mono text-zinc-400">
                <Loader2 className={`w-4 h-4 ${currentAgent.color} animate-spin mt-0.5 flex-none`} />
                <div className="space-y-0.5">
                  <div className="text-zinc-300">{currentAgent.name} processing query...</div>
                  <div className="text-[10px] text-zinc-500">Executing background diagnostics...</div>
                </div>
              </div>
              
              {/* Live Telemetry Display while loading */}
              {liveTelemetry.length > 0 && (
                <div className="p-2.5 bg-zinc-950/90 border border-zinc-800/80 rounded-xl font-mono text-[10px] space-y-1">
                  <div className="text-zinc-500 uppercase tracking-wider text-[9px] font-bold">
                    Live Specialist Telemetry:
                  </div>
                  {liveTelemetry.map((d, idx) => (
                    <div key={idx} className="flex items-center justify-between text-zinc-400 gap-2 animate-in fade-in slide-in-from-bottom-1 duration-300">
                      <span className="flex items-center space-x-1 truncate">
                        <span className={currentAgent.color}>↳</span>
                        <span className="text-zinc-300 font-semibold">{d.agent}:</span>
                        <span className="truncate">{d.status}</span>
                      </span>
                      {d.latencyMs !== null && <span className="text-zinc-500 text-[10px] flex-none">{d.latencyMs}ms</span>}
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {/* Input Rail & Context Stashers */}
      <div className="flex-none p-3 sm:p-4 bg-zinc-900/40 border-t border-zinc-800">
        <div className="max-w-4xl mx-auto w-full space-y-2">
          {/* Quick Context Stash Tags */}
          <div className="flex items-center space-x-1.5 overflow-x-auto text-[10px] font-mono text-zinc-400 py-0.5">
            <span className="text-zinc-500 text-[9px] uppercase tracking-wider flex-none">Quick Mentions:</span>
            {["@scout", "@cro", "@debater", "@outbox", "@lead"].map((agTag, idx) => (
              <button
                key={idx}
                onClick={() => insertTag(agTag)}
                className="px-2 py-0.5 bg-zinc-900 hover:bg-zinc-800 border border-zinc-800 hover:border-zinc-700 rounded text-cyan-300 transition-colors flex-none"
              >
                {agTag}
              </button>
            ))}
            <span className="text-zinc-600">|</span>
            {["@XRP", "@BTC", "@ETH", "@SOL"].map((sym, idx) => (
              <button
                key={idx}
                onClick={() => insertTag(sym)}
                className="px-2 py-0.5 bg-zinc-900 hover:bg-zinc-800 border border-zinc-800 hover:border-zinc-700 rounded text-zinc-300 transition-colors flex-none"
              >
                {sym}
              </button>
            ))}
            <button
              onClick={() => insertTag(`@LiveChart(${activeSymbol || "BTCUSDT"}, ${activeTimeframe})`)}
              className="px-2 py-0.5 bg-cyan-950/50 hover:bg-cyan-950 border border-cyan-800/60 rounded text-cyan-300 transition-colors flex items-center space-x-1 flex-none"
            >
              <Tag className="w-2.5 h-2.5" />
              <span>+ Attach Live Chart</span>
            </button>
            <button
              onClick={() => insertTag(`@Backtest(latest)`)}
              className="px-2 py-0.5 bg-blue-950/50 hover:bg-blue-950 border border-blue-800/60 rounded text-blue-300 transition-colors flex items-center space-x-1 flex-none"
            >
              <BrainCircuit className="w-2.5 h-2.5" />
              <span>+ Attach Backtest</span>
            </button>
            <button
              onClick={() => insertTag(`/delta @PaperPosition(latest)`)}
              className="px-2 py-0.5 bg-purple-950/50 hover:bg-purple-950 border border-purple-800/60 rounded text-purple-300 transition-colors flex items-center space-x-1 flex-none"
            >
              <Zap className="w-2.5 h-2.5" />
              <span>+ Delta Diagnostic</span>
            </button>
          </div>

          {/* Input Box */}
          <div className="relative flex items-center">
            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder={currentAgent.placeholder}
              rows={2}
              className="w-full bg-zinc-900/90 border border-zinc-800 focus:border-cyan-500 rounded-xl py-3 pl-3.5 pr-11 text-xs text-zinc-100 placeholder-zinc-500 resize-none outline-none font-sans transition-colors"
            />
            <button
              onClick={() => handleSend()}
              disabled={!input.trim() || loading}
              className={`absolute right-3 bottom-3.5 p-1.5 rounded-lg bg-cyan-600 hover:bg-cyan-500 disabled:bg-zinc-800 text-zinc-950 disabled:text-zinc-600 transition-colors`}
              title="Send prompt"
            >
              <Send className="w-3.5 h-3.5" />
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
