import { liquidationDistance, perpSymbol } from '@/lib/perp/margin';
import type { PaperPosition } from '@/lib/stores/usePortfolioStore';
import { fmtQuantity, isZeroQuantity } from '@/lib/format';

const DAY_MS = 86_400_000;
const QUOTE_SUFFIXES = ['USDT', 'USDC', 'FDUSD', 'BUSD'];

export const isPerp = (instrumentId: string) => perpSymbol(instrumentId) !== null;

/** "BTCUSDT.BINANCE" / "BTCUSDT-PERP.BINANCE" -> "BTC". */
export function baseAsset(instrumentId: string): string {
  const sym = perpSymbol(instrumentId) ?? instrumentId.split('.')[0];
  const quote = QUOTE_SUFFIXES.find((q) => sym.endsWith(q) && sym.length > q.length);
  return quote ? sym.slice(0, -quote.length) : sym;
}

export const markOf = (p: PaperPosition) => p.current_price || p.entry_price;
/** An OPEN row that actually holds something: FLAT and zero-quantity rows (e.g. a netted perp at "0E-18") are hidden (G4). */
export const isHeldPosition = (p: PaperPosition) => p.status === 'OPEN' && p.side !== 'FLAT' && !isZeroQuantity(p.quantity);
export const notionalOf = (p: PaperPosition) => Math.abs(p.quantity * markOf(p));

/** Fraction of mark between mark and liquidation price; null when not applicable. */
export function liqDistanceOf(p: PaperPosition, mark = markOf(p)): number | null {
  return p.liquidation_price != null && p.liquidation_price > 0 ? liquidationDistance(mark, p.liquidation_price) : null;
}

export interface StrategyStats {
  closedCount: number;
  winRate: number | null; // 0..1
  profitFactor: number | null; // Infinity when there are no losing trades
  pnl7d: number;
  curve7d: number[]; // cumulative realized PnL at the end of each of the last 7 UTC days
  openNotional: number;
}

/** Per-instance performance from position history (realized PnL is booked on each closed row). */
export function strategyStats(positions: PaperPosition[], instanceId: string, now = Date.now()): StrategyStats {
  const today = Math.floor(now / DAY_MS);
  const daily = new Array<number>(7).fill(0);
  let wins = 0, closedCount = 0, grossProfit = 0, grossLoss = 0, openNotional = 0;

  for (const p of positions) {
    if (p.instance_id !== instanceId) continue;
    if (p.status === 'OPEN') { openNotional += notionalOf(p); continue; }
    const pnl = Number(p.realized_pnl ?? 0);
    closedCount += 1;
    if (pnl > 0) { wins += 1; grossProfit += pnl; } else if (pnl < 0) grossLoss -= pnl;
    const closedAt = p.closed_at ? Date.parse(p.closed_at) : NaN;
    if (Number.isFinite(closedAt)) {
      const ago = today - Math.floor(closedAt / DAY_MS);
      if (ago >= 0 && ago < 7) daily[6 - ago] += pnl;
    }
  }

  let running = 0;
  const curve7d = daily.map((d) => (running += d));
  return {
    closedCount,
    winRate: closedCount > 0 ? wins / closedCount : null,
    profitFactor: grossLoss > 0 ? grossProfit / grossLoss : grossProfit > 0 ? Infinity : null,
    pnl7d: running,
    curve7d,
    openNotional,
  };
}

export interface ExposureSlice {
  key: string;
  gross: number; // |long| + |short| notional
  net: number; // long - short notional
}

export function exposureBy(open: PaperPosition[], by: 'asset' | 'strategy'): ExposureSlice[] {
  const map = new Map<string, ExposureSlice>();
  for (const p of open) {
    const key = by === 'asset' ? baseAsset(p.instrument_id) : p.instance_id;
    const n = notionalOf(p);
    const s = map.get(key) ?? { key, gross: 0, net: 0 };
    s.gross += n;
    s.net += p.side === 'SHORT' ? -n : n;
    map.set(key, s);
  }
  return [...map.values()].sort((a, b) => b.gross - a.gross);
}

/** Intraday drawdown from the high-water mark as a fraction (0.02 = 2%). */
export function drawdownFrom(hwm: number | null | undefined, equity: number | null | undefined): number {
  if (!hwm || !equity || hwm <= 0) return 0;
  return Math.max(0, (hwm - equity) / hwm);
}

const csvCell = (v: unknown) => {
  const s = v === null || v === undefined ? '' : String(v);
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
};

export function toCsv<T>(rows: T[], columns: { header: string; value: (r: T) => unknown }[]): string {
  const lines = [columns.map((c) => csvCell(c.header)).join(',')];
  for (const r of rows) lines.push(columns.map((c) => csvCell(c.value(r))).join(','));
  return lines.join('\n');
}

export function downloadCsv(filename: string, csv: string) {
  const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

const usd = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2 });
const compactUsd = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', notation: 'compact', maximumFractionDigits: 1 });

export const fmtUsd = (v: number) => usd.format(Number.isFinite(v) ? v : 0);
export const fmtCompactUsd = (v: number) => compactUsd.format(Number.isFinite(v) ? v : 0);
/** "+$1,234.50" / "-$12.00" */
export const fmtSignedUsd = (v: number) => `${v >= 0 ? '+' : '-'}${usd.format(Math.abs(Number.isFinite(v) ? v : 0))}`;
export const fmtPct = (v: number, digits = 1) => `${(v * 100).toFixed(digits)}%`;
/** Price with separators and precision scaled to magnitude (sub-dollar assets keep their decimals). */
export function fmtPrice(v: number): string {
  if (!Number.isFinite(v)) return '—';
  const digits = Math.abs(v) >= 1000 ? 2 : Math.abs(v) >= 1 ? 4 : 6;
  return `$${v.toLocaleString('en-US', { minimumFractionDigits: Math.min(digits, 2), maximumFractionDigits: digits })}`;
}
/** Quantity without a known step size: significant-digit fallback of the shared formatter (G4). */
export const fmtQty = (v: number | string) => fmtQuantity(v);
