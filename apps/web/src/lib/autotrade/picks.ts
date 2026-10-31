/** P3b strategy and timeframe picks, mirroring `effective_presets` / `effective_timeframes` in
 * packages/schemas/cryptonite_schemas/profile.py. An empty list means the style defaults. */
import type { Direction, PresetStats, Style } from "./types";

export interface Picks {
  strategies: string[];
  timeframes: string[];
}

const allows = (direction: Direction, side: "LONG" | "SHORT") => direction === "BOTH" || direction === side;

/** Every catalog preset the direction allows, in catalog order. */
export const selectablePresets = (stats: PresetStats, direction: Direction) =>
  Object.entries(stats.catalog).filter(([, m]) => allows(direction, m.side)).map(([slug]) => slug);

export const defaultPresets = (stats: PresetStats, style: Style, direction: Direction) =>
  stats.preset_ids[`${style}:${direction}`] ?? [];

export function effectivePresets(stats: PresetStats, style: Style, direction: Direction, strategies: string[]): string[] {
  if (strategies.length === 0) return defaultPresets(stats, style, direction);
  return strategies.filter((s) => stats.catalog[s] && allows(direction, stats.catalog[s].side));
}

/** The picked (or default) timeframes, before the presets' own support is applied. */
export const wantedTimeframes = (stats: PresetStats, style: Style, timeframes: string[]) =>
  timeframes.length ? timeframes : stats.default_timeframes[style] ?? [];

export function runnableTimeframes(stats: PresetStats, presets: string[]): Set<string> {
  return new Set(presets.flatMap((s) => stats.catalog[s]?.timeframes ?? []));
}

export function effectiveTimeframes(stats: PresetStats, style: Style, direction: Direction, p: Picks): string[] {
  const wanted = new Set(wantedTimeframes(stats, style, p.timeframes));
  const runnable = runnableTimeframes(stats, effectivePresets(stats, style, direction, p.strategies));
  return stats.profile_timeframes.filter((tf) => wanted.has(tf) && runnable.has(tf));
}

const same = (a: string[], b: string[]) => a.length === b.length && a.every((x) => b.includes(x));

/** Store `[]` when the ticks equal the style defaults, so a profile keeps following its style. */
export function normalizePicks(stats: PresetStats, style: Style, direction: Direction, p: Picks): Picks {
  return {
    strategies: same(p.strategies, defaultPresets(stats, style, direction)) ? [] : p.strategies,
    timeframes: same(p.timeframes, stats.default_timeframes[style] ?? []) ? [] : p.timeframes,
  };
}

/** Human summary: "Style defaults" or the lists. */
export function picksText(p: Picks): string {
  const s = p.strategies.length ? p.strategies.join(", ") : "Style defaults";
  const t = p.timeframes.length ? p.timeframes.join(", ") : "style defaults";
  return `${s} · timeframes: ${t}`;
}
