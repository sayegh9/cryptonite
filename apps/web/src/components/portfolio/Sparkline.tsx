import React from "react";

interface SparklineProps {
  values: number[];
  width?: number;
  height?: number;
  label: string;
}

/** Minimal inline trend line; colored by the sign of the last value, with a zero baseline. */
export const Sparkline = React.memo(function Sparkline({ values, width = 84, height = 24, label }: SparklineProps) {
  const pad = 2;
  const min = Math.min(0, ...values);
  const max = Math.max(0, ...values);
  const span = max - min || 1;
  const x = (i: number) => pad + (i * (width - pad * 2)) / Math.max(values.length - 1, 1);
  const y = (v: number) => pad + ((max - v) * (height - pad * 2)) / span;
  const last = values[values.length - 1] ?? 0;
  const flat = values.every((v) => v === 0);
  const stroke = flat ? "stroke-zinc-600" : last >= 0 ? "stroke-emerald-400" : "stroke-red-400";
  const points = values.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");

  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-label={label} className="shrink-0">
      <line x1={pad} x2={width - pad} y1={y(0)} y2={y(0)} className="stroke-zinc-800" strokeDasharray="2 2" />
      <polyline points={points} fill="none" strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" className={stroke} />
      {values.length > 0 && <circle cx={x(values.length - 1)} cy={y(last)} r={1.8} className={flat ? "fill-zinc-600" : last >= 0 ? "fill-emerald-400" : "fill-red-400"} />}
    </svg>
  );
});
