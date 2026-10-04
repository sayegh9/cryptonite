import {
  BrainCircuit,
  TrendingUp,
  ShieldAlert,
  Search,
  Zap,
  CheckCircle2,
  ShieldCheck,
  Cpu,
  Layers,
  Tag,
} from "lucide-react";

/** Canvas agent configs, message types and starter prompts (split from OrchestratorCanvas, R6.7). */

export interface Dispatch {
  agent: string;
  /** Not reported by the heartbeat stream: null renders nothing rather than a made-up latency. */
  latencyMs: number | null;
  status: string;
}

export interface Message {
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

export const newId = (prefix: string) => `${prefix}-${crypto.randomUUID()}`;

/** Symbol named in operator text (e.g. "BTC" -> BTCUSDT), else the fallback. */
export function parseTargetSymbol(text: string, fallback: string): string {
  const match = text.match(/\b(BTC|ETH|SOL|XRP|BNB|DOGE|ADA|AVAX|SUI|LINK|DOT|NEAR|MATIC|ATOM|APT|OP|ARB|UNI)\b/i);
  if (match) return match[1].toUpperCase() + "USDT";
  return fallback || "BTCUSDT";
}

export interface AgentConfig {
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

export interface StarterChip {
  icon: any;
  color: string;
  title: string;
  prompt: string;
}

export const AGENT_CONFIGS: Record<string, AgentConfig> = {
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

/** Starter prompt chips per agent. */
export function starterChips(agentId: string, sym: string): StarterChip[] {
  switch (agentId) {
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
}
