"use client";

import React, { useState, useEffect, useMemo } from "react";
import { useDebateStore, DebateRun, type DebateActionError } from "@/lib/stores/useDebateStore";
import { useSessionStore, type SessionStatus } from "@/lib/stores/useSessionStore";
import { fetchApi } from "@/lib/api";
import { OperatorLoginModal } from "@/components/operator/OperatorLoginModal";
import { usePaperEquity, EQUITY_MAX_AGE_MS } from "@/lib/portfolio/usePaperEquity";
import { Fresh } from "@/components/Fresh";
import { ApprovalRiskContext, sideOf, type SimilarDecision } from "@/components/ApprovalRiskContext";
import { metricOrNull } from "@/lib/freshness";
import {
  ShieldCheck,
  CheckCircle2,
  XCircle,
  Zap,
  Sliders,
  ChevronDown,
  ChevronUp,
  Clock,
  MessageSquare,
  Trash2,
  Archive,
  } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";

interface ActionInboxProps {
  /** Queue a real debate (POST /agent-api/debates/trigger) on `symbol`. */
  onTriggerDebate?: () => void;
  symbol?: string;
}

type FilterTab = "pending" | "completed" | "rejected" | "no_trade" | "all";

/** Local-only run ids from older builds (injected demo debates, optimistic placeholders): never server data. */
const LOCAL_ONLY_PREFIXES = ["demo-", "sim-", "pending-"];

/** A proposals request that has not answered by then is reported as not loaded, not waited on forever */
const INBOX_TIMEOUT_MS = 10_000;

/**
 * A Brain debate the safety gate refused before it ran: a finished no trade with nothing to approve. Read as a plain
 * string because the debate store's status type does not list it.
 */
const gateRefused = (run: { status: string }) => run.status === "GATE_REFUSED";

/** The inbox could not load: say so, and never show "nothing to approve" in its place */
function InboxLoadError({ syncError, session, onRetry }: { syncError: string; session: SessionStatus; onRetry: () => void }) {
  return (
    <div role="alert" className="p-4 bg-rose-950/30 border border-rose-900/50 rounded-lg text-center space-y-2" data-testid="inbox-load-failed">
      <div className="font-bold text-xs text-rose-200">Can&apos;t load proposals</div>
      <p className="text-2xs text-rose-300/80 leading-relaxed">{syncError} Nothing here is confirmed as current.</p>
      {session !== "anonymous" && (
        <Button size="sm" onClick={onRetry} className="bg-zinc-800 hover:bg-zinc-700 text-zinc-100 font-mono text-xs h-7 px-3 mx-auto">
          Retry
        </Button>
      )}
    </div>
  );
}

const clampPct = (v: number) => Math.min(25, Math.max(1, Math.round(v)));

const fmtUsd = (v: number) => `$${v.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;

export function ActionInbox({ onTriggerDebate, symbol }: ActionInboxProps) {
  const activeDebates = useDebateStore((state) => state.activeDebates);
  const approveDebate = useDebateStore((state) => state.approveDebate);
  const rejectDebate = useDebateStore((state) => state.rejectDebate);
  const removeDebate = useDebateStore((state) => state.removeDebate);
  const clearCompleted = useDebateStore((state) => state.clearCompleted);
  const clearAll = useDebateStore((state) => state.clearAll);

  const debatesList = Object.values(activeDebates);

  const [activeTab, setActiveTab] = useState<FilterTab>("pending");
  const [expandedRunId, setExpandedRunId] = useState<string | null>(null);
  // Operator-chosen allocation per run, in % of equity (1–25)
  const [sizingMap, setSizingMap] = useState<Record<string, number>>({});
  // The AI's proposed allocation_pct per run, from /debates/{id}/decision (null = not available)
  const [proposedPct, setProposedPct] = useState<Record<string, number | null>>({});
  // The run's portfolio decision (decision_price, recommendation, …) for the approval context
  const [decisions, setDecisions] = useState<Record<string, Record<string, unknown> | null>>({});
  const [processingId, setProcessingId] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<{ runId: string; action: "approve" | "reject" } | null>(null);
  const [showAuthModal, setShowAuthModal] = useState<boolean>(false);
  const [pendingRunId, setPendingRunId] = useState<string | null>(null);
  const [syncError, setSyncError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<Record<string, string>>({});
  const session = useSessionStore((s) => s.status);
  // Bumped by Retry to run the sync again
  const [syncNonce, setSyncNonce] = useState(0);
  const equity = usePaperEquity();

  // Sync real debate runs from API and remove local-only leftovers. Not while the session is known to be signed out.
  useEffect(() => {
    if (session === "anonymous") {
      setSyncError("Sign in as operator to see proposals.");
      return;
    }
    fetchApi("/agents/debates?limit=50", { timeoutMs: INBOX_TIMEOUT_MS })
      .then(data => {
        if (!Array.isArray(data)) throw new Error("unexpected response");
        setSyncError(null);
        useDebateStore.setState(state => {
          const next = { ...state.activeDebates };

          // Remove leftovers persisted by older builds (demo-/sim- debates, pending- placeholders)
          Object.keys(next).forEach(k => {
            if (LOCAL_ONLY_PREFIXES.some((p) => k.startsWith(p))) delete next[k];
          });

          // Create a Set of valid runIds from the DB
          const validRunIds = new Set(data.map((r: any) => r.runId));
          
          // Delete anything in local storage that no longer exists in the DB (like the truncated RUNNING ones)
          Object.keys(next).forEach(k => {
            if (!validRunIds.has(k)) {
              delete next[k];
            }
          });
          
          // Merge real data
          data.forEach((r: any) => {
            if (!next[r.runId]) {
              next[r.runId] = {
                runId: r.runId,
                instrumentId: r.instrumentId,
                status: r.status,
                decision: r.decision,
                timestamp: r.timestamp,
                turns: []
              };
            } else {
              next[r.runId].status = r.status;
              if (r.decision) next[r.runId].decision = r.decision;
              if (r.timestamp) next[r.runId].timestamp = r.timestamp;
            }
          });
          return { activeDebates: next };
        });
      })
      .catch((err: any) => {
        if (err?.status === 401 || err?.status === 403) {
          void useSessionStore.getState().check();
          setSyncError("Proposals were refused. Your operator session may have ended.");
        } else {
          setSyncError(err?.timeout ? "Proposals did not load in time." : "Proposals could not be loaded.");
        }
      });
  }, [session, syncNonce]);

  // Compute counts for filter tabs
  const { pendingCount, completedCount, rejectedCount, noTradeCount, totalCount } = useMemo(() => {
    let pending = 0;
    let completed = 0;
    let rejected = 0;
    let noTrade = 0;

    for (const run of debatesList) {
      // NO_PRICE is a finished debate with nothing to approve (it had no price), so it counts as no trade; so does a
      // debate the safety gate refused before it ran (GATE_REFUSED)
      if (run.status === "NO_TRADE" || run.status === "NO_PRICE" || gateRefused(run)) {
        noTrade++;
      } else if (run.status === "COMPLETED") {
        if (run.decision === "REJECTED") rejected++;
        else completed++;
      } else if (run.status === "REJECTED" || run.status === "FAILED") {
        rejected++;
      } else {
        pending++;
      }
    }

    return {
      pendingCount: pending,
      completedCount: completed,
      rejectedCount: rejected,
      noTradeCount: noTrade,
      totalCount: debatesList.length,
    };
  }, [debatesList]);

  // If active tab is empty and another tab has items, auto-navigate gracefully if on initial mount
  useEffect(() => {
    if (activeTab === "pending" && pendingCount === 0 && (completedCount > 0 || rejectedCount > 0)) {
      // Keep on pending or let user stay; default is pending
    }
  }, [pendingCount, completedCount, rejectedCount, activeTab]);

  // Filter and sort list
  const filteredDebates = useMemo(() => {
    let list = [...debatesList];

    // Filter by tab
    if (activeTab === "pending") {
      list = list.filter(
        (r) =>
          r.status !== "COMPLETED" && r.status !== "REJECTED" && r.status !== "FAILED" && r.status !== "NO_TRADE" && r.status !== "NO_PRICE" &&
          !gateRefused(r)
      );
    } else if (activeTab === "no_trade") {
      list = list.filter((r) => r.status === "NO_TRADE" || r.status === "NO_PRICE" || gateRefused(r));
    } else if (activeTab === "completed") {
      list = list.filter((r) => r.status === "COMPLETED" && r.decision !== "REJECTED");
    } else if (activeTab === "rejected") {
      list = list.filter((r) => r.status === "REJECTED" || r.status === "FAILED" || (r.status === "COMPLETED" && r.decision === "REJECTED"));
    }

    // Sort: Pending items first, then by newest timestamp
    return list.sort((a, b) => {
      const isActionableA = a.status === "PENDING_APPROVAL" || a.status === "RUNNING";
      const isActionableB = b.status === "PENDING_APPROVAL" || b.status === "RUNNING";
      if (isActionableA && !isActionableB) return -1;
      if (!isActionableA && isActionableB) return 1;

      const timeA = a.timestamp || (a.turns?.length ? Math.max(...a.turns.map((t) => t.timestamp || 0)) : 0);
      const timeB = b.timestamp || (b.turns?.length ? Math.max(...b.turns.map((t) => t.timestamp || 0)) : 0);
      return timeB - timeA;
    });
  }, [debatesList, activeTab]);

  // Fallback symbol parser
  const resolveInstrument = (run: DebateRun): string => {
    if (run.instrumentId && run.instrumentId !== "UNKNOWN") {
      return run.instrumentId.replace(".BINANCE", "");
    }
    if (run.turns && run.turns.length > 0) {
      for (const t of run.turns) {
        const match = t.content?.match(/\b(BTC|ETH|SOL|XRP|BNB|DOGE|ADA|AVAX|SUI|LINK)\b/i);
        if (match) return match[1].toUpperCase() + "USDT";
      }
    }
    return "UNKNOWN";
  };

  // The AI's proposed size for each run awaiting approval (the slider starts there)
  const fetchDecisionData = useDebateStore((state) => state.fetchDecisionData);
  const awaitingIds = debatesList.filter((r) => r.status === "PENDING_APPROVAL").map((r) => r.runId).join(",");
  useEffect(() => {
    if (!awaitingIds) return;
    let cancelled = false;
    for (const runId of awaitingIds.split(",")) {
      if (runId in proposedPct) continue;
      void fetchDecisionData(runId).then((d) => {
        if (cancelled) return;
        const pct = d && d.allocation_pct !== undefined && d.allocation_pct !== null ? Number(d.allocation_pct) : NaN;
        setProposedPct((prev) => ({ ...prev, [runId]: Number.isFinite(pct) ? clampPct(pct) : null }));
        setDecisions((prev) => ({ ...prev, [runId]: d && typeof d === "object" ? d : null }));
      });
    }
    return () => {
      cancelled = true;
    };
  }, [awaitingIds, fetchDecisionData, proposedPct]);

  const DEFAULT_PCT = 5;
  const pctFor = (runId: string) => sizingMap[runId] ?? proposedPct[runId] ?? DEFAULT_PCT;

  const reportError = (runId: string, err: unknown, what: string) => {
    const e = err as DebateActionError;
    const msg = e?.message || `${what} failed`;
    setActionError((prev) => ({ ...prev, [runId]: msg }));
    toast.error(`${what} failed`, { description: msg });
    if (e?.status === 401) {
      setPendingRunId(runId);
      setShowAuthModal(true);
    }
  };

  const handleApprove = async (runId: string) => {
    // Size only from known, non-stale equity: the slider is a % of the server's sizing base
    if (!equity.usable || equity.equity === null) {
      setActionError((prev) => ({ ...prev, [runId]: "Equity unavailable: cannot size this order" }));
      return;
    }
    setProcessingId(runId);
    setActionError((prev) => ({ ...prev, [runId]: "" }));
    const pct = clampPct(pctFor(runId));
    try {
      const res = await approveDebate(runId, { allocation_pct: pct });
      const serverPct = typeof res.allocation_pct === "number" ? res.allocation_pct : pct;
      toast.success("Approved by server", {
        description: `Order requested at ${serverPct}% of equity. The final size may be reduced to the mandate's room.`,
      });
    } catch (err) {
      reportError(runId, err, "Approval");
    } finally {
      setProcessingId(null);
    }
  };

  const handleReject = async (runId: string) => {
    setProcessingId(runId);
    setActionError((prev) => ({ ...prev, [runId]: "" }));
    try {
      await rejectDebate(runId);
      toast.info("Rejected by server", { description: "No order was staged." });
    } catch (err) {
      reportError(runId, err, "Rejection");
    } finally {
      setProcessingId(null);
    }
  };

  const handleDismiss = (runId: string) => {
    removeDebate(runId);
    toast.success("Card dismissed from inbox");
  };

  const handleClearCompleted = () => {
    clearCompleted();
    toast.success("Cleaned inactive and completed proposals");
  };

  const handleClearAll = () => {
    if (confirm("Clear all proposals and history from this Inbox?")) {
      clearAll();
      toast.success("Action Inbox cleared");
    }
  };

  const setSize = (runId: string, val: number) => {
    setSizingMap((prev) => ({ ...prev, [runId]: val }));
  };

  return (
    <div className="flex flex-col h-full w-full min-h-0 bg-zinc-950 overflow-hidden">
      {/* Inbox Header */}
      <div className="h-10 flex-none px-3 bg-zinc-900/60 border-b border-zinc-800 flex items-center justify-between">
        <div className="flex items-center space-x-2 text-xs font-mono font-bold text-zinc-200">
          <ShieldCheck className="w-3.5 h-3.5 text-emerald-400" />
          <span className="uppercase tracking-wider">Action & Decision Inbox</span>
        </div>

        {/* Quick Header Actions */}
        <div className="flex items-center space-x-1.5">
          {completedCount + rejectedCount > 0 && (
            <button
              onClick={handleClearCompleted}
              title="Clear all completed and rejected proposals"
              className="px-2 py-0.5 rounded bg-zinc-900 hover:bg-zinc-800 border border-zinc-800 hover:border-zinc-700 text-2xs font-mono text-zinc-400 hover:text-cyan-300 transition-colors flex items-center space-x-1"
            >
              <Archive className="w-3 h-3" />
              <span>Clear History</span>
            </button>
          )}

          {totalCount > 0 && (
            <button
              onClick={handleClearAll}
              title="Purge all inbox items"
              aria-label="Clear all inbox items"
              className="p-1 rounded bg-zinc-900 hover:bg-rose-950/60 border border-zinc-800 hover:border-rose-800 text-zinc-400 hover:text-rose-300 transition-colors"
            >
              <Trash2 className="w-3 h-3" />
            </button>
          )}
        </div>
      </div>

      {/* Filter Tabs Bar */}
      <div className="flex-none px-3 py-1.5 bg-zinc-900/40 border-b border-zinc-800/80 flex flex-wrap items-center justify-between gap-y-1 text-2xs font-mono">
        <div className="flex flex-wrap items-center gap-1">
          <button
            onClick={() => setActiveTab("pending")}
            className={`whitespace-nowrap px-2 py-0.5 rounded transition-colors flex items-center space-x-1 ${
              activeTab === "pending"
                ? "bg-amber-950/80 text-amber-300 border border-amber-800/80 font-bold"
                : "text-zinc-400 hover:text-zinc-200 hover:bg-zinc-900"
            }`}
          >
            <span>Pending</span>
            {pendingCount > 0 && (
              <span className="px-1 py-0.2 rounded-full bg-amber-500/20 text-amber-300 text-2xs">
                {pendingCount}
              </span>
            )}
          </button>

          <button
            onClick={() => setActiveTab("completed")}
            className={`whitespace-nowrap px-2 py-0.5 rounded transition-colors flex items-center space-x-1 ${
              activeTab === "completed"
                ? "bg-emerald-950/80 text-emerald-300 border border-emerald-800/80 font-bold"
                : "text-zinc-400 hover:text-zinc-200 hover:bg-zinc-900"
            }`}
          >
            <span>Staged</span>
            {completedCount > 0 && (
              <span className="px-1 py-0.2 rounded-full bg-emerald-500/20 text-emerald-300 text-2xs">
                {completedCount}
              </span>
            )}
          </button>

          <button
            onClick={() => setActiveTab("rejected")}
            className={`whitespace-nowrap px-2 py-0.5 rounded transition-colors flex items-center space-x-1 ${
              activeTab === "rejected"
                ? "bg-zinc-800 text-zinc-200 border border-zinc-700 font-bold"
                : "text-zinc-400 hover:text-zinc-200 hover:bg-zinc-900"
            }`}
          >
            <span>Rejected</span>
            {rejectedCount > 0 && (
              <span className="px-1 py-0.2 rounded-full bg-zinc-700 text-zinc-300 text-2xs">
                {rejectedCount}
              </span>
            )}
          </button>

          <button
            onClick={() => setActiveTab("no_trade")}
            className={`whitespace-nowrap px-2 py-0.5 rounded transition-colors flex items-center space-x-1 ${
              activeTab === "no_trade"
                ? "bg-sky-950/80 text-sky-300 border border-sky-800/80 font-bold"
                : "text-zinc-400 hover:text-zinc-200 hover:bg-zinc-900"
            }`}
            title="HOLD verdicts: nothing to approve"
          >
            <span>No trade</span>
            {noTradeCount > 0 && (
              <span className="px-1 py-0.2 rounded-full bg-sky-500/20 text-sky-300 text-2xs">
                {noTradeCount}
              </span>
            )}
          </button>

          <button
            onClick={() => setActiveTab("all")}
            className={`whitespace-nowrap px-2 py-0.5 rounded transition-colors flex items-center space-x-1 ${
              activeTab === "all"
                ? "bg-cyan-950/80 text-cyan-300 border border-cyan-800/80 font-bold"
                : "text-zinc-400 hover:text-zinc-200 hover:bg-zinc-900"
            }`}
          >
            <span>All</span>
            <span className="text-zinc-500 text-2xs">({totalCount})</span>
          </button>
        </div>

        <span className="text-2xs text-zinc-500 font-mono">
          {filteredDebates.length} {filteredDebates.length === 1 ? "item" : "items"}
        </span>
      </div>

      {syncError && (
        <div role="alert" className="flex-none px-3 py-1.5 bg-rose-950/40 border-b border-rose-900/60 text-2xs font-mono text-rose-300" data-testid="inbox-sync-error">
          {syncError} · showing only what this browser already knew
        </div>
      )}

      {/* Decision Cards Stream */}
      <div className="flex-1 min-h-0 overflow-y-auto overflow-x-hidden p-3 space-y-3 overscroll-contain">
        {filteredDebates.length === 0 ? (
          /* Empty States. A failed load is never shown as "nothing to approve": that would read as all clear. */
          syncError && activeTab !== "pending" ? (
            <InboxLoadError syncError={syncError} session={session} onRetry={() => setSyncNonce((n) => n + 1)} />
          ) : activeTab === "pending" ? (
            <div className="space-y-4">
              {syncError ? (
                <InboxLoadError syncError={syncError} session={session} onRetry={() => setSyncNonce((n) => n + 1)} />
              ) : (
              <div className="p-4 bg-zinc-900/40 border border-zinc-800 rounded-lg text-center space-y-2">
                <div className="w-8 h-8 rounded-full bg-emerald-950/80 border border-emerald-800/60 text-emerald-400 flex items-center justify-center mx-auto">
                  <CheckCircle2 className="w-4 h-4" />
                </div>
                <div className="font-bold text-xs text-zinc-200">Inbox Zero (All Clear)</div>
                <p className="text-2xs text-zinc-400 leading-relaxed">
                  No active trade proposals awaiting operator approval. When new signals or debate consensus trigger, actionable cards appear here.
                </p>
                {onTriggerDebate && (
                  <Button
                    size="sm"
                    onClick={onTriggerDebate}
                    className="bg-purple-600 hover:bg-purple-500 text-white font-mono text-xs font-bold h-7 px-3 flex items-center space-x-1.5 mx-auto mt-2"
                  >
                    <Zap className="w-3 h-3" />
                    <span>Run a real debate on {symbol || "the active symbol"}</span>
                  </Button>
                )}
              </div>
              )}

            </div>
          ) : (
            <div className="p-6 bg-zinc-900/30 border border-zinc-800/80 rounded-lg text-center space-y-2">
              <div className="w-8 h-8 rounded-full bg-zinc-900 text-zinc-500 flex items-center justify-center mx-auto border border-zinc-800">
                <Archive className="w-4 h-4" />
              </div>
              <div className="font-bold text-xs text-zinc-300">No {activeTab} proposals</div>
              <p className="text-2xs text-zinc-500">
                No items found in this category. Use the tabs above to switch views.
              </p>
            </div>
          )
        ) : (
          /* Filtered Debates List */
          filteredDebates.map((run) => {
            const isExpanded = expandedRunId === run.runId;
            const currentPct = clampPct(pctFor(run.runId));
            const isCompleted = run.status === "COMPLETED";
            const isRunning = run.status === "RUNNING";
            const isFailed = run.status === "FAILED";
            const isRejected = run.status === "REJECTED" || (run.status === "COMPLETED" && run.decision === "REJECTED");
            const isNoTrade = run.status === "NO_TRADE";
            const isNoPrice = run.status === "NO_PRICE";
            const isGateRefused = gateRefused(run);
            // Only a run the server holds in PENDING_APPROVAL can be approved
            const isActionable = run.status === "PENDING_APPROVAL";
            const symbolDisplay = resolveInstrument(run);

            return (
              <div
                key={run.runId}
                className={`p-3 rounded-lg border transition-all relative ${
                  isCompleted && !isRejected
                    ? "bg-zinc-900/25 border-emerald-900/30 opacity-85"
                    : isFailed
                    ? "bg-rose-950/20 border-rose-900/40"
                    : isRejected
                    ? "bg-zinc-900/20 border-zinc-800/60 opacity-75"
                    : "bg-zinc-900/70 border-zinc-800 shadow-md ring-1 ring-cyan-500/10"
                }`}
              >
                {/* Header Row */}
                <div className="flex items-start justify-between mb-2">
                  <div className="flex flex-col">
                    <div className="flex items-center space-x-2">
                      <span
                        className={`w-2 h-2 rounded-full flex-shrink-0 ${
                          isCompleted && !isRejected
                            ? "bg-emerald-400"
                            : isFailed
                            ? "bg-rose-500"
                            : isRejected
                            ? "bg-zinc-500"
                            : isRunning
                            ? "bg-purple-400 animate-pulse"
                            : "bg-amber-400"
                        }`}
                      />
                      <span className="font-bold text-xs text-zinc-100 font-mono">
                        {symbolDisplay}
                      </span>
                    </div>
                    {run.timestamp && (
                      <span className="text-2xs text-zinc-500 font-mono flex items-center mt-1 ml-4">
                        <Clock className="w-2.5 h-2.5 mr-1 flex-shrink-0" />
                        {new Date(run.timestamp).toLocaleString()}
                      </span>
                    )}
                  </div>

                  <div className="flex items-center space-x-1.5 flex-shrink-0">
                    <span

                      className={`px-1.5 py-0.5 rounded text-2xs font-mono font-bold border ${
                        isCompleted && !isRejected
                          ? "bg-emerald-950 text-emerald-400 border-emerald-800"
                          : isFailed
                          ? "bg-rose-950 text-rose-300 border-rose-800"
                          : isRejected
                          ? "bg-zinc-800 text-zinc-400 border-zinc-700"
                          : isRunning
                          ? "bg-purple-950 text-purple-300 border-purple-800"
                          : isNoTrade || isNoPrice || isGateRefused
                          ? "bg-sky-950 text-sky-300 border-sky-800"
                          : "bg-amber-950 text-amber-300 border-amber-800"
                      }`}
                    >
                      {isRejected
                        ? "REJECTED"
                        : isNoTrade
                        ? "NO TRADE"
                        : isNoPrice
                        ? "NO PRICE DATA"
                        : isGateRefused
                        ? "Not run (safety gate)"
                        : run.status}
                    </span>

                    {/* Individual Dismiss Button */}
                    <button
                      onClick={() => handleDismiss(run.runId)}
                      title="Dismiss proposal"
                      aria-label={`Dismiss ${symbolDisplay} proposal from this inbox`}
                      className="p-1 text-zinc-500 hover:text-rose-400 transition-colors rounded hover:bg-zinc-800"
                    >
                      <Trash2 className="w-3 h-3" />
                    </button>
                  </div>
                </div>

                {/* Brief Thesis */}
                <div className="p-2 bg-zinc-950/70 border border-zinc-800/80 rounded text-2xs text-zinc-300 space-y-1 mb-2 font-sans">
                  {run.decision ? (
                    <div>
                      <strong>Verdict:</strong> {run.decision}
                    </div>
                  ) : isFailed ? (
                    <div className="text-rose-400 font-mono text-2xs">
                      <strong>Notice:</strong> Debate run failed or timed out during execution.
                    </div>
                  ) : (
                    <div className="text-zinc-500">
                      {isRunning
                        ? "Debate running: no verdict yet."
                        : isNoPrice
                          ? "No verdict: the debate had no price data to decide on."
                          : "No verdict recorded."}
                    </div>
                  )}
                </div>

                {/* Sizing: a % of the paper account's equity, sent as allocation_pct */}
                {isActionable && (
                  <div className="p-2 bg-zinc-950/50 border border-zinc-800/60 rounded space-y-1.5 mb-2 font-mono text-2xs" data-testid="inbox-sizing">
                    <div className="flex items-center justify-between text-zinc-400">
                      <label htmlFor={`size-${run.runId}`} className="flex items-center space-x-1">
                        <Sliders className="w-3 h-3 text-cyan-400" aria-hidden="true" />
                        <span>Allocation: {currentPct}% of equity</span>
                      </label>
                      <span className="text-cyan-300 font-bold tabular-nums">
                        <Fresh
                          value={equity.usable ? equity.equity : null}
                          asOf={equity.asOf}
                          maxAgeMs={EQUITY_MAX_AGE_MS}
                          format={(e) => `≈ ${fmtUsd((e * currentPct) / 100)}`}
                          unknownText="equity unavailable"
                          testId="inbox-size-usd"
                        />
                      </span>
                    </div>
                    <input
                      id={`size-${run.runId}`}
                      type="range"
                      min={1}
                      max={25}
                      step={1}
                      value={currentPct}
                      disabled={!equity.usable}
                      onChange={(e) => setSize(run.runId, Number(e.target.value))}
                      aria-valuetext={`${currentPct}% of equity`}
                      className="w-full accent-cyan-400 h-1 bg-zinc-800 rounded cursor-pointer disabled:cursor-not-allowed disabled:opacity-40"
                    />
                    <div className="flex items-center justify-between text-2xs text-zinc-500">
                      <span>1%</span>
                      <span>
                        {proposedPct[run.runId] != null
                          ? `AI proposed ${proposedPct[run.runId]}%`
                          : run.runId in proposedPct
                          ? `AI proposal unavailable · default ${DEFAULT_PCT}%`
                          : "loading AI proposal…"}
                      </span>
                      <span>25% max</span>
                    </div>
                    <p className="text-2xs text-zinc-500">Final size may be reduced to the mandate's room.</p>
                    <ApprovalRiskContext
                      symbol={symbolDisplay === "UNKNOWN" ? null : symbolDisplay}
                      side={sideOf(decisions[run.runId]?.recommendation ?? run.decision)}
                      decisionPrice={metricOrNull(decisions[run.runId]?.decision_price)}
                      notionalUsd={equity.usable && equity.equity !== null ? (equity.equity * currentPct) / 100 : null}
                      stopPct={null}
                      roomUsd={null}
                      requestedPct={currentPct}
                      similar={Array.isArray(decisions[run.runId]?.similar_decisions) ? (decisions[run.runId]!.similar_decisions as SimilarDecision[]) : null}
                    />
                  </div>
                )}

                {/* Action Buttons */}
                {isActionable && (
                  <div className="flex flex-col space-y-2 mb-2 font-mono text-xs">
                    <button
                      onClick={() => {
                        const debatePrompt = `Review this debate for ${run.instrumentId}. Decision: ${run.decision || "Pending"}. What is your assessment?`;
                        window.dispatchEvent(
                          new CustomEvent("cryptonite:open-copilot", {
                            detail: { mode: "research", prompt: debatePrompt, autoSend: false },
                          })
                        );
                      }}
                      className="py-1.5 w-full bg-cyan-900/60 hover:bg-cyan-800 text-cyan-300 border border-cyan-800 rounded flex items-center justify-center space-x-1 transition-colors"
                    >
                      <MessageSquare className="w-3.5 h-3.5" />
                      <span>Ask Copilot</span>
                    </button>
                    {confirming?.runId === run.runId ? (
                      // Confirm step (R6.6): keyboard reachable, Esc or Cancel backs out
                      <div
                        className="grid grid-cols-2 gap-2"
                        role="group"
                        aria-label={confirming.action === "approve" ? "Confirm approval" : "Confirm rejection"}
                        onKeyDown={(e) => {
                          if (e.key === "Escape") setConfirming(null);
                        }}
                      >
                        <button
                          type="button"
                          autoFocus
                          data-testid="confirm-debate-btn"
                          disabled={processingId === run.runId || (confirming.action === "approve" && !equity.usable)}
                          onClick={() => {
                            const action = confirming.action;
                            setConfirming(null);
                            void (action === "approve" ? handleApprove(run.runId) : handleReject(run.runId));
                          }}
                          className={`py-1.5 font-bold rounded flex items-center justify-center transition-colors disabled:opacity-50 ${
                            confirming.action === "approve" ? "bg-emerald-500 text-zinc-950" : "bg-zinc-600 text-zinc-50"
                          }`}
                        >
                          {confirming.action === "approve" ? `Confirm approve at ${currentPct}%` : "Confirm reject"}
                        </button>
                        <button
                          type="button"
                          onClick={() => setConfirming(null)}
                          className="py-1.5 bg-zinc-800 hover:bg-zinc-700 text-zinc-300 rounded"
                        >
                          Cancel
                        </button>
                      </div>
                    ) : (
                    <div className="grid grid-cols-2 gap-2">
                      <button
                        type="button"
                        data-testid="approve-debate-btn"
                        onClick={() => setConfirming({ runId: run.runId, action: "approve" })}
                        disabled={processingId === run.runId || !equity.usable}
                        title={equity.usable ? undefined : "Equity unavailable: cannot size this order"}
                        className="py-1.5 bg-emerald-600 hover:bg-emerald-500 text-zinc-950 font-bold rounded flex items-center justify-center space-x-1 transition-colors disabled:opacity-50"
                      >
                        <CheckCircle2 className="w-3.5 h-3.5" aria-hidden="true" />
                        <span>Approve & Stage</span>
                      </button>
                      <button
                        type="button"
                        onClick={() => setConfirming({ runId: run.runId, action: "reject" })}
                        disabled={processingId === run.runId}
                        className="py-1.5 bg-zinc-800 hover:bg-zinc-700 text-zinc-300 rounded flex items-center justify-center space-x-1 transition-colors disabled:opacity-50"
                      >
                        <XCircle className="w-3.5 h-3.5" aria-hidden="true" />
                        <span>Reject</span>
                      </button>
                    </div>
                    )}
                    {actionError[run.runId] && (
                      <p role="alert" className="text-2xs text-rose-400 font-mono" data-testid="inbox-action-error">
                        {actionError[run.runId]}
                      </p>
                    )}
                  </div>
                )}

                {/* Collapsible Transcript Viewer */}
                <div className="pt-1.5 border-t border-zinc-800/60 flex items-center justify-between">
                  <button
                    onClick={() => setExpandedRunId(isExpanded ? null : run.runId)}
                    className="text-2xs font-mono text-zinc-400 hover:text-cyan-400 flex items-center space-x-1 transition-colors"
                  >
                    <span>
                      {isExpanded ? "Hide Debate Log" : "Audit Full Transcript"} (
                      {run.turns?.length || 0} turns)
                    </span>
                    {isExpanded ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
                  </button>
                </div>

                {isExpanded && (
                  <div className="mt-2 p-2 bg-zinc-950 rounded border border-zinc-800 space-y-2 max-h-48 overflow-y-auto text-2xs font-mono">
                    {run.turns?.map((turn, tIdx) => (
                      <div
                        key={tIdx}
                        className="space-y-0.5 border-b border-zinc-900 pb-1.5 last:border-0"
                      >
                        <div className="flex items-center justify-between text-zinc-500 font-bold">
                          <span className="text-cyan-400">{turn.role}</span>
                          <span>{new Date(turn.timestamp).toLocaleTimeString()}</span>
                        </div>
                        <p className="text-zinc-300 font-sans leading-relaxed">{turn.content}</p>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            );
          })
        )}
      </div>

      {/* Operator Login Modal */}
      {showAuthModal && (
        <OperatorLoginModal
          onSuccess={() => {
            setShowAuthModal(false);
            if (pendingRunId) {
              handleApprove(pendingRunId);
              setPendingRunId(null);
            }
          }}
          onClose={() => {
            setShowAuthModal(false);
            setPendingRunId(null);
          }}
        />
      )}
    </div>
  );
}
