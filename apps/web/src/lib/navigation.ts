/**
 * Workstation navigation (R6.1/R6.2): every workspace and sub-view has a URL `/?ws=<ws>&view=<view>`.
 * Internal workspace ids are kept (the AI Brain is "debates" internally, "brain" in the URL).
 */
export type WorkspaceMode =
  | "trades"
  | "strategies"
  | "governance"
  | "terminal"
  | "markets"
  | "portfolio"
  | "research"
  | "debates"
  | "mission"
  | "operator"
  | "alerts"
  | "settings";

/** Landing workspace when the URL names none (R6.3: Mission Control). */
export const DEFAULT_WORKSPACE: WorkspaceMode = "mission";

const URL_ALIAS: Partial<Record<WorkspaceMode, string>> = { debates: "brain" };
const MODES: WorkspaceMode[] = [
  "trades",
  "strategies",
  "governance",
  "terminal",
  "markets",
  "portfolio",
  "research",
  "debates",
  "mission",
  "operator",
  "alerts",
  "settings",
];

export function parseWorkspace(ws: string | null | undefined): WorkspaceMode | null {
  if (!ws) return null;
  const v = ws.toLowerCase().trim();
  if (v === "brain") return "debates";
  if (v === "trade") return "trades";
  if (v === "strategy") return "strategies";
  if (v === "gov") return "governance";
  return (MODES as string[]).includes(v) ? (v as WorkspaceMode) : null;
}

export const workspaceParam = (mode: WorkspaceMode) => URL_ALIAS[mode] ?? mode;

/** Sub-views that have their own URL. */
export const VIEWS: Partial<Record<WorkspaceMode, { id: string; label: string }[]>> = {
  terminal: [{ id: "scanner", label: "Scanner" }],
  trades: [{ id: "scanner", label: "Scanner" }],
  debates: [
    { id: "home", label: "Brain Home" },
    { id: "debates", label: "Debates" },
    { id: "pixel-office", label: "Pixel Office" },
  ],
  mission: [
    { id: "overview", label: "Overview" },
    { id: "mandates", label: "Mandate editor" },
    { id: "autonomy", label: "Autonomy & Evals" },
  ],
  governance: [
    { id: "overview", label: "Overview" },
    { id: "mandates", label: "Mandate editor" },
    { id: "approvals", label: "Approval queue" },
    { id: "limits", label: "Risk limits" },
    { id: "reconciliation", label: "Reconciliation" },
    { id: "autonomy", label: "Autonomy & Evals" },
  ],
};

export function parseView(mode: WorkspaceMode, view: string | null | undefined): string | null {
  if (!view) return null;
  return VIEWS[mode]?.some((v) => v.id === view) ? view : null;
}

export function buildQuery(mode: WorkspaceMode, view?: string | null): string {
  const p = new URLSearchParams();
  p.set("ws", workspaceParam(mode));
  if (view && parseView(mode, view)) p.set("view", view);
  return `?${p.toString()}`;
}

export type SettingsSection =
  | "preferences"
  | "ai"
  | "exit"
  | "risk"
  | "scanner"
  | "alerts"
  | "security"
  | "system";

export interface SettingsSectionMeta {
  id: SettingsSection;
  label: string;
  shortLabel: string;
  description: string;
  keywords: string;
}

export const SETTINGS_SECTIONS: SettingsSectionMeta[] = [
  {
    id: "preferences",
    label: "Desk Preferences",
    shortLabel: "Preferences",
    description: "Theme, density, audio alerts, quote currency & workstation defaults",
    keywords: "theme light dark density audio volume chime pnl currency precision timezone",
  },
  {
    id: "ai",
    label: "AI Gateway & Model Routing",
    shortLabel: "AI Gateway",
    description: "Gemini / local LLM provider keys, temperature, latency tiers & fallback routing",
    keywords: "ai llm gemini claude gpt model routing providers temperature tokens fallback",
  },
  {
    id: "exit",
    label: "Exit Rules & Stop Loss",
    shortLabel: "Exit Rules",
    description: "Dynamic trailing stops, take-profit ladders, time stops & breakeven triggers",
    keywords: "exit stoploss takeprofit trailing breakeven ladder liquidation rmultiples",
  },
  {
    id: "risk",
    label: "Risk & Guardrails",
    shortLabel: "Risk Guardrails",
    description: "Mandate exposure caps, daily loss limits, leverage ceilings & drawdown controls",
    keywords: "risk exposure drawdown leverage limits guardrails capital allocation",
  },
  {
    id: "scanner",
    label: "Scanner & Universe",
    shortLabel: "Scanner Universe",
    description: "Screening presets, candle poll intervals, Polars compute threads & pair universe",
    keywords: "scanner universe polars filters momentum breakout screening refresh",
  },
  {
    id: "alerts",
    label: "Alerts & Integrations",
    shortLabel: "Alerts & Webhooks",
    description: "Webhook notification targets, Telegram/Discord routing & severity filters",
    keywords: "alerts webhooks notifications telegram discord slack pagerduty email",
  },
  {
    id: "security",
    label: "Security & Session Policy",
    shortLabel: "Security & Access",
    description: "Operator TOTP 2FA, session timeout rules, API key rotation & CSRF policy",
    keywords: "security totp 2fa auth session timeout apikey rotation credentials",
  },
  {
    id: "system",
    label: "System Health & Audit Log",
    shortLabel: "System Audit",
    description: "Database migration ledger, Redis stream telemetry & immutable audit trail",
    keywords: "system health audit logs postgres redis stream telemetry migrations",
  },
];

export interface NavItem {
  mode: WorkspaceMode;
  /** Visible tab text: e2e specs click these by name, keep them stable. */
  label: string;
  shortLabel?: string;
  view?: string;
  /**
   * Secondary destinations. Below the header's overflow breakpoint they move into the "More" menu, so the
   * primary tabs fit a narrow pane instead of scrolling.
   */
  overflow?: boolean;
  description: string;
  iconName?: string;
}

export interface NavGroup {
  id: string;
  label: string;
  pillarTitle: string;
  shortLabel: string;
  pillarNumber: number;
  description: string;
  iconName: string;
  items: NavItem[];
}

export interface PillarMeta {
  id: string;
  pillar: string;
  pillarNumber: number;
  label: string;
  shortLabel: string;
  description: string;
  mode: WorkspaceMode;
  iconName: string;
}

export const FOUR_PILLARS: PillarMeta[] = [
  {
    id: "trades",
    pillar: "Pillar 1",
    pillarNumber: 1,
    label: "Trades & Execution",
    shortLabel: "Trades",
    description: "Real-time charts, order placement, blotter & execution terminal",
    mode: "trades",
    iconName: "TrendingUp",
  },
  {
    id: "strategies",
    pillar: "Pillar 2",
    pillarNumber: 2,
    label: "Strategy Studio",
    shortLabel: "Strategies",
    description: "Vector backtesting, alpha strategy discovery & parameter sweeps",
    mode: "strategies",
    iconName: "FlaskConical",
  },
  {
    id: "ai-brain",
    pillar: "Pillar 3",
    pillarNumber: 3,
    label: "AI Brain",
    shortLabel: "AI Brain",
    description: "Consensus debates, thesis evaluations & LLM decision audit",
    mode: "debates",
    iconName: "BrainCircuit",
  },
  {
    id: "governance",
    pillar: "Pillar 4",
    pillarNumber: 4,
    label: "Risk & Governance",
    shortLabel: "Risk & Governance",
    description: "Trading mandates, risk autonomy levels, kill switch & operator controls",
    mode: "governance",
    iconName: "ShieldAlert",
  },
];

export const UTILITY_ITEMS: NavItem[] = [
  {
    mode: "alerts",
    label: "Alerts",
    description: "Real-time price breach alerts & notification rules",
    iconName: "Bell",
  },
  {
    mode: "settings",
    label: "Settings",
    description: "Desk preferences, risk limits, AI routing & institutional policies",
    iconName: "Settings",
  },
];

/** Header groups reflecting the Institutional 4 Core Pillars. Labels match existing e2e groups. */
export const NAV_GROUPS: NavGroup[] = [
  {
    id: "trade",
    label: "Trade",
    pillarTitle: "Trades & Execution",
    shortLabel: "Trades",
    pillarNumber: 1,
    description: "Real-time charts, blotter & execution terminal",
    iconName: "TrendingUp",
    items: [
      {
        mode: "terminal",
        label: "Terminal",
        description: "Real-time charts, order placement & trade execution",
        iconName: "Terminal",
      },
      {
        mode: "markets",
        label: "Markets",
        description: "Live price tickers, volume movers & orderbook depth",
        iconName: "TrendingUp",
      },
      {
        mode: "portfolio",
        label: "Portfolio",
        description: "Realized PnL, open positions & asset allocation",
        iconName: "Briefcase",
      },
    ],
  },
  {
    id: "research",
    label: "Research",
    pillarTitle: "Strategy Studio",
    shortLabel: "Strategies",
    pillarNumber: 2,
    description: "Vector backtesting, alpha strategy discovery & parameter sweeps",
    iconName: "FlaskConical",
    items: [
      {
        mode: "research",
        label: "Research Lab",
        description: "Vector backtesting, alpha strategy discovery & parameter sweeps",
        iconName: "FlaskConical",
      },
      {
        mode: "terminal",
        label: "Scanner",
        view: "scanner",
        overflow: true,
        description: "Vectorized Polars scanner & candidate momentum signals",
        iconName: "Search",
      },
    ],
  },
  {
    id: "ai",
    label: "AI",
    pillarTitle: "AI Brain",
    shortLabel: "AI Brain",
    pillarNumber: 3,
    description: "Consensus debates, thesis evaluations & LLM decision audit",
    iconName: "BrainCircuit",
    items: [
      {
        mode: "debates",
        label: "AI Brain",
        description: "Consensus debates, thesis evaluations & LLM decision audit",
        iconName: "BrainCircuit",
      },
      {
        mode: "mission",
        label: "Mission Control",
        description: "Trading mandates, risk autonomy levels & human-in-the-loop approvals",
        iconName: "Gauge",
      },
    ],
  },
  {
    id: "system",
    label: "System",
    pillarTitle: "Risk & Governance",
    shortLabel: "Risk & Governance",
    pillarNumber: 4,
    description: "Emergency kill switch, ledger reconciliation & engine instances",
    iconName: "ShieldAlert",
    items: [
      {
        mode: "operator",
        label: "Operator",
        overflow: true,
        description: "Emergency kill switch, ledger reconciliation & engine instances",
        iconName: "Activity",
      },
      {
        mode: "alerts",
        label: "Alerts",
        overflow: true,
        description: "Real-time price breach alerts & notification rules",
        iconName: "Bell",
      },
      {
        mode: "settings",
        label: "Settings",
        overflow: true,
        description: "Desk preferences, risk limits, AI routing & institutional policies",
        iconName: "Settings",
      },
    ],
  },
];

export interface SwitchWorkspaceDetail {
  mode: WorkspaceMode;
  view?: string;
  section?: SettingsSection;
}

let lastDispatchedSection: SettingsSection | null = null;

export function getLastDispatchedSettingsSection(): SettingsSection | null {
  const sec = lastDispatchedSection;
  lastDispatchedSection = null;
  return sec;
}

/** Typed `cryptonite:switch-workspace` event (the page pushes a history entry for it). */
export function dispatchSwitchWorkspace(detail: SwitchWorkspaceDetail) {
  if (detail.section) {
    lastDispatchedSection = detail.section;
  }
  window.dispatchEvent(new CustomEvent<SwitchWorkspaceDetail>("cryptonite:switch-workspace", { detail }));
}

