import { SMA, EMA, WMA, WEMA, RSI, ATR, ROC, WilliamsR, TRIX, CCI, ForceIndex, MFI, SD, AverageGain, AverageLoss } from "technicalindicators";
import type { Time } from "lightweight-charts";

export type IndicatorType =
  | "SMA" | "EMA" | "WMA" | "WEMA" | "RSI" | "ATR" | "ROC" | "WilliamsR" | "TRIX" | "CCI" | "ForceIndex" | "MFI" | "SD" | "AverageGain" | "AverageLoss";

export interface ChartIndicator {
  id: string;
  type: IndicatorType;
  period: number;
  color: string;
  pane: "main" | "bottom";
}

export const DEFAULT_COLORS = ["#3b82f6", "#eab308", "#ec4899", "#8b5cf6", "#10b981"];

export const INDICATOR_OPTIONS: { value: IndicatorType; label: string }[] = [
  { value: "SMA", label: "SMA" },
  { value: "EMA", label: "EMA" },
  { value: "WMA", label: "WMA" },
  { value: "WEMA", label: "WEMA" },
  { value: "RSI", label: "RSI" },
  { value: "ATR", label: "ATR" },
  { value: "ROC", label: "ROC" },
  { value: "WilliamsR", label: "Williams %R" },
  { value: "TRIX", label: "TRIX" },
  { value: "CCI", label: "CCI" },
  { value: "ForceIndex", label: "Force Index" },
  { value: "MFI", label: "MFI" },
  { value: "SD", label: "Standard Deviation" },
  { value: "AverageGain", label: "Avg Gain" },
  { value: "AverageLoss", label: "Avg Loss" },
];

/** Overlays share the price pane; oscillators go to the bottom pane. */
export const paneFor = (type: IndicatorType): ChartIndicator["pane"] =>
  ["SMA", "EMA", "WMA", "WEMA", "SD"].includes(type) ? "main" : "bottom";

function values(type: IndicatorType, period: number, bars: any[]): number[] {
  const close = bars.map((b) => Number(b.close));
  const high = bars.map((b) => Number(b.high));
  const low = bars.map((b) => Number(b.low));
  const volume = bars.map((b) => Number(b.volume));
  switch (type) {
    case "SMA": return SMA.calculate({ period, values: close });
    case "EMA": return EMA.calculate({ period, values: close });
    case "WMA": return WMA.calculate({ period, values: close });
    case "WEMA": return WEMA.calculate({ period, values: close });
    case "RSI": return RSI.calculate({ period, values: close });
    case "ATR": return ATR.calculate({ period, high, low, close });
    case "ROC": return ROC.calculate({ period, values: close });
    case "WilliamsR": return WilliamsR.calculate({ period, high, low, close });
    case "TRIX": return TRIX.calculate({ period, values: close });
    case "CCI": return CCI.calculate({ period, high, low, close });
    case "ForceIndex": return ForceIndex.calculate({ period, close, volume });
    case "MFI": return MFI.calculate({ period, high, low, close, volume });
    case "SD": return SD.calculate({ period, values: close });
    case "AverageGain": return AverageGain.calculate({ period, values: close });
    case "AverageLoss": return AverageLoss.calculate({ period, values: close });
    default: return [];
  }
}

/** Indicator line points aligned to the right end of `bars` (null when there are too few bars). */
export function indicatorSeries(ind: ChartIndicator, bars: any[]): { time: Time; value: number }[] | null {
  const vals = values(ind.type, ind.period, bars);
  const offset = bars.length - vals.length;
  if (offset < 0) return null;
  return vals.map((value, i) => {
    const b = bars[i + offset];
    const t = b.time !== undefined ? b.time : Math.floor((b.bar_open_ns || 0) / 1_000_000_000);
    return { time: t as unknown as Time, value };
  });
}
