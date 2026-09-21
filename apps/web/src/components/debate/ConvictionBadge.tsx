import React from 'react';

interface Props {
  conviction: string; // e.g. "OVERWEIGHT", "BUY", "HOLD", "UNDERWEIGHT", "SELL"
  score?: number;
}

export const ConvictionBadge: React.FC<Props> = ({ conviction, score }) => {
  const normalized = conviction.toUpperCase();
  
  let colors = 'bg-slate-500/20 text-slate-400 border-slate-500/30';
  if (normalized === 'OVERWEIGHT' || normalized === 'BUY') {
    colors = 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30';
  } else if (normalized === 'UNDERWEIGHT' || normalized === 'SELL') {
    colors = 'bg-rose-500/20 text-rose-400 border-rose-500/30';
  } else if (normalized === 'HOLD') {
    colors = 'bg-amber-500/20 text-amber-400 border-amber-500/30';
  }

  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-semibold border ${colors}`}>
      {normalized}
      {score !== undefined && (
        <span className="ml-1 pl-1 border-l border-current opacity-70">
          {(score * 100).toFixed(0)}%
        </span>
      )}
    </span>
  );
};
