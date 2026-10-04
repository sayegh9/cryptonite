/**
 * Workstation navigation (R6.1/R6.2): every workspace and sub-view has a URL `/?ws=<ws>&view=<view>`.
 * Internal workspace ids are kept (the AI Brain is "debates" internally, "brain" in the URL).
 */
export type WorkspaceMode =
  | "terminal" | "markets" | "portfolio" | "research" | "debates" | "mission" | "operator" | "alerts" | "settings";

/** Landing workspace when the URL names none (R6.3: Mission Control). */
export const DEFAULT_WORKSPACE: WorkspaceMode = "mission";

const URL_ALIAS: Partial<Record<WorkspaceMode, string>> = { debates: "brain" };
const MODES: WorkspaceMode[] = ["terminal", "markets", "portfolio", "research", "debates", "mission", "operator", "alerts", "settings"];

export function parseWorkspace(ws: string | null | undefined): WorkspaceMode | null {
  if (!ws) return null;
  const v = ws.toLowerCase();
  if (v === "brain") return "debates";
  return (MODES as string[]).includes(v) ? (v as WorkspaceMode) : null;
}

export const workspaceParam = (mode: WorkspaceMode) => URL_ALIAS[mode] ?? mode;

/** Sub-views that have their own URL. */
export const VIEWS: Partial<Record<WorkspaceMode, { id: string; label: string }[]>> = {
  terminal: [{ id: "scanner", label: "Scanner" }],
  debates: [
    { id: "home", label: "Brain Home" },
    { id: "debates", label: "Debates" },
  ],
  mission: [
    { id: "overview", label: "Overview" },
    { id: "mandates", label: "Mandate editor" },
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

export interface NavItem {
  mode: WorkspaceMode;
  /** Visible tab text: e2e specs click these by name, keep them stable. */
  label: string;
  view?: string;
}

/** Header groups. Labels are the visible tab texts. */
export const NAV_GROUPS: { id: string; label: string; items: NavItem[] }[] = [
  { id: "trade", label: "Trade", items: [
    { mode: "terminal", label: "Terminal" },
    { mode: "markets", label: "Markets" },
    { mode: "portfolio", label: "Portfolio" },
  ] },
  { id: "research", label: "Research", items: [
    { mode: "research", label: "Research Lab" },
    { mode: "terminal", label: "Scanner", view: "scanner" },
  ] },
  { id: "ai", label: "AI", items: [
    { mode: "debates", label: "AI Brain" },
    { mode: "mission", label: "Mission Control" },
  ] },
  { id: "system", label: "System", items: [
    { mode: "operator", label: "Operator" },
    { mode: "alerts", label: "Alerts" },
    { mode: "settings", label: "Settings" },
  ] },
];

export interface SwitchWorkspaceDetail {
  mode: WorkspaceMode;
  view?: string;
}

/** Typed `cryptonite:switch-workspace` event (the page pushes a history entry for it). */
export function dispatchSwitchWorkspace(detail: SwitchWorkspaceDetail) {
  window.dispatchEvent(new CustomEvent<SwitchWorkspaceDetail>("cryptonite:switch-workspace", { detail }));
}
