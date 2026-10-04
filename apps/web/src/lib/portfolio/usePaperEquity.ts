import { useEffect, useState } from "react";
import { PRIMARY_ACCOUNT_ID, refreshPortfolio, usePortfolioStore } from "@/lib/stores/usePortfolioStore";
import { freshness, knownPositive, type FreshState } from "@/lib/freshness";

/** Equity older than this is "delayed"; older than 3× is "stale" and no longer used for sizing. */
export const EQUITY_MAX_AGE_MS = 30_000;

export interface PaperEquity {
  /** `total_equity` of PAPER-ACCOUNT-001 (what the server sizes from); null while unknown. */
  equity: number | null;
  asOf: number | null;
  state: FreshState;
  /** True only when equity is known and not stale: the one condition under which UI may size from it. */
  usable: boolean;
}

/**
 * The paper account's equity from `/api/v1/paper/accounts` (and the positions snapshot stream when
 * the Portfolio workspace is open). Never falls back to a constant.
 */
export function usePaperEquity(pollMs = 15_000): PaperEquity {
  const account = usePortfolioStore((s) => s.account);
  const accountAt = usePortfolioStore((s) => s.accountAt);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    void refreshPortfolio();
    const id = setInterval(() => {
      setNow(Date.now());
      if (typeof document !== "undefined" && document.hidden) return;
      void refreshPortfolio();
    }, pollMs);
    return () => clearInterval(id);
  }, [pollMs]);

  const equity = account && account.account_id === PRIMARY_ACCOUNT_ID ? knownPositive(account.total_equity) : null;
  const state: FreshState = equity === null ? "unknown" : freshness(accountAt, EQUITY_MAX_AGE_MS, Math.max(now, accountAt ?? 0));
  return { equity, asOf: accountAt, state, usable: equity !== null && (state === "live" || state === "delayed") };
}

/** Allocation as % of equity, rounded to an integer and clamped to the dispatcher's 1–25% band. */
export function allocationPct(sizeUsd: number, equity: number): number {
  const pct = Math.round((sizeUsd / equity) * 100);
  return Math.min(25, Math.max(1, pct));
}
