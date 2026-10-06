"use client";

import { useState } from "react";
import type { Reliability } from "@/lib/mission/types";
import { calibrationReadable, calibrationThreshold } from "@/lib/format";

/**
 * Reliability chart (blueprint A7.2): stated confidence (x) against how often the decision was right
 * (y), one dot per confidence bin, sized by sample count. The dashed diagonal is perfect calibration.
 * One series, so no legend box: the heading names it. Mark colour #0891b2 passes the dataviz palette
 * checks on the zinc-950 surface; all text stays in text tokens. A table view follows the plot.
 * F5: once the server has fitted the isotonic map (>= 200 labels), it is drawn as a step line in a
 * second, dashed amber series and named in the caption. Informational only.
 */
const SIZE = 180;
const PAD = 28;
const MARK = "#0891b2";
const FIT = "#d97706";

/** Step path through the isotonic points: horizontal at each level, vertical at each jump. */
const stepPoints = (pts: [number, number][]) => {
  const out: string[] = [];
  pts.forEach(([x, y], i) => {
    if (i > 0) out.push(`${sx(x)},${sy(pts[i - 1][1])}`);
    out.push(`${sx(x)},${sy(y)}`);
  });
  return out.join(" ");
};

const sx = (v: number) => PAD + v * (SIZE - 2 * PAD);
const sy = (v: number) => SIZE - PAD - v * (SIZE - 2 * PAD);
const pct = (v: number | null) => (v == null ? "n/a" : `${Math.round(v * 100)}%`);

export default function ReliabilityChart({ role, data }: { role: string; data: Reliability }) {
  const [hover, setHover] = useState<number | null>(null);
  const points = data.bins
    .map((b, i) => ({ ...b, i }))
    .filter((b) => b.n > 0 && b.mean_confidence != null && b.hit_rate != null);
  const maxN = Math.max(1, ...points.map((p) => p.n));
  const radius = (n: number) => 4 + 4 * Math.sqrt(n / maxN); // >= 8px diameter
  const active = hover == null ? null : points.find((p) => p.i === hover) ?? null;
  const fit = data.isotonic?.fitted && data.isotonic.points.length > 0 ? data.isotonic.points : null;
  // G4: below max(20, min_samples) labelled decisions the curve is noise; grey it out and say so
  const readable = calibrationReadable(data.n, data.min_samples);
  const noteId = `reliability-note-${role}`;

  return (
    <figure className="space-y-2" data-testid={`reliability-${role}`} data-readable={readable ? "true" : "false"}>
      <figcaption className="text-zinc-400">
        <span className="font-semibold text-zinc-300">{role}</span>: confidence vs 24h hit rate ·{" "}
        {data.n} labelled · ECE {data.ece_pp == null ? `n/a (needs ${data.min_samples})` : `${data.ece_pp} pp`}
        {data.isotonic && (
          <span data-testid={`isotonic-${role}`}>
            {" "}· isotonic fit{" "}
            {fit ? (
              <span style={{ color: FIT }}>shown dashed (informational)</span>
            ) : (
              `not fitted (needs ${data.isotonic.min_points ?? 200})`
            )}
          </span>
        )}
      </figcaption>
      {!readable && points.length > 0 && (
        <p id={noteId} className="text-amber-300" data-testid={`reliability-insufficient-${role}`}>
          Too few labelled decisions to read: {data.n} of {calibrationThreshold(data.min_samples)}. Shown greyed out; the curve is noise until then.
        </p>
      )}
      {points.length === 0 ? (
        <p className="text-zinc-500">No labelled decisions yet.</p>
      ) : (
        <div className={`relative w-fit ${readable ? "" : "opacity-40 grayscale"}`}>
          <svg width={SIZE} height={SIZE} role="img" aria-describedby={readable ? undefined : noteId}
               aria-label={`${readable ? "" : "Not enough data: "}Reliability of ${role}: ${points.map((p) => `${pct(p.mean_confidence)} stated, ${pct(p.hit_rate)} right, n ${p.n}`).join("; ")}`}>
            {[0, 0.5, 1].map((t) => (
              <g key={t}>
                <line x1={sx(0)} x2={sx(1)} y1={sy(t)} y2={sy(t)} stroke="#27272a" strokeWidth={1} />
                <text x={PAD - 4} y={sy(t) + 3} textAnchor="end" fontSize={9} fill="#71717a">{t * 100}%</text>
                <text x={sx(t)} y={SIZE - PAD + 12} textAnchor="middle" fontSize={9} fill="#71717a">{t * 100}%</text>
              </g>
            ))}
            <line x1={sx(0)} y1={sy(0)} x2={sx(1)} y2={sy(1)} stroke="#52525b" strokeWidth={1} strokeDasharray="3 3" />
            {fit && (
              <polyline data-testid={`isotonic-line-${role}`} fill="none" stroke={FIT} strokeWidth={2} strokeDasharray="5 3"
                        points={stepPoints(fit)} />
            )}
            <polyline fill="none" stroke={MARK} strokeWidth={2}
                      points={points.map((p) => `${sx(p.mean_confidence!)},${sy(p.hit_rate!)}`).join(" ")} />
            {points.map((p) => (
              <g key={p.i} onMouseEnter={() => setHover(p.i)} onMouseLeave={() => setHover(null)}>
                <circle cx={sx(p.mean_confidence!)} cy={sy(p.hit_rate!)} r={14} fill="transparent" />
                <circle cx={sx(p.mean_confidence!)} cy={sy(p.hit_rate!)} r={radius(p.n)} fill={MARK}
                        stroke="#09090b" strokeWidth={2} />
              </g>
            ))}
          </svg>
          {active && (
            <div role="tooltip" className="pointer-events-none absolute rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-2xs text-zinc-200"
                 style={{ left: sx(active.mean_confidence!) + 10, top: sy(active.hit_rate!) - 10 }}>
              stated {pct(active.mean_confidence)} · right {pct(active.hit_rate)} · n {active.n}
            </div>
          )}
        </div>
      )}
      <table className="w-full text-left text-2xs" aria-label={`Reliability table for ${role}`}>
        <thead className="text-zinc-500">
          <tr><th>Confidence</th><th>Stated</th><th>Right</th><th>n</th></tr>
        </thead>
        <tbody className="text-zinc-300">
          {data.bins.map((b) => (
            <tr key={b.lo}>
              <td>{Math.round(b.lo * 100)}–{Math.round(b.hi * 100)}%</td>
              <td>{pct(b.mean_confidence)}</td>
              <td>{pct(b.hit_rate)}</td>
              <td>{b.n}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </figure>
  );
}
