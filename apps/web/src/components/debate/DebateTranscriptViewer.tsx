import React, { useRef, useEffect, useState } from 'react';
import { useDebateStore } from '@/lib/stores/useDebateStore';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { TrendingUp, TrendingDown, ShieldAlert, Scale, Search, ArrowDown } from 'lucide-react';

interface Props {
  runId: string;
  symbol?: string;
}

export const DebateTranscriptViewer: React.FC<Props> = ({ runId, symbol }) => {
  const run = useDebateStore((state) => state.activeDebates[runId]);
  
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  
  const [isPinnedToBottom, setIsPinnedToBottom] = useState(true);
  const [unseenTurns, setUnseenTurns] = useState(0);

  const prevTurnsLengthRef = useRef(run?.turns.length || 0);

  useEffect(() => {
    if (!run) return;
    
    if (run.turns.length > prevTurnsLengthRef.current) {
      if (isPinnedToBottom) {
        bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
      } else {
        setUnseenTurns(prev => prev + (run.turns.length - prevTurnsLengthRef.current));
      }
    }
    prevTurnsLengthRef.current = run.turns.length;
  }, [run?.turns.length, isPinnedToBottom, run]);

  useEffect(() => {
    const observer = new IntersectionObserver(
      ([entry]) => {
        setIsPinnedToBottom(entry.isIntersecting);
        if (entry.isIntersecting) {
          setUnseenTurns(0);
        }
      },
      {
        root: scrollContainerRef.current,
        threshold: 0.1,
      }
    );

    if (bottomRef.current) {
      observer.observe(bottomRef.current);
    }

    return () => {
      observer.disconnect();
    };
  }, []);

  if (!run) {
    return <div className="text-slate-500 p-4 font-mono text-xs">No active debate found.</div>;
  }

  const displaySymbol = (!run.instrumentId || run.instrumentId === 'UNKNOWN') 
    ? (symbol ? symbol.replace('.BINANCE', '') : 'LIVE SESSION')
    : run.instrumentId.replace('.BINANCE', '');

  const getRoleInfo = (role: string) => {
    const r = role.toLowerCase();
    if (r.includes('bull researcher')) return { color: 'text-emerald-400 border-emerald-500/25 bg-emerald-950/20', Icon: TrendingUp };
    if (r.includes('bear researcher')) return { color: 'text-rose-400 border-rose-500/25 bg-rose-950/20', Icon: TrendingDown };
    if (r.includes('portfolio manager')) return { color: 'text-violet-400 border-violet-500/25 bg-violet-950/20', Icon: Scale };
    if (r.includes('chief risk officer')) return { color: 'text-amber-400 border-amber-500/25 bg-amber-950/20', Icon: ShieldAlert };
    return { color: 'text-cyan-400 border-cyan-500/25 bg-cyan-950/20', Icon: Search }; // Analysts
  };

  const scrollToBottom = () => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
    setUnseenTurns(0);
  };

  const isLive = run.status === 'RUNNING';

  return (
    <div className="flex flex-col h-full bg-zinc-950 border border-slate-800 rounded-lg overflow-hidden relative min-w-0 max-w-full">
      <div className="p-3 border-b border-slate-800 bg-zinc-900 flex justify-between items-center z-10 flex-none select-none">
        <div className="flex items-center gap-2.5 min-w-0">
          <h3 className="font-semibold text-slate-200 text-xs truncate">
            AI Debate: <span className="text-cyan-400 font-mono">{displaySymbol}</span>
          </h3>
          {isLive && (
            <div className="flex items-center gap-1.5 px-2 py-0.5 rounded-full bg-rose-500/10 border border-rose-500/20 flex-none">
              <div className="w-1.5 h-1.5 rounded-full bg-rose-500 animate-pulse" />
              <span className="text-[9px] font-bold tracking-wider text-rose-400">LIVE</span>
            </div>
          )}
        </div>
        <span className={`px-2 py-0.5 text-[10px] font-mono font-bold rounded flex-none ${
            run.status === 'COMPLETED' ? 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30' :
            run.status === 'PENDING_APPROVAL' ? 'bg-amber-500/20 text-amber-400 border border-amber-500/30 animate-pulse' :
            'bg-cyan-500/20 text-cyan-400 border border-cyan-500/30'
        }`}>
            {run.status}
        </span>
      </div>
      
      <div 
        ref={scrollContainerRef}
        role="region"
        aria-label={`AI debate transcript for ${displaySymbol}`}
        aria-live="polite"
        tabIndex={0}
        className="flex-1 overflow-y-auto p-3 space-y-3 font-mono text-xs relative scroll-smooth focus-visible:ring-1 focus-visible:ring-cyan-500/30 focus:outline-none min-w-0 max-w-full"
      >
        {run.turns.map((turn, idx) => {
          const { color, Icon } = getRoleInfo(turn.role);
          return (
            <div key={idx} className={`p-3 rounded border ${color} min-w-0 max-w-full overflow-hidden shadow-sm`}>
              <div className="flex items-center gap-2 font-bold mb-2 uppercase tracking-wide opacity-90 text-[11px]">
                <Icon size={14} aria-hidden="true" className="flex-none" />
                <span className="truncate">{turn.role}</span>
              </div>
              <div className="prose prose-invert prose-xs max-w-full min-w-0 overflow-hidden text-zinc-300">
                <ReactMarkdown 
                  remarkPlugins={[remarkGfm]}
                  components={{
                    table: ({ node, ...props }) => (
                      <div className="overflow-x-auto my-2 max-w-full rounded border border-white/10 bg-zinc-950/80 scrollbar-thin">
                        <table className="min-w-full border-collapse text-[11px] font-mono" {...props} />
                      </div>
                    ),
                    thead: ({ node, ...props }) => (
                      <thead className="bg-zinc-900 border-b border-white/10 text-zinc-200" {...props} />
                    ),
                    th: ({ node, ...props }) => (
                      <th className="px-2.5 py-1.5 text-left font-bold text-[10px] tracking-wider uppercase border-r border-white/5 whitespace-nowrap" {...props} />
                    ),
                    td: ({ node, ...props }) => (
                      <td className="px-2.5 py-1 text-[11px] text-zinc-300 border-b border-white/5 border-r border-white/5 align-top whitespace-normal break-words max-w-[200px]" {...props} />
                    ),
                    pre: ({ node, ...props }) => (
                      <div className="overflow-x-auto max-w-full my-2">
                        <pre className="p-2 rounded bg-black/60 font-mono text-[10px] text-zinc-200 border border-white/10" {...props} />
                      </div>
                    ),
                    code: ({ node, className, children, ...props }: any) => {
                      return (
                        <code className={`font-mono text-[10px] px-1 py-0.5 rounded bg-zinc-900/80 text-cyan-300 border border-white/10 ${className || ''}`} {...props}>
                          {children}
                        </code>
                      );
                    },
                    p: ({ node, ...props }) => (
                      <p className="mb-1.5 leading-relaxed break-words text-[11px]" {...props} />
                    ),
                  }}
                >
                  {turn.content}
                </ReactMarkdown>
                {turn.isStreaming && (
                  <span className="inline-block w-1.5 h-3.5 bg-cyan-400 animate-pulse ml-1 align-middle" />
                )}
              </div>
            </div>
          );
        })}
        <div ref={bottomRef} className="h-4" />
      </div>

      {!isPinnedToBottom && unseenTurns > 0 && (
        <button
          onClick={scrollToBottom}
          className="absolute bottom-6 left-1/2 -translate-x-1/2 flex items-center gap-2 px-4 py-2 bg-slate-800 hover:bg-slate-700 text-cyan-400 border border-cyan-500/30 rounded-full shadow-lg transition-all z-20 text-sm font-semibold focus-visible:ring-2 focus-visible:ring-cyan-500 focus-visible:outline-none"
          aria-label={`Scroll to bottom, ${unseenTurns} new turns`}
        >
          <ArrowDown size={16} />
          {unseenTurns} new turn{unseenTurns !== 1 ? 's' : ''} below
        </button>
      )}
    </div>
  );
};
