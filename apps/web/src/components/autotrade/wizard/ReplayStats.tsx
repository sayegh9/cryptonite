"use client";

import type { PresetStatRow, PresetStats } from "@/lib/autotrade/types";
import type { Loadable } from "@/lib/stores/useAutoTradeStore";

const pct = (v: number | null | undefined) => (v == null ? "—" : `${(v * 100).toFixed(1)}%`);
const r = (v: number | null | undefined) => (v == null ? "—" : `${v >= 0 ? "+" : ""}${v.toFixed(2)}R`);

function warning(row: PresetStatRow | undefined, coin: string): { text: string; tone: string } {
  if (!row) return { text: `No replay data for ${coin}.`, tone: "text-zinc-400" };
  if (row.quality === "thin") return { text: `Thin replay: ${row.trades} trades. Treat these results as limited evidence.`, tone: "text-amber-300" };
  if (row.quality === "negative")
    return { text: `Negative replay: ${r(row.avg_r)} average R over ${row.trades} trades. This replay lost on average.`, tone: "text-red-300" };
  return { text: `${row.trades} trades in the replay.`, tone: "text-zinc-400" };
}

/**
 * Per coin and scanner preset: the replay as the API serves it, with thin and negative results labelled and never hidden.
 * ``ids`` are the presets the profile runs (its picks, or the style defaults).
 */
export function ReplayStats({ presets, coins, ids }: { presets: Loadable<PresetStats>; coins: string[]; ids: string[] }) {
  if (presets.status === "error") return <p className="text-xs text-amber-300">Replay stats unavailable. You can continue, but results won&apos;t be shown.</p>;
  const data = presets.data;
  if (!data) return <p role="status" className="text-xs text-zinc-400">Loading replay stats…</p>;
  if (coins.length === 0) return <p className="text-xs text-zinc-400">Choose coins in step 1 to see their replay results.</p>;
  return (
    <div className="space-y-2" data-testid="replay-stats">
      <p className="text-xs text-zinc-300">
        Replay results for the ticked strategies: <span className="font-mono text-zinc-100">{ids.join(", ") || "none"}</span>. A losing or
        thin replay is a warning only.
      </p>
      <div className="overflow-x-auto rounded-lg border border-zinc-800">
        <table className="w-full min-w-[560px] text-left text-xs">
          <caption className="sr-only">Six-month replay results per coin and preset</caption>
          <thead className="bg-zinc-900 text-zinc-400">
            <tr>
              <th scope="col" className="px-2 py-1.5">Coin</th>
              <th scope="col" className="px-2 py-1.5">Preset</th>
              <th scope="col" className="px-2 py-1.5 text-right">Trades</th>
              <th scope="col" className="px-2 py-1.5 text-right">Win rate</th>
              <th scope="col" className="px-2 py-1.5 text-right">Avg R</th>
              <th scope="col" className="px-2 py-1.5">Note</th>
            </tr>
          </thead>
          <tbody>
            {coins.flatMap((coin) =>
              ids.map((id) => {
                const row = data.results.find((x) => x.coin === coin && x.preset === id);
                const w = coin === "MUBARAKUSDT" && !row ? { text: "No lake data for MUBARAK; no replay result is available.", tone: "text-zinc-400" } : warning(row, coin);
                return (
                  <tr key={`${coin}:${id}`} className="border-t border-zinc-800">
                    <td className="px-2 py-1.5 text-zinc-200">{coin}</td>
                    <td className="px-2 py-1.5 font-mono text-zinc-300">{id}</td>
                    <td className="px-2 py-1.5 text-right text-zinc-200">{row ? row.trades : "—"}</td>
                    <td className="px-2 py-1.5 text-right text-zinc-200">{row ? pct(row.win_rate) : "—"}</td>
                    <td className="px-2 py-1.5 text-right text-zinc-200">{row ? r(row.avg_r) : "—"}</td>
                    <td className={`px-2 py-1.5 ${w.tone}`}>{w.text}</td>
                  </tr>
                );
              }),
            )}
          </tbody>
        </table>
      </div>
      <p className="text-2xs text-zinc-500">
        Replay generated {new Date(data.generated_at).toLocaleString()} · <span className="font-mono">{data.source_command}</span>. A replay is evidence,
        not a forecast.
      </p>
    </div>
  );
}
