import React from 'react';

interface Props {
  conviction: number; // -1.0 to 1.0
  label?: string;
}

export const SegmentedConvictionIndicator: React.FC<Props> = ({ conviction, label }) => {
  const isBullish = conviction >= 0;
  const absConviction = Math.abs(conviction);
  const activeSegments = Math.round(absConviction * 5); // 0 to 5

  const segments = Array.from({ length: 5 }).map((_, i) => i < activeSegments);

  const activeColor = isBullish ? 'bg-emerald-500' : 'bg-rose-500';
  const glowShadow = isBullish
    ? 'shadow-[0_0_8px] shadow-emerald-500/50'
    : 'shadow-[0_0_8px] shadow-rose-500/50';

  const percentage = (absConviction * 100).toFixed(0);
  const defaultLabel = isBullish ? 'BULLISH' : 'BEARISH';
  const displayLabel = label || defaultLabel;

  return (
    <div className="flex flex-col gap-1">
      <div className="flex justify-between items-center text-xs font-mono">
        <span className={isBullish ? 'text-emerald-500' : 'text-rose-500'}>
          {displayLabel}
        </span>
        <span className="tabular-nums text-slate-300">{percentage}%</span>
      </div>
      <div className="flex gap-1 h-2">
        {segments.map((isActive, i) => (
          <div
            key={i}
            className={`flex-1 rounded-sm transition-all duration-300 ${
              isActive ? `${activeColor} ${glowShadow}` : 'bg-slate-800'
            }`}
            aria-hidden="true"
          />
        ))}
      </div>
    </div>
  );
};
