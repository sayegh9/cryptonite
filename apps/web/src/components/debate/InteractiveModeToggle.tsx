import React, { useState, useRef } from 'react';
import { useDebateStore } from '@/lib/stores/useDebateStore';
import { SegmentedConvictionIndicator } from './SegmentedConvictionIndicator';

interface Props {
  runId: string;
}

export const InteractiveModeToggle: React.FC<Props> = ({ runId }) => {
  const run = useDebateStore((state) => state.activeDebates[runId]);
  const approveDebate = useDebateStore((state) => state.approveDebate);
  const rejectDebate = useDebateStore((state) => state.rejectDebate);

  const [holdProgress, setHoldProgress] = useState(0);
  const holdTimerRef = useRef<NodeJS.Timeout | null>(null);
  const intervalRef = useRef<NodeJS.Timeout | null>(null);

  if (!run || run.status !== 'PENDING_APPROVAL') return null;

  // Placeholder data for the trade context
  const mockTradeContext = {
    pair: run.instrumentId,
    direction: 'LONG',
    allocation: 5.0,
    conviction: 0.8,
    estSize: '$250,000',
    croSummary: 'Risk parameters are within acceptable limits. Position sizing is conservative relative to volatility.',
  };

  const handleMouseDown = () => {
    let progress = 0;
    const intervalMs = 20;
    const totalMs = 2000;
    const increment = (intervalMs / totalMs) * 100;

    intervalRef.current = setInterval(() => {
      progress += increment;
      if (progress >= 100) {
        progress = 100;
        clearInterval(intervalRef.current!);
      }
      setHoldProgress(progress);
    }, intervalMs);

    holdTimerRef.current = setTimeout(() => {
      clearInterval(intervalRef.current!);
      approveDebate(runId);
    }, totalMs);
  };

  const handleMouseUpOrLeave = () => {
    if (holdTimerRef.current) clearTimeout(holdTimerRef.current);
    if (intervalRef.current) clearInterval(intervalRef.current);
    setHoldProgress(0);
  };

  return (
    <div className="flex flex-col gap-4 mt-4 p-5 bg-slate-900 border-2 border-amber-500/50 rounded-lg animate-[pulse_3s_ease-in-out_infinite] shadow-[0_0_15px] shadow-amber-500/10">
      <div>
        <h4 className="text-amber-400 font-semibold mb-2">Operator Action Required</h4>
        <p className="text-sm text-slate-300">The Portfolio Manager has requested execution. Do you approve this trade?</p>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-4 p-4 bg-slate-950/50 rounded border border-slate-800 text-sm">
        <div className="flex flex-col">
          <span className="text-slate-500 text-xs">Pair</span>
          <span className="font-mono text-slate-200">{mockTradeContext.pair}</span>
        </div>
        <div className="flex flex-col">
          <span className="text-slate-500 text-xs">Direction</span>
          <span className="font-mono text-emerald-400">{mockTradeContext.direction}</span>
        </div>
        <div className="flex flex-col">
          <span className="text-slate-500 text-xs">Allocation</span>
          <span className="font-mono tabular-nums text-slate-200">{mockTradeContext.allocation.toFixed(1)}%</span>
        </div>
        <div className="flex flex-col">
          <span className="text-slate-500 text-xs">Est. Size</span>
          <span className="font-mono tabular-nums text-slate-200">{mockTradeContext.estSize}</span>
        </div>
      </div>

      <div className="w-64">
        <SegmentedConvictionIndicator conviction={mockTradeContext.conviction} />
      </div>

      <div className="p-3 bg-amber-500/10 border border-amber-500/20 rounded text-sm text-amber-200/90">
        <span className="font-bold">CRO Summary:</span> {mockTradeContext.croSummary}
      </div>

      <div className="flex items-center gap-3 pt-2">
        <button 
          onClick={() => rejectDebate(runId)}
          className="px-4 py-2 bg-slate-800 hover:bg-slate-700 text-rose-400 border border-slate-700 rounded transition-colors text-sm font-semibold focus-visible:ring-2 focus-visible:ring-rose-500 focus-visible:outline-none"
          aria-label="Reject and Archive"
        >
          Reject & Archive
        </button>
        
        <button 
          onMouseDown={handleMouseDown}
          onMouseUp={handleMouseUpOrLeave}
          onMouseLeave={handleMouseUpOrLeave}
          className="relative overflow-hidden px-6 py-2 bg-emerald-900/40 text-emerald-400 border border-emerald-500/50 rounded transition-colors text-sm font-semibold select-none focus-visible:ring-2 focus-visible:ring-emerald-500 focus-visible:outline-none flex-1 max-w-[200px]"
          aria-label="Hold to Approve Trade"
        >
          <div 
            className="absolute inset-y-0 left-0 bg-emerald-600/40 transition-all duration-75 ease-linear pointer-events-none"
            style={{ width: `${holdProgress}%` }}
          />
          <span className="relative z-10 pointer-events-none">
            {holdProgress > 0 ? 'Holding...' : 'Hold to Approve'}
          </span>
        </button>
      </div>
    </div>
  );
};
