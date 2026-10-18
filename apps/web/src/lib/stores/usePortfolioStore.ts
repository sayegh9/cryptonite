import { useEffect } from 'react';
import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import { fetchApi } from '@/lib/api';
import { getStreamClient } from '@/lib/ws';

export interface PaperAccount {
  account_id: string;
  venue: string;
  currency: string;
  starting_balance: number;
  cash_balance: number;
  total_equity: number;
  realized_pnl: number;
  daily_realized_pnl: number;
  daily_loss_latched: boolean;
}

export interface PaperPosition {
  position_id: string;
  account_id: string;
  instance_id: string;
  instrument_id: string;
  side: 'LONG' | 'SHORT' | 'FLAT';
  entry_price: number;
  quantity: number;
  current_price: number;
  unrealized_pnl: number;
  realized_pnl?: number;
  status: 'OPEN' | 'CLOSED';
  opened_at?: string | null;
  closed_at?: string | null;
  strategy_name?: string | null;
  duration_s?: number | null;
  stop_loss_price?: number | null;
  take_profit_price?: number | null;
  // Perpetual-only (null/absent on spot rows)
  leverage?: number | null;
  margin_mode?: 'ISOLATED' | 'CROSS' | null;
  liquidation_price?: number | null;
  maintenance_margin?: number | null;
}

export interface PaperOrder {
  client_order_id: string;
  instance_id: string;
  strategy_name?: string | null;
  instrument_id: string;
  side: 'BUY' | 'SELL';
  order_type: 'MARKET' | 'LIMIT' | 'STOP_MARKET';
  quantity: number;
  price: number | null;
  filled_qty: number;
  avg_fill_price: number | null;
  status: string;
  created_at: string;
}

export interface PaperTrade {
  trade_id: string;
  client_order_id: string;
  instance_id?: string;
  strategy_name?: string | null;
  slippage_bps?: number | null;
  instrument_id: string;
  side: 'BUY' | 'SELL';
  price: number;
  quantity: number;
  commission: number;
  executed_at: string;
}

export interface StrategyInstance {
  instance_id: string;
  strategy_name: string;
  instrument_id: string;
  timeframe: string;
  allocation_usd: number;
  status: string; // RUNNING | PAUSED | STOPPED; anything else is treated as an error state
  last_processed_bar_ns: number;
  resolved_parameters?: unknown;
  created_at?: string;
}

export interface KillSwitchState {
  id: number;
  is_latched: boolean;
  latched_by?: string;
  reason?: string;
  latched_at?: string;
}

export interface AvailableStrategy {
  slug: string;
  name: string;
  active_version_id: string;
}

export type LedgerTab = 'orders' | 'trades';
export type LedgerRange = '24h' | '7d' | '30d' | 'all';

export interface LedgerPrefs {
  tab: LedgerTab;
  range: LedgerRange;
  symbol: string; // '' = all
  origin: string; // '' = all
  side: '' | 'BUY' | 'SELL';
  pageSize: number;
}

export const PRIMARY_ACCOUNT_ID = 'PAPER-ACCOUNT-001';
/** A WS snapshot younger than this is fresher than any REST row for live (mark-dependent) fields. */
const SNAPSHOT_FRESH_MS = 5000;

interface PortfolioState {
  account: PaperAccount | null;
  positions: PaperPosition[];
  orders: PaperOrder[];
  trades: PaperTrade[];
  strategies: StrategyInstance[];
  availableStrategies: AvailableStrategy[];
  killSwitch: KillSwitchState;
  loaded: boolean; // first REST round-trip has completed
  lastSnapshotAt: number | null;
  lastRestOkAt: number | null;
  /** When `account` was last confirmed by REST or a snapshot (null = never this session). */
  accountAt: number | null;
  /** When positions / the kill switch were last confirmed (null = never this session: unknown) */
  positionsAt: number | null;
  killSwitchAt: number | null;
  restDegraded: boolean; // at least one endpoint failed on the last poll; last-known data is shown
  equityHwm: { day: string; value: number } | null; // intraday high-water mark (persisted)
  ledger: LedgerPrefs;
  // Optimistic strategy statuses, held until a REST response issued after the change confirms them
  statusOverrides: Record<string, { status: string; at: number }>;

  applyRest: (r: RestResults, requestedAt: number) => void;
  applySnapshot: (msg: unknown) => void;
  setLedger: (patch: Partial<LedgerPrefs>) => void;
  setStrategyStatusOptimistic: (instanceId: string, status: string) => void;
}

interface RestResults {
  accounts?: unknown;
  positions?: unknown;
  orders?: unknown;
  trades?: unknown;
  strategies?: unknown;
  killSwitch?: unknown;
  availableStrategies?: unknown;
  failed: number;
}

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null;
const LIVE_POSITION_FIELDS = ['current_price', 'unrealized_pnl'] as const;
const LIVE_ACCOUNT_FIELDS = ['total_equity', 'cash_balance', 'realized_pnl', 'daily_realized_pnl'] as const;

function shallowEqual(a: object, b: object): boolean {
  const ka = Object.keys(a);
  if (ka.length !== Object.keys(b).length) return false;
  return ka.every((k) => Object.is((a as Record<string, unknown>)[k], (b as Record<string, unknown>)[k]));
}

/** Keep the previous object when nothing changed so memoized rows skip re-rendering. */
function reuse<T extends object>(prev: T | undefined, next: T): T {
  return prev && shallowEqual(prev, next) ? prev : next;
}

/** Reuse previous items (by key) and the previous array itself when the list is unchanged. */
function reconcileList<T extends object>(prev: T[], next: T[], key: (t: T) => string): T[] {
  const byKey = new Map(prev.map((p) => [key(p), p]));
  const merged = next.map((n) => reuse(byKey.get(key(n)), n));
  return merged.length === prev.length && merged.every((m, i) => m === prev[i]) ? prev : merged;
}

function coercePosition(row: Record<string, unknown>): PaperPosition {
  const n = (v: unknown) => Number(v ?? 0);
  const nOrNull = (v: unknown) => (v === null || v === undefined || v === '' ? null : Number(v));
  return {
    ...(row as unknown as PaperPosition),
    entry_price: n(row.entry_price),
    quantity: n(row.quantity),
    current_price: n(row.current_price ?? row.entry_price),
    unrealized_pnl: n(row.unrealized_pnl),
    realized_pnl: n(row.realized_pnl),
    leverage: nOrNull(row.leverage),
    liquidation_price: nOrNull(row.liquidation_price),
    maintenance_margin: nOrNull(row.maintenance_margin),
  };
}

function coerceAccount(row: Record<string, unknown>): PaperAccount {
  const n = (v: unknown) => Number(v ?? 0);
  return {
    ...(row as unknown as PaperAccount),
    starting_balance: n(row.starting_balance),
    cash_balance: n(row.cash_balance),
    total_equity: n(row.total_equity),
    realized_pnl: n(row.realized_pnl),
    daily_realized_pnl: n(row.daily_realized_pnl),
  };
}

const utcDay = (ms = Date.now()) => new Date(ms).toISOString().slice(0, 10);

/** Fold a new equity observation into today's high-water mark. */
function nextHwm(hwm: PortfolioState['equityHwm'], account: PaperAccount | null): PortfolioState['equityHwm'] {
  if (!account || !(account.total_equity > 0)) return hwm;
  const day = utcDay();
  // Start-of-day equity approximates the engine's HWM baseline when this is the first sighting today
  const startOfDay = account.total_equity - account.daily_realized_pnl;
  const base = hwm && hwm.day === day ? hwm.value : Math.max(startOfDay, 0);
  const value = Math.max(base, account.total_equity);
  return hwm && hwm.day === day && hwm.value === value ? hwm : { day, value };
}

export const usePortfolioStore = create<PortfolioState>()(persist((set) => ({
  account: null,
  positions: [],
  orders: [],
  trades: [],
  strategies: [],
  availableStrategies: [],
  killSwitch: { id: 1, is_latched: false },
  loaded: false,
  lastSnapshotAt: null,
  lastRestOkAt: null,
  accountAt: null,
  positionsAt: null,
  killSwitchAt: null,
  restDegraded: false,
  equityHwm: null,
  ledger: { tab: 'orders', range: 'all', symbol: '', origin: '', side: '', pageSize: 25 },
  statusOverrides: {},

  setLedger: (patch) => set((s) => ({ ledger: { ...s.ledger, ...patch } })),
  setStrategyStatusOptimistic: (instanceId, status) => set((s) => ({
    statusOverrides: { ...s.statusOverrides, [instanceId]: { status, at: Date.now() } },
    strategies: s.strategies.map((x) => (x.instance_id === instanceId ? { ...x, status } : x)),
  })),

  // Each field is only written when its request succeeded: a failed poll keeps the last good data
  // on screen instead of blanking it (the original flicker).
  applyRest: (r, requestedAt) => set((s) => {
    const patch: Partial<PortfolioState> = {
      loaded: true,
      restDegraded: r.failed > 0,
      lastRestOkAt: r.failed < 7 ? Date.now() : s.lastRestOkAt,
    };
    // A snapshot that landed after this request started is newer than the REST rows it returned
    const snapshotNewer = s.lastSnapshotAt !== null && s.lastSnapshotAt > requestedAt;
    const snapshotFresh = s.lastSnapshotAt !== null && Date.now() - s.lastSnapshotAt < SNAPSHOT_FRESH_MS;

    if (Array.isArray(r.accounts)) {
      const row = r.accounts.find((a) => isRecord(a) && a.account_id === PRIMARY_ACCOUNT_ID) ?? r.accounts[0];
      if (isRecord(row)) {
        const next = coerceAccount(row);
        if (s.account && snapshotFresh) {
          for (const k of LIVE_ACCOUNT_FIELDS) (next as unknown as Record<string, unknown>)[k] = s.account[k];
        }
        patch.account = reuse(s.account ?? undefined, next);
        patch.accountAt = Date.now();
      }
    }

    if (Array.isArray(r.positions)) {
      const prevById = new Map(s.positions.map((p) => [p.position_id, p]));
      const restIds = new Set<string>();
      const next: PaperPosition[] = [];
      for (const raw of r.positions) {
        if (!isRecord(raw)) continue;
        const row = coercePosition(raw);
        restIds.add(row.position_id);
        const prev = prevById.get(row.position_id);
        if (snapshotNewer && row.status === 'OPEN' && prev?.status === 'CLOSED') {
          // A newer snapshot already saw this position close: keep the local CLOSED view
          next.push(prev);
          continue;
        }
        if (row.status === 'OPEN' && prev?.status === 'OPEN' && snapshotFresh) {
          for (const k of LIVE_POSITION_FIELDS) (row as unknown as Record<string, unknown>)[k] = prev[k];
        }
        next.push(row);
      }
      // Positions opened after this request started exist only in the newer snapshot so far
      if (snapshotNewer) {
        for (const p of s.positions) if (p.status === 'OPEN' && !restIds.has(p.position_id)) next.unshift(p);
      }
      patch.positions = reconcileList(s.positions, next, (p) => p.position_id);
      patch.positionsAt = Date.now();
    }

    if (Array.isArray(r.orders)) patch.orders = reconcileList(s.orders, r.orders as PaperOrder[], (o) => o.client_order_id);
    if (Array.isArray(r.trades)) patch.trades = reconcileList(s.trades, r.trades as PaperTrade[], (t) => t.trade_id);
    if (Array.isArray(r.strategies)) {
      // Responses to requests issued before an optimistic change would revert it for one poll
      const overrides = Object.fromEntries(Object.entries(s.statusOverrides).filter(([, o]) => o.at > requestedAt));
      const rows = (r.strategies as StrategyInstance[]).map((x) => (overrides[x.instance_id] ? { ...x, status: overrides[x.instance_id].status } : x));
      patch.strategies = reconcileList(s.strategies, rows, (x) => x.instance_id);
      patch.statusOverrides = overrides;
    }
    if (Array.isArray(r.availableStrategies)) {
      patch.availableStrategies = reconcileList(s.availableStrategies, r.availableStrategies as AvailableStrategy[], (x) => x.slug);
    }
    if (isRecord(r.killSwitch) && typeof r.killSwitch.is_latched === 'boolean') {
      patch.killSwitch = reuse(s.killSwitch, r.killSwitch as unknown as KillSwitchState);
      patch.killSwitchAt = Date.now();
    }
    patch.equityHwm = nextHwm(s.equityHwm, patch.account ?? s.account);
    return patch;
  }),

  // The orchestrator's 1 Hz snapshot carries OPEN rows only, without perp margin fields: merge it
  // into the REST rows by position_id rather than replacing the list.
  applySnapshot: (msg) => set((s) => {
    if (!isRecord(msg) || !Array.isArray(msg.positions)) return {};
    const snapRows = msg.positions.filter(isRecord).map(coercePosition);
    const snapById = new Map(snapRows.map((p) => [p.position_id, p]));
    const snapAccounts = new Set(snapRows.map((p) => p.account_id));
    snapAccounts.add(s.account?.account_id ?? PRIMARY_ACCOUNT_ID);

    const known = new Set<string>();
    const next = s.positions.map((p) => {
      known.add(p.position_id);
      const live = snapById.get(p.position_id);
      if (live) return reuse(p, { ...p, ...live, status: 'OPEN' as const });
      // Absent from the engine's authoritative open set: it has been closed
      if (p.status === 'OPEN' && snapAccounts.has(p.account_id)) return { ...p, status: 'CLOSED' as const };
      return p;
    });
    const added = snapRows.filter((p) => !known.has(p.position_id));

    const patch: Partial<PortfolioState> = {
      lastSnapshotAt: Date.now(),
      positionsAt: Date.now(),
      positions: reconcileList(s.positions, [...added, ...next], (p) => p.position_id),
    };
    if (isRecord(msg.account)) {
      const merged = coerceAccount({ ...(s.account ?? { account_id: PRIMARY_ACCOUNT_ID }), ...msg.account });
      patch.account = reuse(s.account ?? undefined, merged);
      patch.accountAt = Date.now();
      patch.equityHwm = nextHwm(s.equityHwm, patch.account);
    }
    return patch;
  }),
}), {
  name: 'cryptonite-portfolio',
  // Live account/position data must never rehydrate as if current; only UI prefs and today's HWM persist
  partialize: (s) => ({ ledger: s.ledger, equityHwm: s.equityHwm }),
}));

let inFlight = false;
let rerun = false;

/**
 * One REST round-trip. Calls made while a request is in flight coalesce into a single follow-up
 * round-trip, so a refresh requested after a command always observes that command.
 */
export async function refreshPortfolio(): Promise<void> {
  if (inFlight) {
    rerun = true;
    return;
  }
  inFlight = true;
  const requestedAt = Date.now();
  try {
    const endpoints = [
      '/paper/accounts', '/paper/positions', '/paper/orders', '/paper/trades',
      '/paper/strategies', '/paper/kill-switch', '/strategies',
    ];
    const settled = await Promise.allSettled(endpoints.map((e) => fetchApi(e)));
    const val = (i: number) => (settled[i].status === 'fulfilled' ? (settled[i] as PromiseFulfilledResult<unknown>).value : undefined);
    usePortfolioStore.getState().applyRest({
      accounts: val(0),
      positions: val(1),
      orders: val(2),
      trades: val(3),
      strategies: val(4),
      killSwitch: val(5),
      availableStrategies: val(6),
      failed: settled.filter((x) => x.status === 'rejected').length,
    }, requestedAt);
  } finally {
    inFlight = false;
    if (rerun) {
      rerun = false;
      void refreshPortfolio();
    }
  }
}

/**
 * Keep the portfolio store in sync: REST poll plus the 1 Hz `positions_snapshot` stream.
 * The interval stays below the API's 5 s keep-alive timeout — polling at exactly 5 s makes the Next
 * proxy reuse sockets the server is closing (ECONNRESET), which used to blank the tables.
 */
export function usePortfolioSync(intervalMs = 3000) {
  useEffect(() => {
    const tick = () => {
      if (typeof document !== 'undefined' && document.hidden) return;
      void refreshPortfolio();
    };
    void refreshPortfolio();
    const id = setInterval(tick, intervalMs);

    const client = getStreamClient();
    // addSubscription (unlike a one-off send) is replayed when the socket reconnects
    client.addSubscription('portfolio', ['positions_snapshot']);
    const unsubscribe = client.subscribe((msg) => {
      if (isRecord(msg) && msg.type === 'positions_snapshot') usePortfolioStore.getState().applySnapshot(msg);
    });
    return () => {
      clearInterval(id);
      unsubscribe();
      client.removeSubscription('portfolio');
    };
  }, [intervalMs]);
}
