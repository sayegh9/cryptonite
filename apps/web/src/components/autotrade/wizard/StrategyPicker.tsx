"use client";

import { useId } from "react";
import {
  effectivePresets,
  effectiveTimeframes,
  normalizePicks,
  runnableTimeframes,
  selectablePresets,
  wantedTimeframes,
  type Picks,
} from "@/lib/autotrade/picks";
import type { Direction, PresetStats, Style } from "@/lib/autotrade/types";

/**
 * P3b: the scanner presets and bar timeframes a profile runs. The style pre-ticks its defaults; ticks equal to the
 * defaults are stored as empty lists so the profile keeps following its style. A losing replay is a warning, never a block.
 */
export function StrategyPicker({ stats, style, direction, value, onChange }: {
  stats: PresetStats; style: Style; direction: Direction; value: Picks; onChange: (p: Picks) => void;
}) {
  const uid = useId();
  const ticked = effectivePresets(stats, style, direction, value.strategies);
  const runnable = runnableTimeframes(stats, ticked);
  const wanted = wantedTimeframes(stats, style, value.timeframes);
  const sides = new Set(ticked.map((s) => stats.catalog[s].side));
  const emit = (p: Picks) => onChange(normalizePicks(stats, style, direction, p));
  const togglePreset = (slug: string, on: boolean) =>
    emit({ strategies: on ? [...ticked, slug] : ticked.filter((s) => s !== slug), timeframes: wanted });
  const toggleTimeframe = (tf: string, on: boolean) =>
    emit({ strategies: ticked, timeframes: stats.profile_timeframes.filter((t) => (t === tf ? on : wanted.includes(t))) });

  return (
    <div className="space-y-3" data-testid="strategy-picker">
      <fieldset className="space-y-1.5">
        <legend className="text-xs font-semibold text-zinc-300">Strategies</legend>
        <p className="text-xs text-zinc-400">The style ticks its defaults. Add or remove any; each runs on the timeframes listed.</p>
        <div className="grid grid-cols-1 gap-1 sm:grid-cols-2">
          {selectablePresets(stats, direction).map((slug) => (
            <label key={slug} className="flex items-center gap-2 text-xs text-zinc-200">
              <input type="checkbox" name={`${uid}-strategy`} checked={ticked.includes(slug)} onChange={(e) => togglePreset(slug, e.target.checked)} />
              <span className="font-mono">{slug}</span>
              <span className="text-zinc-500">
                {stats.catalog[slug].side === "SHORT" ? "short" : "long"} · {stats.catalog[slug].timeframes.join("/")}
              </span>
            </label>
          ))}
        </div>
      </fieldset>
      <fieldset className="space-y-1.5">
        <legend className="text-xs font-semibold text-zinc-300">Timeframes</legend>
        <div className="flex flex-wrap gap-3">
          {stats.profile_timeframes.map((tf) => (
            <label key={tf} className={`inline-flex items-center gap-1.5 text-xs ${runnable.has(tf) ? "text-zinc-200" : "text-zinc-500"}`}>
              <input type="checkbox" name={`${uid}-timeframe`} disabled={!runnable.has(tf)}
                checked={runnable.has(tf) && wanted.includes(tf)} onChange={(e) => toggleTimeframe(tf, e.target.checked)} />
              {tf}
            </label>
          ))}
        </div>
      </fieldset>
      {effectiveTimeframes(stats, style, direction, value).length === 0 && (
        <p role="alert" className="text-xs text-red-300">Pick at least one strategy and one timeframe it runs on.</p>
      )}
      {direction === "BOTH" && ticked.length > 0 && sides.size === 1 && (
        <p className="text-xs text-amber-300">Only {sides.has("LONG") ? "long" : "short"} strategies are ticked, so this profile will only open {sides.has("LONG") ? "longs" : "shorts"}.</p>
      )}
    </div>
  );
}
