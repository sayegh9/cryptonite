import React, { useState, useRef, useEffect } from 'react';
import { useDebateStore } from '@/lib/stores/useDebateStore';
import { SegmentedConvictionIndicator } from './SegmentedConvictionIndicator';

interface Props {
  runId: string;
}

export const ExecutionStagingCanvas: React.FC<Props> = ({ runId }) => {
  const run = useDebateStore((state) => state.activeDebates[runId]);
  const approveDebate = useDebateStore((state) => state.approveDebate);
  const rejectDebate = useDebateStore((state) => state.rejectDebate);
  const fetchDecisionData = useDebateStore((state) => state.fetchDecisionData);

  const [holdProgress, setHoldProgress] = useState(0);
  const [decisionData, setDecisionData] = useState<any>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  // Local overrides
  const [allocationPct, setAllocationPct] = useState<number>(5.0);
  const [orderType, setOrderType] = useState<'Market' | 'Limit'>('Market');
  const [limitOffsetPct, setLimitOffsetPct] = useState<number>(0.1);
  const [stopLossPct, setStopLossPct] = useState<number>(2.0);
  
  // Mock live price
  const [livePrice, setLivePrice] = useState<number | null>(null);

  const holdTimerRef = useRef<NodeJS.Timeout | null>(null);
  const intervalRef = useRef<NodeJS.Timeout | null>(null);

  useEffect(() => {
    if (run?.status === 'PENDING_APPROVAL') {
      fetchDecisionData(runId).then(data => {
        if (data) {
          setDecisionData(data);
          if (data.allocation_pct) setAllocationPct(data.allocation_pct);
          if (data.decision_price) {
            setLivePrice(data.decision_price);
          }
        }
      });
    }
  }, [run?.status, runId, fetchDecisionData]);

  // Mock drifting price for realism
  useEffect(() => {
    if (!livePrice) return;
    const interval = setInterval(() => {
      setLivePrice(prev => {
        if (!prev) return prev;
        const drift = prev * (Math.random() * 0.0004 - 0.0002);
        return prev + drift;
      });
    }, 1000);
    return () => clearInterval(interval);
  }, [livePrice]);

  if (!run || run.status !== 'PENDING_APPROVAL') return null;

  const decisionPrice = decisionData?.decision_price || livePrice;
  const priceDrift = (decisionPrice && livePrice) ? ((livePrice - decisionPrice) / decisionPrice) * 100 : 0;
  
  const tradeContext = {
    pair: decisionData?.instrument_id || run.instrumentId,
    direction: decisionData ? (decisionData.recommendation === 'BUY' ? 'LONG' : decisionData.recommendation === 'SELL' ? 'SHORT' : 'FLAT') : 'LONG',
    conviction: decisionData?.conviction_score ?? 0.8,
    estSize: `${allocationPct.toFixed(1)}%`,
    croSummary: decisionData?.reasoning || 'Risk parameters are within acceptable limits. Position sizing is conservative relative to volatility.',
  };

  const handleReject = async () => {
    try {
      setActionError(null);
      await rejectDebate(runId);
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    }
  };

  const handleMouseDown = () => {
    let progress = 0;
    const intervalMs = 20;
    const totalMs = 2000;
    const increment = (intervalMs / totalMs) * 100;

    setActionError(null);

    intervalRef.current = setInterval(() => {
      progress += increment;
      if (progress >= 100) {
        progress = 100;
        clearInterval(intervalRef.current!);
      }
      setHoldProgress(progress);
    }, intervalMs);

    holdTimerRef.current = setTimeout(async () => {
      clearInterval(intervalRef.current!);
      try {
        await approveDebate(runId, {
          allocation_pct: allocationPct,
          order_type: orderType,
          limit_offset_pct: limitOffsetPct,
          stop_loss_pct: stopLossPct
        });
      } catch (e) {
        setActionError(e instanceof Error ? e.message : String(e));
        setHoldProgress(0);
      }
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
        <h4 className="text-amber-400 font-semibold mb-2">Execution Staging Canvas</h4>
        <p className="text-sm text-slate-300">Adjust execution parameters before approving the trade.</p>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-4 p-4 bg-slate-950/50 rounded border border-slate-800 text-sm">
        <div className="flex flex-col">
          <span className="text-slate-500 text-xs">Pair</span>
          <span className="font-mono text-slate-200">{tradeContext.pair}</span>
        </div>
        <div className="flex flex-col">
          <span className="text-slate-500 text-xs">Direction</span>
          <span className={`font-mono ${tradeContext.direction === 'LONG' ? 'text-emerald-400' : tradeContext.direction === 'SHORT' ? 'text-rose-400' : 'text-slate-400'}`}>{tradeContext.direction}</span>
        </div>
        <div className="flex flex-col">
          <span className="text-slate-500 text-xs">Decision Price</span>
          <span className="font-mono text-slate-200">{decisionPrice ? `$${decisionPrice.toFixed(2)}` : 'N/A'}</span>
        </div>
        <div className="flex flex-col">
          <span className="text-slate-500 text-xs">Live Drift</span>
          <span className={`font-mono ${priceDrift > 0 ? 'text-emerald-400' : priceDrift < 0 ? 'text-rose-400' : 'text-slate-400'}`}>
            {priceDrift > 0 ? '+' : ''}{priceDrift.toFixed(3)}%
          </span>
        </div>
      </div>

      <div className="w-64">
        <SegmentedConvictionIndicator conviction={tradeContext.conviction} />
      </div>

      {/* Override Sliders */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6 bg-slate-950/40 p-4 rounded border border-slate-800/80">
        
        <div className="flex flex-col gap-2">
          <div className="flex justify-between items-center text-sm">
            <label className="text-slate-400 font-medium text-xs uppercase tracking-wider">Allocation %</label>
            <span className="text-amber-400 font-mono">{allocationPct.toFixed(1)}%</span>
          </div>
          <input 
            type="range" min="0.1" max="20" step="0.1" 
            value={allocationPct} onChange={(e) => setAllocationPct(parseFloat(e.target.value))}
            className="w-full accent-amber-500 h-1 bg-slate-700 rounded-lg appearance-none cursor-pointer" 
          />
        </div>

        <div className="flex flex-col gap-2">
          <div className="flex justify-between items-center text-sm">
            <label className="text-slate-400 font-medium text-xs uppercase tracking-wider">Stop Loss %</label>
            <span className="text-amber-400 font-mono">{stopLossPct.toFixed(1)}%</span>
          </div>
          <input 
            type="range" min="0.5" max="10" step="0.1" 
            value={stopLossPct} onChange={(e) => setStopLossPct(parseFloat(e.target.value))}
            className="w-full accent-amber-500 h-1 bg-slate-700 rounded-lg appearance-none cursor-pointer" 
          />
        </div>

        <div className="flex flex-col gap-2">
          <div className="flex justify-between items-center text-sm">
            <label className="text-slate-400 font-medium text-xs uppercase tracking-wider">Order Type</label>
          </div>
          <div className="flex gap-2">
            <button 
              onClick={() => setOrderType('Market')}
              className={`flex-1 py-1 text-sm rounded border ${orderType === 'Market' ? 'bg-amber-500/20 border-amber-500 text-amber-400' : 'bg-slate-800 border-slate-700 text-slate-400 hover:bg-slate-700'}`}
            >
              Market
            </button>
            <button 
              onClick={() => setOrderType('Limit')}
              className={`flex-1 py-1 text-sm rounded border ${orderType === 'Limit' ? 'bg-amber-500/20 border-amber-500 text-amber-400' : 'bg-slate-800 border-slate-700 text-slate-400 hover:bg-slate-700'}`}
            >
              Limit
            </button>
          </div>
        </div>

        <div className={`flex flex-col gap-2 transition-opacity ${orderType === 'Market' ? 'opacity-30 pointer-events-none' : 'opacity-100'}`}>
          <div className="flex justify-between items-center text-sm">
            <label className="text-slate-400 font-medium text-xs uppercase tracking-wider">Limit Offset %</label>
            <span className="text-amber-400 font-mono">{limitOffsetPct.toFixed(2)}%</span>
          </div>
          <input 
            type="range" min="-1" max="1" step="0.05" 
            value={limitOffsetPct} onChange={(e) => setLimitOffsetPct(parseFloat(e.target.value))}
            className="w-full accent-amber-500 h-1 bg-slate-700 rounded-lg appearance-none cursor-pointer" 
          />
        </div>

      </div>

      <div className="p-3 bg-amber-500/10 border border-amber-500/20 rounded text-sm text-amber-200/90">
        <span className="font-bold">CRO Summary:</span> {tradeContext.croSummary}
      </div>

      {actionError && (
        <div className="p-3 bg-rose-500/10 border border-rose-500/20 rounded text-sm text-rose-400">
          <span className="font-bold">Error:</span> {actionError}
        </div>
      )}

      <div className="flex items-center gap-3 pt-2">
        <button 
          onClick={handleReject}
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
