import { useEffect } from 'react';
import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import { toast } from 'sonner';
import { fetchApi } from '@/lib/api';
import { getStreamClient } from '@/lib/ws';
import {
  MAX_PERP_LEVERAGE,
  liquidationDistance,
  maintenanceMargin,
  marginRatio,
  perpSymbol,
  type PerpSide,
} from '@/lib/perp/margin';

export type MarginMode = 'ISOLATED' | 'CROSS';

export interface MarkTick {
  instrumentId: string;
  symbol: string;
  markPrice: number;
  indexPrice: number;
  fundingRate: number;
  nextFundingTime: number; // epoch ms
  eventTime: number; // epoch ms
}

export interface PerpetualPosition {
  instrumentId: string;
  symbol: string;
  side: PerpSide;
  size: number; // absolute
  entryPrice: number;
  markPrice: number;
  leverage: number;
  marginMode: MarginMode;
  isolatedMargin: number;
  liquidationPrice: number | null;
  maintenanceMargin: number | null;
  unrealizedPnl: number;
  marginRatio: number | null; // MM / margin balance (1 = liquidation)
  liquidationDistance: number | null; // fraction of mark
}

export interface LiquidationEvent {
  clientOrderId: string;
  instrumentId: string;
  side: PerpSide;
  quantity: number;
  markPrice: number;
  fillPrice: number;
  liquidationPrice: number;
  realizedPnl: number;
  fees: number;
  insuranceFundDeficit: number;
  receivedAt: number;
}

export interface FundingEvent {
  instrumentId: string;
  signedQty: number;
  markPrice: number;
  fundingRate: number;
  cashFlow: number;
  cumulativeFunding: number;
  fundingTime: number; // epoch ms
}

interface PerpetualState {
  // Order ticket preferences (persisted)
  leverage: number;
  marginMode: MarginMode;
  // Session state (not persisted)
  reduceOnly: boolean;
  marks: Record<string, MarkTick>;
  positions: Record<string, PerpetualPosition>;
  crossMarginBalance: number | null; // account equity backing CROSS positions
  liquidations: LiquidationEvent[];
  liquidationAlert: LiquidationEvent | null;
  fundingHistory: FundingEvent[];

  setLeverage: (leverage: number) => void;
  setMarginMode: (mode: MarginMode) => void;
  setReduceOnly: (val: boolean) => void;
  handlePerpMessage: (msg: unknown) => boolean;
  setPositionsFromRows: (rows: unknown[], accountEquity?: number | null) => void;
  dismissLiquidationAlert: () => void;
}

const MAX_LIQUIDATIONS = 20;
const MAX_FUNDING_EVENTS = 50;
const NS_PER_MS = 1_000_000;

const num = (v: unknown): number => (v === null || v === undefined || v === '' ? NaN : Number(v));
const numOrNull = (v: unknown): number | null => {
  const n = num(v);
  return Number.isFinite(n) ? n : null;
};
const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null;

export function clampLeverage(leverage: number): number {
  if (!Number.isFinite(leverage)) return 1;
  return Math.min(MAX_PERP_LEVERAGE, Math.max(1, Math.round(leverage)));
}

/** Recompute mark-dependent fields of a position. */
function revalue(pos: PerpetualPosition, mark: number, crossBalance: number | null): PerpetualPosition {
  const direction = pos.side === 'LONG' ? 1 : -1;
  const unrealizedPnl = direction * pos.size * (mark - pos.entryPrice);
  const mm = maintenanceMargin(pos.instrumentId, pos.size * mark) ?? pos.maintenanceMargin;
  const balance = pos.marginMode === 'ISOLATED' ? pos.isolatedMargin + unrealizedPnl : crossBalance;
  return {
    ...pos,
    markPrice: mark,
    unrealizedPnl,
    maintenanceMargin: mm,
    marginRatio: mm !== null && balance !== null ? marginRatio(mm, balance) : null,
    liquidationDistance: pos.liquidationPrice !== null ? liquidationDistance(mark, pos.liquidationPrice) : null,
  };
}

function parseMark(d: Record<string, unknown>): MarkTick | null {
  const instrumentId = String(d.instrument_id ?? '');
  const markPrice = num(d.mark_price);
  if (!instrumentId || !Number.isFinite(markPrice)) return null;
  return {
    instrumentId,
    symbol: String(d.symbol ?? perpSymbol(instrumentId) ?? instrumentId),
    markPrice,
    indexPrice: num(d.index_price),
    fundingRate: num(d.funding_rate),
    nextFundingTime: num(d.next_funding_time_ns) / NS_PER_MS,
    eventTime: num(d.ts_event_ns) / NS_PER_MS,
  };
}

export const usePerpetualStore = create<PerpetualState>()(persist((set, get) => ({
  leverage: 1,
  marginMode: 'ISOLATED', // ADR 0009 D5: the manual ticket defaults to ISOLATED
  reduceOnly: false,
  marks: {},
  positions: {},
  crossMarginBalance: null,
  liquidations: [],
  liquidationAlert: null,
  fundingHistory: [],

  setLeverage: (leverage) => set({ leverage: clampLeverage(leverage) }),
  setMarginMode: (mode) => set({ marginMode: mode }),
  setReduceOnly: (val) => set({ reduceOnly: val }),
  dismissLiquidationAlert: () => set({ liquidationAlert: null }),

  handlePerpMessage: (msg) => {
    if (!isRecord(msg) || !isRecord(msg.data)) return false;
    const data = msg.data;

    if (msg.type === 'perp_mark_tick') {
      const tick = parseMark(data);
      if (!tick) return false;
      const prev = get().marks[tick.instrumentId];
      if (prev && prev.eventTime > tick.eventTime) return true; // out of order
      set((state) => {
        const pos = state.positions[tick.instrumentId];
        return {
          marks: { ...state.marks, [tick.instrumentId]: tick },
          positions: pos
            ? { ...state.positions, [tick.instrumentId]: revalue(pos, tick.markPrice, state.crossMarginBalance) }
            : state.positions,
        };
      });
      return true;
    }

    if (msg.type === 'perp_liquidation') {
      const event: LiquidationEvent = {
        clientOrderId: String(data.client_order_id ?? ''),
        instrumentId: String(data.instrument_id ?? ''),
        side: data.side === 'SHORT' ? 'SHORT' : 'LONG',
        quantity: num(data.quantity),
        markPrice: num(data.mark_price),
        fillPrice: num(data.fill_price),
        liquidationPrice: num(data.liquidation_price),
        realizedPnl: num(data.realized_pnl),
        fees: num(data.trading_fee) + num(data.clearance_fee),
        insuranceFundDeficit: num(data.insurance_fund_deficit),
        receivedAt: Date.now(),
      };
      set((state) => {
        const positions = { ...state.positions };
        delete positions[event.instrumentId];
        return {
          positions,
          liquidations: [event, ...state.liquidations].slice(0, MAX_LIQUIDATIONS),
          liquidationAlert: event,
        };
      });
      return true;
    }

    if (msg.type === 'perp_funding') {
      const event: FundingEvent = {
        instrumentId: String(data.instrument_id ?? ''),
        signedQty: num(data.signed_qty),
        markPrice: num(data.mark_price),
        fundingRate: num(data.funding_rate),
        cashFlow: num(data.cash_flow),
        cumulativeFunding: num(data.cumulative_funding),
        fundingTime: num(data.funding_time_ns) / NS_PER_MS,
      };
      set((state) => ({ fundingHistory: [event, ...state.fundingHistory].slice(0, MAX_FUNDING_EVENTS) }));
      return true;
    }

    return false;
  },

  setPositionsFromRows: (rows, accountEquity = null) => {
    const { marks } = get();
    const crossBalance = accountEquity ?? get().crossMarginBalance;
    const positions: Record<string, PerpetualPosition> = {};
    for (const row of rows) {
      if (!isRecord(row) || row.status !== 'OPEN') continue;
      const instrumentId = String(row.instrument_id ?? '');
      if (!perpSymbol(instrumentId) || (row.side !== 'LONG' && row.side !== 'SHORT')) continue;
      const base: PerpetualPosition = {
        instrumentId,
        symbol: perpSymbol(instrumentId) as string,
        side: row.side,
        size: num(row.quantity),
        entryPrice: num(row.entry_price),
        markPrice: num(row.current_price),
        leverage: numOrNull(row.leverage) ?? 1,
        marginMode: row.margin_mode === 'ISOLATED' ? 'ISOLATED' : 'CROSS',
        isolatedMargin: numOrNull(row.isolated_margin) ?? 0,
        liquidationPrice: numOrNull(row.liquidation_price),
        maintenanceMargin: numOrNull(row.maintenance_margin),
        unrealizedPnl: num(row.unrealized_pnl),
        marginRatio: null,
        liquidationDistance: null,
      };
      positions[instrumentId] = revalue(base, marks[instrumentId]?.markPrice ?? base.markPrice, crossBalance);
    }
    set({ positions, crossMarginBalance: crossBalance });
  },
}), {
  name: 'cryptonite-perpetuals',
  // Only ticket preferences persist; live marks/positions must never rehydrate as if current,
  // and a remembered reduce-only flag would silently turn the next entry order into a rejected close.
  partialize: (state) => ({ leverage: state.leverage, marginMode: state.marginMode }),
  // A leverage remembered from before the 10x account maximum (ADR 0009 D2; it was 20x) comes back clamped
  merge: (persisted, current) => {
    const p = isRecord(persisted) ? persisted : {};
    return { ...current, ...p, leverage: clampLeverage(num(p.leverage ?? current.leverage)) };
  },
}));

/**
 * Subscribe the shared stream client to the "perpetuals" channel and feed the store.
 * Mount once (app level) so liquidation alerts arrive regardless of which panel is visible.
 */
export function usePerpetualStream(enabled: boolean) {
  useEffect(() => {
    if (!enabled) return;
    const client = getStreamClient();
    client.addSubscription('perpetuals', ['perpetuals']);
    const unsubscribe = client.subscribe((msg) => {
      const store = usePerpetualStore.getState();
      if (!store.handlePerpMessage(msg)) return;
      if (isRecord(msg) && msg.type === 'perp_liquidation') {
        const liq = usePerpetualStore.getState().liquidationAlert;
        if (liq) {
          toast.error(`Liquidated: ${liq.instrumentId} ${liq.side}`, {
            description: `${liq.quantity} @ ${liq.fillPrice.toLocaleString()} (mark ${liq.markPrice.toLocaleString()})`,
            duration: 15000,
          });
        }
      }
    });
    return () => {
      unsubscribe();
      client.removeSubscription('perpetuals');
    };
  }, [enabled]);
}

/**
 * Poll perp positions and account equity (margin fields are not on the WebSocket snapshot).
 * Keep the interval below the API's 5 s keep-alive timeout: polling at exactly 5 s makes the Next
 * proxy reuse sockets the server is closing ("socket hang up" / ECONNRESET).
 */
export function usePerpetualPositionsPoll(intervalMs = 3000) {
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const [rows, accounts] = await Promise.all([
          fetchApi('/paper/positions').catch(() => []),
          fetchApi('/paper/accounts').catch(() => []),
        ]);
        if (cancelled) return;
        const account = Array.isArray(accounts) && isRecord(accounts[0]) ? accounts[0] : null;
        usePerpetualStore
          .getState()
          .setPositionsFromRows(Array.isArray(rows) ? rows : [], account ? numOrNull(account.total_equity) : null);
      } catch {
        // Operator not signed in or API unavailable: keep the last known state
      }
    };
    load();
    const id = setInterval(load, intervalMs);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [intervalMs]);
}
