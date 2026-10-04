"use client";

import React from "react";

interface Props {
  monthlyReturns: Record<string, Record<string, number>>;
}

const MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export const MonthlyHeatmap: React.FC<Props> = ({ monthlyReturns }) => {
  const years = Object.keys(monthlyReturns || {}).sort((a, b) => parseInt(b) - parseInt(a));

  if (years.length === 0) {
    return (
      <div className="flex items-center justify-center h-32 bg-zinc-950 border border-zinc-900 rounded p-4 text-xs font-mono text-zinc-500">
        No monthly performance history available for this run.
      </div>
    );
  }

  return (
    <div className="w-full bg-zinc-950 border border-zinc-900 rounded overflow-x-auto font-mono text-2xs">
      <table className="w-full text-center border-collapse">
        <thead>
          <tr className="border-b border-zinc-800/80 bg-zinc-900/50 text-zinc-400">
            <th className="py-2 px-3 text-left font-semibold text-zinc-300">Year</th>
            {MONTH_NAMES.map((m) => (
              <th key={m} className="py-2 px-2 font-medium">
                {m}
              </th>
            ))}
            <th className="py-2 px-3 font-semibold text-zinc-300">YTD</th>
          </tr>
        </thead>
        <tbody>
          {years.map((yr) => {
            const monthsData = monthlyReturns[yr] || {};
            // Compute compound YTD return
            let compoundYTD = 1.0;
            let hasAnyData = false;

            const cells = [];
            for (let m = 1; m <= 12; m++) {
              const val = monthsData[m.toString()];
              if (val !== undefined && val !== null) {
                hasAnyData = true;
                compoundYTD *= 1.0 + val;
                const isPositive = val > 0;
                const isZero = Math.abs(val) < 0.0001;
                const pctStr = `${isPositive ? "+" : ""}${(val * 100).toFixed(1)}%`;

                let bgClass = "bg-zinc-900/40 text-zinc-400";
                if (!isZero) {
                  if (val > 0.05) bgClass = "bg-emerald-900/60 text-emerald-300 font-semibold";
                  else if (val > 0) bgClass = "bg-emerald-950/50 text-emerald-400";
                  else if (val < -0.05) bgClass = "bg-rose-900/60 text-rose-300 font-semibold";
                  else bgClass = "bg-rose-950/50 text-rose-400";
                }

                cells.push(
                  <td key={m} className="p-1">
                    <div className={`py-1 rounded border border-zinc-800/30 ${bgClass}`} title={`${MONTH_NAMES[m - 1]} ${yr}: ${pctStr}`}>
                      {pctStr}
                    </div>
                  </td>
                );
              } else {
                cells.push(
                  <td key={m} className="p-1">
                    <div className="py-1 text-zinc-700">-</div>
                  </td>
                );
              }
            }

            const ytdReturn = hasAnyData ? compoundYTD - 1.0 : 0;
            const isYtdPos = ytdReturn > 0;
            const ytdStr = `${isYtdPos ? "+" : ""}${(ytdReturn * 100).toFixed(1)}%`;

            return (
              <tr key={yr} className="border-b border-zinc-900/60 hover:bg-zinc-900/30 transition-colors">
                <td className="py-2 px-3 text-left font-bold text-zinc-300">{yr}</td>
                {cells}
                <td className="py-2 px-3 font-bold">
                  <span className={ytdReturn >= 0 ? "text-emerald-400" : "text-rose-400"}>
                    {hasAnyData ? ytdStr : "-"}
                  </span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
};
