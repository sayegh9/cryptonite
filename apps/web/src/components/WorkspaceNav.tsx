"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  ChevronLeft,
  ChevronRight,
  MoreHorizontal,
  Terminal,
  TrendingUp,
  Briefcase,
  FlaskConical,
  Search,
  BrainCircuit,
  Gauge,
  Activity,
  Bell,
  Settings,
  Layers,
  Cpu,
  ShieldAlert,
  Gamepad2,
  PanelLeftClose,
  PanelLeftOpen,
  Lock,
  ShieldCheck,
  Bot,
  ListChecks,
} from "lucide-react";
import { NAV_GROUPS, type NavItem, type WorkspaceMode } from "@/lib/navigation";

// Written out in full so the class scanner sees each utility as a literal.
// Below md (768px) secondary destinations move into the "More" menu; on md+ the vertical sidebar displays all items.
const SHOW_ON_WIDE = "hidden md:flex";
const HIDE_ON_WIDE = "md:hidden";

const OVERFLOW_ITEMS: NavItem[] = NAV_GROUPS.flatMap((g) => g.items.filter((i) => i.overflow));

const TAB_ICONS: Record<string, React.ComponentType<{ className?: string; "aria-hidden"?: boolean | "true" }>> = {
  Terminal,
  Trades: TrendingUp,
  "Trades & Execution": TrendingUp,
  Markets: TrendingUp,
  Portfolio: Briefcase,
  "Research Lab": FlaskConical,
  Strategies: FlaskConical,
  "Strategy Studio": FlaskConical,
  Scanner: Search,
  Setups: ListChecks,
  "AI Brain": BrainCircuit,
  "Mission Control": Gauge,
  "Auto-Trade": Bot,
  Governance: ShieldAlert,
  "Risk & Governance": ShieldAlert,
  Operator: Activity,
  Alerts: Bell,
  Settings,
  Layers,
  Cpu,
};

const isPillarActive = (groupId: string, workspaceMode: WorkspaceMode) => {
  if (groupId === "trade") {
    return workspaceMode === "trades" || workspaceMode === "terminal" || workspaceMode === "markets" || workspaceMode === "portfolio";
  }
  if (groupId === "research") {
    return workspaceMode === "strategies" || workspaceMode === "research" || workspaceMode === "setups";
  }
  if (groupId === "ai") {
    return workspaceMode === "debates" || workspaceMode === "mission" || workspaceMode === "autotrade";
  }
  if (groupId === "system") {
    return workspaceMode === "governance" || workspaceMode === "operator" || workspaceMode === "alerts" || workspaceMode === "settings";
  }
  return false;
};

const PILLAR_MODE_MAP: Record<string, WorkspaceMode> = {
  trade: "trades",
  research: "strategies",
  ai: "debates",
  system: "governance",
};

const isActiveTab = (tab: NavItem, workspaceMode: WorkspaceMode, urlView: string | null) => {
  if (workspaceMode === tab.mode) {
    if (tab.view) return urlView === tab.view;
    return !(tab.mode === "terminal" && urlView === "scanner");
  }
  // URL alias mapping:
  if ((workspaceMode === "trades" || workspaceMode === "portfolio") && tab.mode === "portfolio") return true;
  if (workspaceMode === "strategies" && tab.mode === "research") return true;
  if (workspaceMode === "governance" && tab.mode === "mission") return true;
  return false;
};

/**
 * Vibe-Trading style adaptive navigation:
 * - On desktop (`md+`), renders as a fixed left vertical sidebar (`w-56` expanded, auto-collapsing to a `w-12`
 *   icon rail in Terminal mode) while remaining mounted inside `<header>` so header DOM invariants hold.
 * - Below `md`, renders as a compact horizontal scrollable strip with an overflow menu.
 */
export function WorkspaceNav({
  workspaceMode,
  urlView,
  navigate,
  collapsed = false,
  onToggleCollapse,
  sessionStatus,
  onSignIn,
  className = "",
}: {
  workspaceMode: WorkspaceMode;
  urlView: string | null;
  navigate: (mode: WorkspaceMode, view?: string | null) => void;
  collapsed?: boolean;
  onToggleCollapse?: () => void;
  sessionStatus?: "checking" | "authenticated" | "anonymous" | "unreachable";
  onSignIn?: () => void;
  className?: string;
}) {
  const navRef = useRef<HTMLElement>(null);
  const [edges, setEdges] = useState({ left: false, right: false });

  const measure = useCallback(() => {
    const el = navRef.current;
    if (!el) return;
    const left = el.scrollLeft > 1;
    const right = el.scrollLeft + el.clientWidth < el.scrollWidth - 1;
    setEdges((e) => (e.left === left && e.right === right ? e : { left, right }));
  }, []);

  const revealCurrent = useCallback(() => {
    const el = navRef.current;
    if (!el) return;
    const current = el.querySelector<HTMLElement>('[aria-current="page"]');
    if (!current) return;
    const curLeft = current.offsetLeft;
    const curRight = current.offsetLeft + current.offsetWidth;
    const viewLeft = el.scrollLeft;
    const viewRight = el.scrollLeft + el.clientWidth;
    if (curLeft < viewLeft) {
      el.scrollLeft = curLeft;
    } else if (curRight > viewRight) {
      el.scrollLeft = curRight - el.clientWidth;
    }
  }, []);

  useEffect(() => {
    const el = navRef.current;
    if (!el) return;
    measure();
    const ro = new ResizeObserver(() => {
      revealCurrent();
      measure();
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [measure, revealCurrent]);

  useEffect(revealCurrent, [revealCurrent, workspaceMode, urlView]);

  const scrollBy = (dir: 1 | -1) => {
    const el = navRef.current;
    if (el) el.scrollBy({ left: dir * Math.max(120, el.clientWidth * 0.6), behavior: "smooth" });
  };

  const isPixelOffice = workspaceMode === "debates" && urlView === "pixel-office";

  return (
    <div
      data-testid="workspace-sidebar"
      data-collapsed={collapsed ? "true" : "false"}
      className={`flex min-w-0 items-center md:fixed md:top-12 md:bottom-7 md:left-0 md:z-30 md:h-auto md:min-w-0 md:flex-col md:items-stretch md:justify-between md:border-r md:border-zinc-800 md:bg-zinc-900/95 md:backdrop-blur-md transition-[width] duration-200 ${
        collapsed ? "md:w-12" : "md:w-56"
      } ${className}`}
    >
      <div className="relative flex min-w-0 flex-1 items-center h-full md:flex-col md:items-stretch md:h-auto md:min-h-0">
        <nav
          ref={navRef}
          aria-label="Workspaces"
          onScroll={measure}
          className="flex items-center h-full min-w-0 font-mono text-xs font-semibold overflow-x-auto scroll-px-8 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden md:flex-col md:items-stretch md:h-auto md:flex-1 md:overflow-y-auto md:overflow-x-hidden md:py-2.5 md:px-1.5 md:gap-2.5"
        >
          {NAV_GROUPS.map((group, gi) => {
            const allOverflow = group.items.every((i) => i.overflow);
            const isSystem = group.id === "system";
            const pillarActive = isPillarActive(group.id, workspaceMode);
            return (
              <div
                key={group.id}
                role="group"
                aria-label={group.label}
                className={`${allOverflow ? SHOW_ON_WIDE : "flex"} flex-none items-center h-full gap-3 sm:gap-4 md:flex-col md:items-stretch md:h-auto md:gap-0.5 ${
                  isSystem
                    ? "max-md:ml-auto max-md:pl-4 max-md:border-l max-md:border-zinc-800 md:mt-auto md:pt-2 md:border-t md:border-zinc-800/80"
                    : gi > 0
                      ? "max-md:ml-4 max-md:pl-4 max-md:border-l max-md:border-zinc-800 md:pt-2 md:border-t md:border-zinc-800/60"
                      : ""
                }`}
              >
                {/* Refined Institutional 4-Pillars header badge */}
                <span
                  role="link"
                  tabIndex={0}
                  onClick={() => navigate(PILLAR_MODE_MAP[group.id] ?? "trades")}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      navigate(PILLAR_MODE_MAP[group.id] ?? "trades");
                    }
                  }}
                  data-pillar={group.shortLabel ?? group.label}
                  data-active={pillarActive}
                  aria-label={group.shortLabel ?? group.label}
                  title={`Jump to ${group.pillarTitle ?? group.label}`}
                  className={`hidden md:inline-flex items-center uppercase tracking-widest select-none rounded border transition-colors cursor-pointer ${
                    collapsed
                      ? "justify-center px-0.5 py-0.5 mb-0.5 text-[8px] truncate max-w-full"
                      : "px-2 py-0.5 mb-0.5 text-2xs"
                  } ${
                    pillarActive
                      ? "text-cyan-400 bg-cyan-950/60 border-cyan-500/50 shadow-xs font-bold"
                      : "text-zinc-400 bg-zinc-900/60 border-white/5 hover:text-zinc-200 hover:border-zinc-700"
                  }`}
                >
                  {collapsed ? (group.shortLabel ?? group.label).slice(0, 3) : (group.shortLabel ?? group.label)}
                </span>
                {group.items.map((tab) => {
                  const active = isActiveTab(tab, workspaceMode, urlView);
                  const Icon = TAB_ICONS[tab.label] ?? Activity;
                  return (
                    <button
                      key={`${tab.mode}-${tab.view ?? ""}`}
                      type="button"
                      onClick={() => navigate(tab.mode, tab.view)}
                      aria-current={active ? "page" : undefined}
                      title={`${tab.label} — ${tab.description}`}
                      className={`relative overflow-hidden h-full flex items-center gap-2 max-md:border-b-2 max-md:px-1 whitespace-nowrap transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-500/60 md:h-8 md:w-full md:rounded-lg ${
                        collapsed ? "md:justify-center md:px-0" : "md:justify-start md:px-2.5"
                      } ${tab.overflow ? SHOW_ON_WIDE : "flex"} ${
                        active
                          ? "max-md:border-cyan-500 text-cyan-400 md:bg-cyan-950/70 md:text-cyan-300 md:border md:border-cyan-500/40 md:shadow-xs"
                          : "border-transparent text-zinc-400 hover:text-zinc-200 max-md:hover:border-zinc-700 md:hover:bg-zinc-800/60"
                      }`}
                    >
                      <Icon className="h-3.5 w-3.5 flex-none" aria-hidden="true" /><span className={collapsed ? "md:sr-only" : "truncate"}>{tab.label}</span>
                    </button>
                  );
                })}
              </div>
            );
          })}
        </nav>
        {/* Mouse affordance on horizontal strip (< md) */}
        {edges.left && (
          <button
            type="button"
            tabIndex={-1}
            aria-hidden="true"
            data-testid="nav-scroll-left"
            onClick={() => scrollBy(-1)}
            className="md:hidden absolute inset-y-0 left-0 flex w-8 items-center justify-start bg-gradient-to-r from-zinc-950 via-zinc-950/90 to-transparent text-zinc-400 hover:text-zinc-100"
          >
            <ChevronLeft className="h-4 w-4" aria-hidden="true" />
          </button>
        )}
        {edges.right && (
          <button
            type="button"
            tabIndex={-1}
            aria-hidden="true"
            data-testid="nav-scroll-right"
            onClick={() => scrollBy(1)}
            className="md:hidden absolute inset-y-0 right-0 flex w-8 items-center justify-end bg-gradient-to-l from-zinc-950 via-zinc-950/90 to-transparent text-zinc-400 hover:text-zinc-100"
          >
            <ChevronRight className="h-4 w-4" aria-hidden="true" />
          </button>
        )}
      </div>

      {/* Desktop Sidebar Footer: Operator Sign-In + Pixel Office quick-launch + Sidebar Collapse Toggle */}
      <div className="hidden md:flex md:flex-col md:gap-1 md:p-1.5 md:border-t md:border-zinc-800/80">
        {sessionStatus === "anonymous" && onSignIn && (
          <button
            type="button"
            onClick={onSignIn}
            title="Sign in as Operator"
            className={`relative overflow-hidden flex items-center gap-2 h-8 w-full rounded-lg border border-amber-500/40 bg-amber-950/40 text-amber-300 hover:bg-amber-900/50 font-mono text-xs font-semibold transition-all ${
              collapsed ? "justify-center px-0" : "justify-start px-2.5"
            }`}
          >
            <Lock className="h-3.5 w-3.5 flex-none text-amber-400" aria-hidden="true" />
            {!collapsed && <span className="truncate">Operator Sign in</span>}
          </button>
        )}
        {sessionStatus === "authenticated" && (
          <div
            title="Operator session active"
            className={`flex items-center gap-2 h-7 w-full rounded-lg font-mono text-2xs text-cyan-400 ${
              collapsed ? "justify-center px-0" : "justify-start px-2.5"
            }`}
          >
            <ShieldCheck className="h-3.5 w-3.5 flex-none text-cyan-400" aria-hidden="true" />
            {!collapsed && <span className="truncate">Operator active</span>}
          </div>
        )}

        <button
          type="button"
          onClick={() => navigate("debates", "pixel-office")}
          title="Pixel Office — Live 2D Quant Agent Floor"
          className={`relative overflow-hidden flex items-center gap-2 h-8 w-full rounded-lg font-mono text-xs font-semibold transition-all ${
            collapsed ? "justify-center px-0" : "justify-start px-2.5"
          } ${
            isPixelOffice
              ? "bg-cyan-950/80 text-cyan-300 border border-cyan-500/50 shadow-xs"
              : "text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800/60"
          }`}
        >
          <Gamepad2 className="h-3.5 w-3.5 flex-none text-cyan-400" aria-hidden="true" />
          {!collapsed && <span className="truncate">Pixel Office</span>}
        </button>

        {onToggleCollapse && (
          <button
            type="button"
            onClick={onToggleCollapse}
            aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            title={collapsed ? "Expand sidebar" : "Collapse sidebar (auto-collapses on Terminal)"}
            className={`flex items-center gap-2 h-7 w-full rounded-lg font-mono text-2xs text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800/60 transition-colors ${
              collapsed ? "justify-center px-0" : "justify-between px-2.5"
            }`}
          >
            {!collapsed && <span className="truncate">Collapse rail</span>}
            {collapsed ? (
              <PanelLeftOpen className="h-3.5 w-3.5 flex-none" aria-hidden="true" />
            ) : (
              <PanelLeftClose className="h-3.5 w-3.5 flex-none" aria-hidden="true" />
            )}
          </button>
        )}
      </div>

      {/* Beside the strip on < md: stays visible for narrow mobile viewports */}
      <OverflowMenu workspaceMode={workspaceMode} urlView={urlView} navigate={navigate} />
    </div>
  );
}

/**
 * The secondary destinations, below 1100px. The button sits in the scrolling strip; the menu is portalled to the
 * body so the strip's overflow cannot clip it. It closes on outside press, Escape, and any scroll (it is fixed to
 * the button's position when it opens).
 */
function OverflowMenu({
  workspaceMode,
  urlView,
  navigate,
}: {
  workspaceMode: WorkspaceMode;
  urlView: string | null;
  navigate: (mode: WorkspaceMode, view?: string | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const [anchor, setAnchor] = useState<{ top: number; right: number } | null>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const anyActive = OVERFLOW_ITEMS.some((tab) => isActiveTab(tab, workspaceMode, urlView));

  const toggle = () => {
    if (!open && buttonRef.current) {
      const r = buttonRef.current.getBoundingClientRect();
      setAnchor({ top: r.bottom + 4, right: Math.max(0, window.innerWidth - r.right) });
    }
    setOpen((o) => !o);
  };

  useEffect(() => {
    if (!open) return;
    const close = () => setOpen(false);
    const onPointer = (e: MouseEvent) => {
      const target = e.target as Node;
      if (!buttonRef.current?.contains(target) && !menuRef.current?.contains(target)) close();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    document.addEventListener("mousedown", onPointer);
    document.addEventListener("keydown", onKey);
    document.addEventListener("scroll", close, true);
    window.addEventListener("resize", close);
    return () => {
      document.removeEventListener("mousedown", onPointer);
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("scroll", close, true);
      window.removeEventListener("resize", close);
    };
  }, [open]);

  return (
    <div className={`flex h-full flex-none items-center ${HIDE_ON_WIDE}`}>
      <button
        ref={buttonRef}
        type="button"
        aria-label="More workspaces"
        aria-haspopup="menu"
        aria-expanded={open}
        title="More workspaces"
        onClick={toggle}
        className={`flex h-full items-center border-b-2 px-2 transition-colors ${
          anyActive ? "border-cyan-500 text-cyan-400" : "border-transparent text-zinc-400 hover:text-zinc-200"
        }`}
      >
        <MoreHorizontal className="h-4 w-4" aria-hidden="true" />
      </button>
      {open &&
        anchor &&
        createPortal(
          <div
            ref={menuRef}
            role="menu"
            aria-label="More workspaces"
            style={{ position: "fixed", top: anchor.top, right: anchor.right }}
            className="z-50 min-w-40 rounded border border-white/10 bg-zinc-950 py-1 shadow-xl"
          >
            {OVERFLOW_ITEMS.map((tab) => {
              const active = isActiveTab(tab, workspaceMode, urlView);
              const Icon = TAB_ICONS[tab.label] ?? Activity;
              return (
                <button
                  key={`${tab.mode}-${tab.view ?? ""}`}
                  type="button"
                  role="menuitem"
                  aria-current={active ? "page" : undefined}
                  title={`${tab.label} — ${tab.description}`}
                  onClick={() => {
                    navigate(tab.mode, tab.view);
                    setOpen(false);
                  }}
                  className={`flex items-center gap-2 w-full px-3 py-2 text-left font-mono text-xs ${
                    active ? "text-cyan-400" : "text-zinc-300 hover:bg-zinc-900"
                  }`}
                >
                  <Icon className="h-3.5 w-3.5 flex-none" aria-hidden="true" /><span>{tab.label}</span>
                </button>
              );
            })}
          </div>,
          document.body,
        )}
    </div>
  );
}
