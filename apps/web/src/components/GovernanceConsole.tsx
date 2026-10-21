"use client";

import { useCallback, useEffect, useId, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";
import { toast } from "sonner";
import {
  ShieldAlert,
  ShieldCheck,
  Power,
  Flame,
  User,
  RefreshCw,
  RefreshCcw,
  AlertTriangle,
  Layers,
  FileCheck,
  Sliders,
  Database,
  Brain,
  Gauge,
  Lock,
  X,
} from "lucide-react";

import { fetchApi } from "@/lib/api";
import { dispatchSwitchWorkspace, parseView } from "@/lib/navigation";
import { MANDATE_UPDATED_EVENT } from "@/lib/mission/events";
import { useMandateStore } from "@/lib/stores/useMandateStore";
import { useAutonomyStore } from "@/lib/stores/useAutonomyStore";
import { useShadowStore } from "@/lib/stores/useShadowStore";
import { useSessionStore } from "@/lib/stores/useSessionStore";
import { useKillSwitch } from "@/lib/stores/useKillSwitchStore";
import {
  refreshPortfolio,
  usePortfolioStore,
  usePortfolioSync,
} from "@/lib/stores/usePortfolioStore";
import { closePosition, postCommand } from "@/lib/portfolio/actions";
import { isHeldPosition } from "@/lib/portfolio/metrics";

import { MandateCard } from "@/components/mission/MandateCard";
import { MandateEditor } from "@/components/mission/MandateEditor";
import { ApprovalQueue } from "@/components/mission/ApprovalQueue";
import { AutonomyEvals } from "@/components/mission/AutonomyEvals";
import ShadowPanel from "@/components/mission/ShadowPanel";
import { MissionBanners } from "@/components/mission/MissionBanners";
import { MissionHealth } from "@/components/mission/MissionHealth";

import { KillSwitchPanel } from "@/components/operator/KillSwitchPanel";
import { EnforcedLimitsCard } from "@/components/operator/EnforcedLimitsCard";
import { RiskLimitsForm } from "@/components/operator/RiskLimitsForm";
import { ReconciliationLedger } from "@/components/operator/ReconciliationLedger";
import { OperatorLoginModal } from "@/components/operator/OperatorLoginModal";

export type GovernanceView =
  | "overview"
  | "mandates"
  | "approvals"
  | "limits"
  | "reconciliation"
  | "autonomy";

interface TabMeta {
  id: GovernanceView;
  label: string;
  shortLabel: string;
  icon: React.ComponentType<{ className?: string }>;
  description: string;
}

const TABS: TabMeta[] = [
  {
    id: "overview",
    label: "Flight Deck Overview",
    shortLabel: "Overview",
    icon: Gauge,
    description: "System health, active mandates summary, and master execution status",
  },
  {
    id: "mandates",
    label: "Trading Mandates",
    shortLabel: "Mandates",
    icon: Layers,
    description: "Mandate parameters, capital budgets, and lifecycle promotion",
  },
  {
    id: "approvals",
    label: "Approval Queue",
    shortLabel: "Approvals",
    icon: FileCheck,
    description: "Human-in-the-loop trade sign-offs with live countdown timers",
  },
  {
    id: "limits",
    label: "Risk Limits & Overrides",
    shortLabel: "Risk Limits",
    icon: Sliders,
    description: "Enforced dispatcher ceilings with audited TOTP override capabilities",
  },
  {
    id: "reconciliation",
    label: "Exchange Reconciliation",
    shortLabel: "Reconciliation",
    icon: Database,
    description: "Real-time ledger audit, discrepancy detection, and settled balances",
  },
  {
    id: "autonomy",
    label: "Autonomy & Evals",
    shortLabel: "Autonomy & Evals",
    icon: Brain,
    description: "Multi-factor statistical gate checks and shadow model routing",
  },
];

let hydrated = false;

export interface GovernanceConsoleProps {
  isActive?: boolean;
  initialView?: string;
}

/**
 * Unified Institutional Governance & Risk Flight Deck
 * Consolidates Mission Control and Operator Console into a single command plane.
 */
export function GovernanceConsole({
  isActive = true,
  initialView,
}: GovernanceConsoleProps) {
  // Synchronize background portfolio data (positions snapshot stream & 3s polling)
  usePortfolioSync(3000);

  // Search parameters for URL view synchronization
  const searchParams = useSearchParams();
  const urlParamView = useMemo(() => {
    try {
      const v = searchParams?.get("view");
      return (parseView("governance", v) as GovernanceView | null) ?? null;
    } catch {
      return null;
    }
  }, [searchParams]);

  // Priority: URL parameter -> initialView prop -> default to "overview"
  const defaultTab: GovernanceView = useMemo(() => {
    if (urlParamView) return urlParamView;
    if (initialView) {
      const parsed = parseView("governance", initialView) as GovernanceView | null;
      if (parsed) return parsed;
    }
    return "overview";
  }, [urlParamView, initialView]);

  const [activeTab, setActiveTab] = useState<GovernanceView>(defaultTab);

  // Sync tab with URL view updates
  useEffect(() => {
    if (urlParamView && urlParamView !== activeTab) {
      setActiveTab(urlParamView);
    }
  }, [urlParamView, activeTab]);

  // Session state
  const sessionStatus = useSessionStore((s) => s.status);
  const username = useSessionStore((s) => s.username);
  const [showLoginModal, setShowLoginModal] = useState(false);

  // Mandate state
  const {
    mandates,
    mandatesOk,
    approvals,
    status,
    loaded,
    error,
    refresh: refreshMandates,
    selectedMandateId,
    selectMandate,
  } = useMandateStore();

  // Portfolio state
  const positions = usePortfolioStore((s) => s.positions);
  const killSwitch = usePortfolioStore((s) => s.killSwitch);
  const openPositions = useMemo(() => positions.filter(isHeldPosition), [positions]);

  // Dedicated kill-switch hook for authoritative PAPER status
  const paperKs = useKillSwitch("PAPER", "PAPER-ACCOUNT-001");

  // Rehydrate persisted stores on initial mount
  useEffect(() => {
    if (!hydrated) {
      hydrated = true;
      void useMandateStore.persist.rehydrate();
      void useAutonomyStore.persist.rehydrate();
      void useShadowStore.persist.rehydrate();
    }
  }, []);

  // Ensure session is verified
  useEffect(() => {
    void useSessionStore.getState().check();
  }, []);

  // Refresh handlers
  const reload = useCallback(() => {
    void refreshMandates();
    void paperKs.refresh();
    void refreshPortfolio();
  }, [refreshMandates, paperKs]);

  const reloadNow = useCallback(() => {
    void refreshMandates({ after: performance.now() });
    void paperKs.refresh();
    void refreshPortfolio();
  }, [refreshMandates, paperKs]);

  // Polling & event listener for updates
  useEffect(() => {
    if (!isActive) return;
    reload();
    const interval = setInterval(reload, 5000);
    const onUpdated = (e: Event) => void refreshMandates({ after: (e as any).timeStamp });
    window.addEventListener(MANDATE_UPDATED_EVENT, onUpdated);
    return () => {
      clearInterval(interval);
      window.removeEventListener(MANDATE_UPDATED_EVENT, onUpdated);
    };
  }, [isActive, reload, refreshMandates]);

  // Modal states for Master Emergency Controls
  const [showFlattenModal, setShowFlattenModal] = useState(false);
  const [isFlattening, setIsFlattening] = useState(false);

  const [showTripModal, setShowTripModal] = useState(false);
  const [tripReason, setTripReason] = useState(
    "Manual operator emergency trip via Governance Flight Deck"
  );
  const [isTripping, setIsTripping] = useState(false);

  const [showResetModal, setShowResetModal] = useState(false);
  const [resetReason, setResetReason] = useState("");
  const [resetPassword, setResetPassword] = useState("");
  const [isResetting, setIsResetting] = useState(false);
  const [resetError, setResetError] = useState("");

  const tripReasonId = useId();
  const resetReasonId = useId();
  const resetPasswordId = useId();

  // Engine latched status calculation
  const isLatched = paperKs.view === "latched" || killSwitch.is_latched;
  const isArmed = paperKs.view === "armed" && !killSwitch.is_latched;

  // Pending approvals count for badges
  const pendingApprovalsCount = useMemo(
    () => approvals.filter((a) => a.status === "PENDING").length,
    [approvals]
  );

  // Tab navigation handler
  const handleTabSelect = (tabId: GovernanceView) => {
    setActiveTab(tabId);
    if (tabId === "overview" || tabId === "mandates" || tabId === "autonomy") {
      useMandateStore.getState().setView(tabId);
    }
    dispatchSwitchWorkspace({ mode: "governance", view: tabId });
  };

  // Emergency action handlers
  const handleConfirmFlatten = async () => {
    if (openPositions.length === 0) {
      toast.info("No open positions to flatten.");
      setShowFlattenModal(false);
      return;
    }
    setIsFlattening(true);
    try {
      const results = await Promise.allSettled(
        openPositions.map((pos) => closePosition(pos.instance_id, pos.instrument_id))
      );
      const succeeded = results.filter((r) => r.status === "fulfilled" && r.value).length;
      if (succeeded === openPositions.length) {
        toast.success(`Successfully flattened all ${succeeded} position(s).`);
      } else {
        toast.warning(
          `Flattened ${succeeded} of ${openPositions.length} positions. Check ledger.`
        );
      }
      await refreshPortfolio();
      setShowFlattenModal(false);
    } catch (err: any) {
      toast.error(err?.message || "Emergency flatten failed.");
    } finally {
      setIsFlattening(false);
    }
  };

  const handleConfirmTrip = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsTripping(true);
    try {
      await fetchApi("/control/kill-switch/trip", {
        method: "POST",
        headers: { "X-CSRF-Token": localStorage.getItem("csrf_token") || "" },
        body: JSON.stringify({
          reason: tripReason,
          environment: "PAPER",
          account_id: "PAPER-ACCOUNT-001",
        }),
      });
      await postCommand("/paper/kill-switch", {
        reason: tripReason,
        operator: username || "operator",
      });
      await paperKs.refresh();
      await refreshPortfolio();
      toast.error("Execution Kill Switch Tripped: All paper execution is halted.");
      setShowTripModal(false);
    } catch (err: any) {
      toast.error(err?.message || "Failed to trip kill switch.");
    } finally {
      setIsTripping(false);
    }
  };

  const handleConfirmReset = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsResetting(true);
    setResetError("");
    try {
      await fetchApi("/control/kill-switch/reset", {
        method: "POST",
        headers: { "X-CSRF-Token": localStorage.getItem("csrf_token") || "" },
        body: JSON.stringify({
          step_up_password: resetPassword,
          reason: resetReason,
          environment: "PAPER",
          account_id: "PAPER-ACCOUNT-001",
        }),
      });
      await postCommand("/paper/kill-switch/reset", {
        operator: username || "operator",
      });
      toast.success(
        "Reset protocol initiated. Paper engine will re-arm when no open orders remain."
      );
      setShowResetModal(false);
      setResetPassword("");
      setResetReason("");
      await paperKs.refresh();
      await refreshPortfolio();
    } catch (err: any) {
      setResetError(err?.message || "Reset request refused. Check step-up credentials.");
    } finally {
      setIsResetting(false);
    }
  };

  return (
    <div
      data-testid="governance-console"
      className="flex h-full min-h-0 flex-col overflow-y-auto bg-zinc-950 p-4 text-zinc-100"
    >
      {/* Compatibility test identifier for existing mission-control tests */}
      <div data-testid="mission-control" className="sr-only" aria-hidden="true" />

      {/* =========================================================================
          1. MASTER EMERGENCY TOP BAR
         ========================================================================= */}
      <header
        aria-label="Master Emergency Top Bar"
        className="mb-4 rounded-lg border border-zinc-800 bg-zinc-900/60 p-3 shadow-md backdrop-blur-sm"
      >
        <div className="flex flex-wrap items-center justify-between gap-3">
          {/* Left: Brand & Engine Status Badge */}
          <div className="flex flex-wrap items-center gap-3">
            <div className="flex items-center gap-2">
              <ShieldAlert className="h-5 w-5 text-cyan-400" aria-hidden="true" />
              <div>
                <h1 className="font-mono text-xs font-bold uppercase tracking-wider text-zinc-200">
                  Governance Flight Deck
                </h1>
                <p className="font-mono text-2xs text-zinc-500">
                  Paper Engine · Logical Account: PAPER-ACCOUNT-001
                </p>
              </div>
            </div>

            {/* Engine Status Badge */}
            {isLatched ? (
              <div
                role="status"
                aria-label="Engine Status: Execution Halted"
                className="flex items-center gap-2 rounded-full border border-rose-600 bg-rose-950/80 px-3 py-1 font-mono text-2xs font-black tracking-widest text-rose-300 shadow-[0_0_12px_rgba(225,29,72,0.4)] animate-pulse"
              >
                <span className="h-2 w-2 rounded-full bg-rose-500 animate-ping" />
                <span>LATCHED · EXECUTION HALTED</span>
              </div>
            ) : isArmed ? (
              <div
                role="status"
                aria-label="Engine Status: Armed & Nominal"
                className="flex items-center gap-2 rounded-full border border-emerald-600/80 bg-emerald-950/80 px-3 py-1 font-mono text-2xs font-bold tracking-wider text-emerald-400"
              >
                <span className="h-2 w-2 rounded-full bg-emerald-400 shadow-[0_0_8px_rgba(52,211,153,0.8)]" />
                <span>ARMED · EXECUTION NOMINAL</span>
              </div>
            ) : (
              <div
                role="status"
                aria-label="Engine Status: Checking or Unknown"
                className="flex items-center gap-2 rounded-full border border-zinc-700 bg-zinc-900 px-3 py-1 font-mono text-2xs font-medium text-zinc-300"
              >
                <span className="h-2 w-2 rounded-full bg-amber-400" />
                <span>
                  {paperKs.view === "checking"
                    ? "CHECKING · ENGINE SYNC"
                    : paperKs.view === "signed-out"
                      ? "STANDBY · AUTH REQUIRED"
                      : "UNKNOWN · ENGINE UNREACHABLE"}
                </span>
              </div>
            )}
          </div>

          {/* Right: Master Actions & Operator Session Pill */}
          <div className="flex flex-wrap items-center gap-2.5">
            {/* Master Paper Kill Switch Button */}
            {isLatched ? (
              <button
                type="button"
                onClick={() => {
                  if (sessionStatus === "anonymous") {
                    setShowLoginModal(true);
                  } else {
                    setShowResetModal(true);
                  }
                }}
                className="flex items-center gap-1.5 rounded bg-amber-600 hover:bg-amber-500 px-3 py-1.5 font-mono text-2xs font-bold uppercase tracking-wider text-zinc-950 transition-colors shadow-[0_0_12px_rgba(217,119,6,0.25)]"
                aria-label="Reset execution kill switch"
              >
                <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
                <span>Reset Switch</span>
              </button>
            ) : (
              <button
                type="button"
                onClick={() => {
                  if (sessionStatus === "anonymous") {
                    setShowLoginModal(true);
                  } else {
                    setShowTripModal(true);
                  }
                }}
                className="flex items-center gap-1.5 rounded bg-rose-600 hover:bg-rose-500 px-3 py-1.5 font-mono text-2xs font-bold uppercase tracking-wider text-white transition-colors shadow-[0_0_12px_rgba(225,29,72,0.3)]"
                aria-label="Trip master paper kill switch"
              >
                <Power className="h-3.5 w-3.5" aria-hidden="true" />
                <span>Trip Kill Switch</span>
              </button>
            )}

            {/* Emergency Flatten All Positions Button */}
            <button
              type="button"
              onClick={() => {
                if (sessionStatus === "anonymous") {
                  setShowLoginModal(true);
                } else {
                  setShowFlattenModal(true);
                }
              }}
              disabled={openPositions.length === 0}
              className="flex items-center gap-1.5 rounded border border-orange-600/70 bg-orange-950/40 hover:bg-orange-900/60 disabled:opacity-40 disabled:hover:bg-orange-950/40 px-3 py-1.5 font-mono text-2xs font-bold uppercase tracking-wider text-orange-300 transition-colors"
              aria-label="Flatten all open positions"
              title={
                openPositions.length === 0
                  ? "No open positions to flatten"
                  : `Liquidate ${openPositions.length} open position(s)`
              }
            >
              <Flame className="h-3.5 w-3.5 text-orange-400" aria-hidden="true" />
              <span>Flatten All ({openPositions.length})</span>
            </button>

            {/* Operator Session Pill */}
            {sessionStatus === "authenticated" ? (
              <div
                data-testid="operator-session-pill"
                className="flex items-center gap-1.5 rounded-full border border-cyan-800/80 bg-cyan-950/40 px-3 py-1 font-mono text-2xs font-bold text-cyan-300"
              >
                <ShieldCheck className="h-3.5 w-3.5 text-cyan-400" aria-hidden="true" />
                <span>OPERATOR: {username || "authenticated"}</span>
              </div>
            ) : sessionStatus === "checking" ? (
              <div className="flex items-center gap-1.5 rounded-full border border-zinc-800 bg-zinc-900 px-3 py-1 font-mono text-2xs text-zinc-400">
                <RefreshCcw className="h-3 w-3 animate-spin text-zinc-500" aria-hidden="true" />
                <span>Verifying…</span>
              </div>
            ) : (
              <button
                type="button"
                data-testid="operator-session-pill"
                onClick={() => setShowLoginModal(true)}
                className="flex items-center gap-1.5 rounded-full border border-amber-600/70 bg-amber-950/40 hover:bg-amber-900/50 px-3 py-1 font-mono text-2xs font-bold text-amber-300 transition-colors"
              >
                <User className="h-3.5 w-3.5 text-amber-400" aria-hidden="true" />
                <span>Anonymous · Sign In</span>
              </button>
            )}

            {/* Quick Synchronize Button */}
            <button
              type="button"
              onClick={reloadNow}
              aria-label="Refresh governance status"
              title="Refresh flight deck telemetry"
              className="inline-flex h-7 w-7 items-center justify-center rounded text-zinc-500 hover:text-zinc-200 transition-colors"
            >
              <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
            </button>
          </div>
        </div>
      </header>

      {/* Desk Operator Alert Banner (when unauthenticated) */}
      {sessionStatus === "anonymous" && (
        <div
          role="region"
          aria-label="Unauthenticated Operator Alert"
          className="mb-4 flex items-center justify-between rounded-lg border border-amber-800/80 bg-amber-950/40 p-3 text-xs text-amber-200"
        >
          <div className="flex items-center gap-2.5">
            <Lock className="h-4 w-4 flex-none text-amber-400" aria-hidden="true" />
            <div>
              <span className="font-bold uppercase tracking-wider font-mono text-2xs">
                Unauthenticated Operator Session
              </span>
              <p className="text-2xs text-amber-300/80 mt-0.5">
                Viewing flight deck in read-only mode. Tripping kill switches, updating risk limits, approving transactions, or resuming mandates requires operator credentials.
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={() => setShowLoginModal(true)}
            className="shrink-0 rounded bg-cyan-600 hover:bg-cyan-500 px-3 py-1 font-mono text-2xs font-bold uppercase text-zinc-950 transition-colors"
          >
            Sign In Now
          </button>
        </div>
      )}

      {/* =========================================================================
          2. SUB-VIEW TABS NAVIGATION
         ========================================================================= */}
      <div className="mb-4 flex items-center justify-between border-b border-zinc-800 pb-2">
        <nav
          role="tablist"
          aria-label="Governance flight deck views"
          className="flex flex-wrap gap-1 font-mono text-2xs"
        >
          {TABS.map((tab) => {
            const Icon = tab.icon;
            const isSelected = activeTab === tab.id;
            const isApprovalsTab = tab.id === "approvals";

            return (
              <button
                key={tab.id}
                role="tab"
                aria-selected={isSelected}
                aria-controls={`governance-panel-${tab.id}`}
                id={`governance-tab-${tab.id}`}
                onClick={() => handleTabSelect(tab.id)}
                className={`relative flex items-center gap-1.5 rounded px-3 py-1.5 font-bold transition-colors ${
                  isSelected
                    ? "bg-cyan-900/50 text-cyan-300 border border-cyan-800/60"
                    : "text-zinc-400 hover:bg-zinc-900 hover:text-zinc-200"
                }`}
                title={tab.description}
              >
                <Icon className="h-3.5 w-3.5" />
                <span>{tab.shortLabel}</span>

                {/* Pending approvals badge */}
                {isApprovalsTab && pendingApprovalsCount > 0 && (
                  <span
                    className="ml-1 inline-flex items-center justify-center rounded-full bg-amber-500 px-1.5 py-0.2 font-mono text-3xs font-extrabold text-zinc-950 animate-pulse"
                    aria-label={`${pendingApprovalsCount} pending approvals`}
                  >
                    {pendingApprovalsCount}
                  </span>
                )}
              </button>
            );
          })}
        </nav>
      </div>

      {/* Global Status & Banner Alerts */}
      <MissionBanners status={status} mandates={mandates} />
      {error && (
        <p role="alert" className="mt-2 font-mono text-2xs text-zinc-400">
          {error}
        </p>
      )}

      {/* =========================================================================
          3. SUB-VIEW PANELS CONTENT
         ========================================================================= */}
      <div className="mt-2 min-h-0 flex-1">
        {/* TAB 1: OVERVIEW — FLIGHT DECK OVERVIEW */}
        {activeTab === "overview" && (
          <div
            id="governance-panel-overview"
            role="tabpanel"
            aria-labelledby="governance-tab-overview"
            className="space-y-4"
          >
            {/* Health Status Telemetry */}
            <MissionHealth status={status} />

            {/* Quick Link to Pending Approvals */}
            {pendingApprovalsCount > 0 && (
              <div
                role="region"
                aria-label="Pending Approvals Alert"
                className="flex items-center justify-between rounded-lg border border-amber-600/70 bg-amber-950/40 p-3.5 text-xs text-amber-200"
              >
                <div className="flex items-center gap-2.5">
                  <AlertTriangle className="h-4 w-4 flex-none text-amber-400 animate-pulse" />
                  <div>
                    <span className="font-bold uppercase tracking-wider font-mono text-2xs">
                      Human-in-the-Loop Action Required: {pendingApprovalsCount} Pending Approval{pendingApprovalsCount > 1 ? "s" : ""}
                    </span>
                    <p className="text-2xs text-amber-300/80 mt-0.5">
                      Autonomous execution decisions are waiting for operator sign-off before entering or modifying positions.
                    </p>
                  </div>
                </div>
                <button
                  type="button"
                  onClick={() => handleTabSelect("approvals")}
                  className="shrink-0 rounded bg-amber-600 hover:bg-amber-500 px-3 py-1.5 font-mono text-2xs font-bold uppercase text-zinc-950 transition-colors"
                >
                  Review Approvals &rarr;
                </button>
              </div>
            )}

            {/* Flight Deck Grid */}
            <div className="grid grid-cols-1 gap-6 xl:grid-cols-12">
              {/* Left Column: Active Mandates & Risk Limits */}
              <div className="space-y-4 xl:col-span-7">
                {/* Active Mandates Header */}
                <div className="flex items-center justify-between">
                  <h3 className="font-mono text-xs font-bold uppercase tracking-widest text-zinc-300">
                    Active Trading Mandates ({mandates.length})
                  </h3>
                  <button
                    type="button"
                    onClick={() => handleTabSelect("mandates")}
                    className="font-mono text-2xs text-cyan-400 hover:text-cyan-300 transition-colors"
                  >
                    Open Editor &rarr;
                  </button>
                </div>

                {loaded && mandatesOk === false && mandates.length === 0 && (
                  <p
                    role="status"
                    className="rounded border border-amber-800/60 p-3 font-mono text-2xs text-amber-200"
                  >
                    Mandates could not be loaded, so the AI's authority is unknown here, not zero.
                  </p>
                )}
                {loaded && mandatesOk === true && mandates.length === 0 && (
                  <p className="rounded border border-zinc-800 p-3 font-mono text-2xs text-zinc-500">
                    No active trading mandates. The AI cannot place any orders until you create one; every mandate starts at L1 Copilot.
                  </p>
                )}

                {/* Mandate Cards Grid */}
                <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                  {mandates.map((m) => (
                    <MandateCard key={m.id} mandate={m} onChanged={reload} />
                  ))}
                </div>

                {status?.account && (
                  <p
                    className="font-mono text-2xs text-zinc-500"
                    data-testid="account-backstop"
                  >
                    Account drawdown{" "}
                    {status.account.drawdown_pct == null
                      ? "—"
                      : `${Number(status.account.drawdown_pct).toFixed(2)}%`}{" "}
                    of the {status.account.backstop_pct}% backstop (latches execution if breached).
                  </p>
                )}

                {/* Enforced Limits Card with Trigger to Full Limits */}
                <div className="pt-2">
                  <EnforcedLimitsCard />
                </div>
              </div>

              {/* Right Column: Kill Switches & Approvals Snapshot */}
              <div className="space-y-4 xl:col-span-5">
                {/* Pending Approvals Summary */}
                <div className="space-y-2">
                  <div className="flex items-center justify-between">
                    <h3 className="font-mono text-xs font-bold uppercase tracking-widest text-zinc-300">
                      Approval Queue
                    </h3>
                    <button
                      type="button"
                      onClick={() => handleTabSelect("approvals")}
                      className="font-mono text-2xs text-cyan-400 hover:text-cyan-300 transition-colors"
                    >
                      Full Queue ({approvals.length}) &rarr;
                    </button>
                  </div>
                  <ApprovalQueue approvals={approvals} onDecided={reload} />
                </div>

                {/* Paper & Demo Kill Switches */}
                <div className="space-y-3">
                  <h3 className="font-mono text-xs font-bold uppercase tracking-widest text-zinc-300">
                    Execution Kill Switches
                  </h3>
                  <KillSwitchPanel environment="PAPER" accountId="PAPER-ACCOUNT-001" />
                  <KillSwitchPanel environment="DEMO" accountId="DEMO-ACCOUNT-001" />
                </div>
              </div>
            </div>
          </div>
        )}

        {/* TAB 2: MANDATES — TRADING MANDATES & EDITOR */}
        {activeTab === "mandates" && (
          <div
            id="governance-panel-mandates"
            role="tabpanel"
            aria-labelledby="governance-tab-mandates"
            className="space-y-4"
          >
            <div className="rounded border border-zinc-800 bg-zinc-900/40 p-4">
              <h3 className="font-mono text-xs font-bold uppercase tracking-wider text-zinc-200">
                Institutional Mandate Lifecycle & Governance
              </h3>
              <p className="mt-1 font-mono text-2xs text-zinc-400 leading-relaxed">
                Every trading mandate defines the strict risk boundaries and execution perimeter for AI strategies.
                Mandates adhere to progressive autonomy levels: <strong>L1 Copilot</strong> (manual sign-off required for all orders),
                <strong>L2 Supervised</strong> (autonomous entry within daily limits; vetoable exits), and <strong>L3 Autonomous</strong>
                (fully autonomous trading governed by automated risk backstops). Promotion between levels requires satisfying
                multi-factor statistical gates and operator confirmation with TOTP.
              </p>
            </div>

            <MandateEditor
              mandates={mandates}
              selectedId={selectedMandateId}
              onSelect={selectMandate}
              onSaved={reload}
            />
          </div>
        )}

        {/* TAB 3: APPROVALS — HUMAN-IN-THE-LOOP APPROVAL QUEUE */}
        {activeTab === "approvals" && (
          <div
            id="governance-panel-approvals"
            role="tabpanel"
            aria-labelledby="governance-tab-approvals"
            className="space-y-4"
          >
            <div className="rounded border border-zinc-800 bg-zinc-900/40 p-4 flex items-center justify-between">
              <div>
                <h3 className="font-mono text-xs font-bold uppercase tracking-wider text-zinc-200">
                  Human-in-the-Loop Approval Queue (HITL)
                </h3>
                <p className="mt-1 font-mono text-2xs text-zinc-400">
                  Real-time approval stream for proposed trade entries and risk alterations. Decisions are enforced with strict deadline timeouts.
                </p>
              </div>
              <div className="font-mono text-xs text-zinc-400">
                Pending: <span className="text-amber-400 font-bold">{pendingApprovalsCount}</span>
              </div>
            </div>

            <div className="max-w-4xl">
              <ApprovalQueue approvals={approvals} onDecided={reload} />
            </div>
          </div>
        )}

        {/* TAB 4: LIMITS — RISK LIMITS & OVERRIDES */}
        {activeTab === "limits" && (
          <div
            id="governance-panel-limits"
            role="tabpanel"
            aria-labelledby="governance-tab-limits"
            className="space-y-6"
          >
            <div className="rounded border border-zinc-800 bg-zinc-900/40 p-4">
              <h3 className="font-mono text-xs font-bold uppercase tracking-wider text-zinc-200">
                Enforced Dispatcher Ceilings & Dynamic Overrides
              </h3>
              <p className="mt-1 font-mono text-2xs text-zinc-400 leading-relaxed">
                The execution dispatcher enforces global hard risk limits across all running strategy instances.
                Operators can freely tighten risk limits below system defaults at any time. Loosening limits toward
                or above default thresholds requires dual-custody verification via operator TOTP and an audited rationale.
              </p>
            </div>

            <div className="grid grid-cols-1 lg:grid-cols-12 gap-6">
              <div className="lg:col-span-6 space-y-4">
                <div className="rounded border border-zinc-800 bg-zinc-950 p-5">
                  <h4 className="font-mono text-xs font-bold uppercase tracking-wider text-zinc-200 mb-3">
                    Configure Limits & Overrides
                  </h4>
                  <RiskLimitsForm onSaved={reload} />
                </div>
              </div>
              <div className="lg:col-span-6 space-y-4">
                <EnforcedLimitsCard />
              </div>
            </div>
          </div>
        )}

        {/* TAB 5: RECONCILIATION — EXCHANGE RECONCILIATION */}
        {activeTab === "reconciliation" && (
          <div
            id="governance-panel-reconciliation"
            role="tabpanel"
            aria-labelledby="governance-tab-reconciliation"
            className="space-y-4"
          >
            <div className="rounded border border-zinc-800 bg-zinc-900/40 p-4">
              <h3 className="font-mono text-xs font-bold uppercase tracking-wider text-zinc-200">
                Authoritative Multi-Asset Ledger & Exchange Audit
              </h3>
              <p className="mt-1 font-mono text-2xs text-zinc-400 leading-relaxed">
                Continuous double-entry ledger reconciliation between internal paper execution records and external venue telemetry.
                Unreconciled balances or unacknowledged orders automatically trigger reconciliation alarms.
              </p>
            </div>

            <ReconciliationLedger />
          </div>
        )}

        {/* TAB 6: AUTONOMY — AUTONOMY & EVALS */}
        {activeTab === "autonomy" && (
          <div
            id="governance-panel-autonomy"
            role="tabpanel"
            aria-labelledby="governance-tab-autonomy"
            className="space-y-4"
          >
            <div className="rounded border border-zinc-800 bg-zinc-900/40 p-4">
              <h3 className="font-mono text-xs font-bold uppercase tracking-wider text-zinc-200">
                Autonomy Evaluations & Shadow Model Calibration
              </h3>
              <p className="mt-1 font-mono text-2xs text-zinc-400 leading-relaxed">
                Objective statistical evaluation of strategy track records across market regimes (Drawdown, Sortino vs BTC benchmark,
                Calibration Error, and Operator Veto Frequency). Shadow models run concurrently on live market feeds without capital risk.
              </p>
            </div>

            <AutonomyEvals mandates={mandates} />
            <ShadowPanel />
          </div>
        )}
      </div>

      {/* =========================================================================
          4. MODALS & CONFIRMATION DIALOGS
         ========================================================================= */}

      {/* Operator Login Modal */}
      {showLoginModal && (
        <OperatorLoginModal
          onSuccess={() => {
            setShowLoginModal(false);
            reload();
          }}
          onClose={() => setShowLoginModal(false)}
        />
      )}

      {/* Emergency Flatten All Positions Modal */}
      {showFlattenModal && (
        <div
          role="presentation"
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-sm p-4"
        >
          <div
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="flatten-modal-title"
            aria-describedby="flatten-modal-desc"
            className="w-full max-w-md rounded-xl border border-orange-600 bg-zinc-950 p-6 shadow-2xl"
          >
            <div className="flex items-center space-x-3 text-orange-400 mb-3">
              <AlertTriangle className="h-6 w-6" aria-hidden="true" />
              <h2
                id="flatten-modal-title"
                className="font-mono text-sm font-black uppercase tracking-wider text-orange-300"
              >
                Confirm Emergency Flatten All
              </h2>
            </div>
            <p
              id="flatten-modal-desc"
              className="text-xs text-zinc-300 font-mono mb-4 leading-relaxed"
            >
              This action will aggressively liquidate all{" "}
              <strong className="text-white">{openPositions.length} open position(s)</strong>{" "}
              concurrently via immediate market orders across the paper execution engine.
              Running strategies are not paused automatically and will remain active unless the
              Kill Switch is also latched.
            </p>

            <div className="mb-4 max-h-36 overflow-y-auto rounded border border-white/5 bg-zinc-900/60 p-2 font-mono text-2xs">
              <span className="text-zinc-500 block mb-1 font-bold">Positions to liquidate:</span>
              {openPositions.map((p) => (
                <div key={p.position_id} className="flex justify-between py-0.5 text-zinc-300">
                  <span>
                    {p.instrument_id} ({p.side})
                  </span>
                  <span>Qty: {p.quantity}</span>
                </div>
              ))}
            </div>

            <div className="flex justify-end gap-3 pt-2">
              <button
                type="button"
                onClick={() => setShowFlattenModal(false)}
                disabled={isFlattening}
                className="rounded border border-zinc-700 bg-zinc-900 px-4 py-2 font-mono text-xs font-bold text-zinc-300 hover:bg-zinc-800 transition-colors"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={handleConfirmFlatten}
                disabled={isFlattening}
                className="rounded bg-orange-700 hover:bg-orange-600 px-4 py-2 font-mono text-xs font-bold uppercase text-white transition-colors"
              >
                {isFlattening ? "Liquidating…" : "FLATTEN ALL POSITIONS"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Master Kill Switch Trip Confirmation Modal */}
      {showTripModal && (
        <div
          role="presentation"
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-sm p-4"
        >
          <div
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="trip-modal-title"
            className="w-full max-w-md rounded-xl border border-rose-600 bg-zinc-950 p-6 shadow-2xl relative"
          >
            <button
              type="button"
              onClick={() => setShowTripModal(false)}
              className="absolute top-4 right-4 text-zinc-500 hover:text-zinc-300"
              aria-label="Close"
            >
              <X className="w-5 h-5" />
            </button>
            <div className="flex items-center space-x-3 text-rose-500 mb-3">
              <Power className="h-6 w-6" aria-hidden="true" />
              <h2
                id="trip-modal-title"
                className="font-mono text-sm font-black uppercase tracking-wider text-rose-400"
              >
                Trip Paper Execution Kill Switch
              </h2>
            </div>
            <p className="text-xs text-zinc-300 font-mono mb-4 leading-relaxed">
              All paper trading and order routing will halt immediately. Re-enabling the engine requires an authorized operator reset protocol with step-up credentials.
            </p>

            <form onSubmit={handleConfirmTrip} className="space-y-4">
              <div>
                <label
                  htmlFor={tripReasonId}
                  className="block text-2xs font-mono text-zinc-400 mb-1 font-bold uppercase"
                >
                  Audit Reason (required)
                </label>
                <input
                  id={tripReasonId}
                  type="text"
                  value={tripReason}
                  onChange={(e) => setTripReason(e.target.value)}
                  className="w-full bg-zinc-900 border border-zinc-700 rounded px-3 py-2 text-xs text-zinc-100 font-mono focus:border-rose-500 focus:outline-none"
                  required
                  minLength={5}
                />
              </div>

              <div className="flex justify-end gap-3 pt-2">
                <button
                  type="button"
                  onClick={() => setShowTripModal(false)}
                  disabled={isTripping}
                  className="rounded border border-zinc-700 bg-zinc-900 px-4 py-2 font-mono text-xs font-bold text-zinc-300 hover:bg-zinc-800 transition-colors"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={isTripping}
                  className="rounded bg-rose-600 hover:bg-rose-500 px-4 py-2 font-mono text-xs font-bold uppercase text-white transition-colors"
                >
                  {isTripping ? "Tripping…" : "TRIP KILL SWITCH"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Authoritative Kill Switch Reset Modal */}
      {showResetModal && (
        <div
          role="presentation"
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-sm p-4"
        >
          <div
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="reset-modal-title"
            className="w-full max-w-md rounded-xl border border-amber-600 bg-zinc-950 p-6 shadow-2xl relative"
          >
            <button
              type="button"
              onClick={() => setShowResetModal(false)}
              className="absolute top-4 right-4 text-zinc-500 hover:text-zinc-300"
              aria-label="Close"
            >
              <X className="w-5 h-5" />
            </button>
            <div className="flex items-center space-x-3 text-amber-500 mb-3">
              <RefreshCw className="h-6 w-6" aria-hidden="true" />
              <h2
                id="reset-modal-title"
                className="font-mono text-sm font-black uppercase tracking-wider text-amber-400"
              >
                Secure Kill Switch Reset Protocol
              </h2>
            </div>
            <p className="text-xs text-zinc-300 font-mono mb-4 leading-relaxed">
              Resets the latched state. The paper engine verifies that no open orders or unhedged exposures remain before allowing nominal execution to resume.
            </p>

            {resetError && (
              <div
                role="alert"
                className="mb-4 rounded bg-rose-950/60 border border-rose-900 p-2.5 font-mono text-2xs text-rose-300"
              >
                {resetError}
              </div>
            )}

            <form onSubmit={handleConfirmReset} className="space-y-4">
              <div>
                <label
                  htmlFor={resetReasonId}
                  className="block text-2xs font-mono text-zinc-400 mb-1 font-bold uppercase"
                >
                  Audit Reason (min 5 characters)
                </label>
                <input
                  id={resetReasonId}
                  type="text"
                  placeholder="Root cause resolved, verified clean state"
                  value={resetReason}
                  onChange={(e) => setResetReason(e.target.value)}
                  className="w-full bg-zinc-900 border border-zinc-700 rounded px-3 py-2 text-xs text-zinc-100 font-mono focus:border-amber-500 focus:outline-none"
                  required
                  minLength={5}
                />
              </div>

              <div>
                <label
                  htmlFor={resetPasswordId}
                  className="block text-2xs font-mono text-zinc-400 mb-1 font-bold uppercase"
                >
                  Step-Up Password
                </label>
                <input
                  id={resetPasswordId}
                  type="password"
                  autoComplete="current-password"
                  value={resetPassword}
                  onChange={(e) => setResetPassword(e.target.value)}
                  className="w-full bg-zinc-900 border border-zinc-700 rounded px-3 py-2 text-xs text-zinc-100 font-mono focus:border-amber-500 focus:outline-none"
                  required
                />
              </div>

              <div className="flex justify-end gap-3 pt-2">
                <button
                  type="button"
                  onClick={() => setShowResetModal(false)}
                  disabled={isResetting}
                  className="rounded border border-zinc-700 bg-zinc-900 px-4 py-2 font-mono text-xs font-bold text-zinc-300 hover:bg-zinc-800 transition-colors"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={isResetting}
                  className="rounded bg-amber-600 hover:bg-amber-500 px-4 py-2 font-mono text-xs font-bold uppercase text-zinc-950 transition-colors"
                >
                  {isResetting ? "Authorizing…" : "Request Reset"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}

export default GovernanceConsole;
