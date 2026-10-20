/**
 * Client-side perpetual margin math: a port of packages/execution/cryptonite_execution/margin.py.
 *
 * Used for pre-trade estimates and HUD display only; the backend dispatcher is authoritative.
 * Brackets are the same labelled reference snapshot of Binance's USD-M tables (live brackets: C11+).
 */

export type PerpSide = "LONG" | "SHORT";

interface Bracket {
  floor: number;
  cap: number | null;
  maxLeverage: number;
  mmr: number;
  cum: number;
}

// [floor, cap, maxLeverage, mmr]; cum is derived so maintenance margin is continuous at each floor
type TierRow = [number, number | null, number, number];

function buildBrackets(rows: TierRow[]): Bracket[] {
  const out: Bracket[] = [];
  let cum = 0;
  let prevMmr = 0;
  for (const [floor, cap, maxLeverage, mmr] of rows) {
    if (out.length) cum += floor * (mmr - prevMmr);
    out.push({ floor, cap, maxLeverage, mmr, cum });
    prevMmr = mmr;
  }
  return out;
}

const REFERENCE_BRACKETS: Record<string, Bracket[]> = {
  BTCUSDT: buildBrackets([
    [0, 50_000, 125, 0.004],
    [50_000, 250_000, 100, 0.005],
    [250_000, 1_000_000, 50, 0.01],
    [1_000_000, 10_000_000, 20, 0.025],
    [10_000_000, 20_000_000, 10, 0.05],
    [20_000_000, 50_000_000, 5, 0.1],
    [50_000_000, 100_000_000, 4, 0.125],
    [100_000_000, 200_000_000, 3, 0.15],
    [200_000_000, 300_000_000, 2, 0.25],
    [300_000_000, null, 1, 0.5],
  ]),
  ETHUSDT: buildBrackets([
    [0, 10_000, 100, 0.005],
    [10_000, 100_000, 75, 0.0065],
    [100_000, 500_000, 50, 0.01],
    [500_000, 1_000_000, 25, 0.02],
    [1_000_000, 2_000_000, 10, 0.05],
    [2_000_000, 5_000_000, 5, 0.1],
    [5_000_000, 10_000_000, 4, 0.125],
    [10_000_000, 20_000_000, 3, 0.15],
    [20_000_000, 50_000_000, 2, 0.25],
    [50_000_000, null, 1, 0.5],
  ]),
};

/** The account maximum (ADR 0009 D2: was 20) */
export const MAX_PERP_LEVERAGE = 10;

/** "BTCUSDT-PERP.BINANCE" -> "BTCUSDT"; null for spot ids. */
export function perpSymbol(instrumentId: string): string | null {
  const [symbolPart] = instrumentId.split(".");
  return symbolPart.endsWith("-PERP") ? symbolPart.slice(0, -5) : null;
}

export function toPerpInstrumentId(symbol: string): string {
  return `${symbol.toUpperCase().replace(".BINANCE", "").replace("-PERP", "")}-PERP.BINANCE`;
}

export function hasBrackets(instrumentId: string): boolean {
  const sym = perpSymbol(instrumentId);
  return sym !== null && sym in REFERENCE_BRACKETS;
}

function brackets(instrumentId: string): Bracket[] | null {
  const sym = perpSymbol(instrumentId);
  return sym ? REFERENCE_BRACKETS[sym] ?? null : null;
}

function contains(b: Bracket, notional: number): boolean {
  return b.floor <= notional && (b.cap === null || notional < b.cap);
}

export function initialMargin(notional: number, leverage: number): number {
  return Math.abs(notional) / Math.max(1, leverage);
}

export function maintenanceMargin(instrumentId: string, notional: number): number | null {
  const table = brackets(instrumentId);
  const n = Math.abs(notional);
  const b = table?.find((t) => contains(t, n));
  return b ? n * b.mmr - b.cum : null;
}

/**
 * Mark price at which margin ratio hits 100% (Binance one-way formula), with the MMR tier solved
 * at the trigger notional. ``wallet`` is isolated margin (ISOLATED) or the cross wallet (CROSS);
 * ``othersMm`` / ``othersUpnl`` are the other cross positions' MM and uPnL.
 */
export function liquidationPrice(
  instrumentId: string,
  side: PerpSide,
  quantity: number,
  entryPrice: number,
  wallet: number,
  othersMm = 0,
  othersUpnl = 0,
): number | null {
  const table = brackets(instrumentId);
  if (!table || quantity <= 0) return null;
  const s = side === "LONG" ? 1 : -1;
  const effectiveWallet = wallet - othersMm + othersUpnl;
  for (const b of table) {
    const price = (effectiveWallet + b.cum - s * quantity * entryPrice) / (quantity * b.mmr - s * quantity);
    if (price > 0 && contains(b, quantity * price)) return price;
  }
  return null;
}

/** |mark - liq| / mark as a fraction (0.05 = 5%). */
export function liquidationDistance(markPrice: number, liqPrice: number): number {
  return markPrice > 0 ? Math.abs(markPrice - liqPrice) / markPrice : 0;
}

/** Maintenance margin / margin balance; >= 1 means liquidation. */
export function marginRatio(maintenance: number, marginBalance: number): number {
  return marginBalance > 0 ? maintenance / marginBalance : Infinity;
}

/** Funding cash flow for a signed size: longs pay a positive rate, shorts a negative one. */
export function fundingPayment(signedSize: number, markPrice: number, fundingRate: number): number {
  return -signedSize * markPrice * fundingRate;
}
