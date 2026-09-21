import React, { useRef, useEffect, useState, useMemo } from 'react';
import { useDebateStore } from '@/lib/stores/useDebateStore';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { TrendingUp, TrendingDown, ShieldAlert, Scale, Search, ArrowDown } from 'lucide-react';

interface Props {
  runId: string;
}

export const DebateTranscriptViewer: React.FC<Props> = ({ runId }) => {
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
    return <div className="text-slate-500 p-4">No active debate found.</div>;
  }

  const getRoleInfo = (role: string) => {
    const r = role.toLowerCase();
    if (r.includes('bull researcher')) return { color: 'text-emerald-500 border-emerald-500/20 bg-emerald-500/10', Icon: TrendingUp };
    if (r.includes('bear researcher')) return { color: 'text-rose-500 border-rose-500/20 bg-rose-500/10', Icon: TrendingDown };
    if (r.includes('portfolio manager')) return { color: 'text-violet-500 border-violet-500/20 bg-violet-500/10', Icon: Scale };
    if (r.includes('chief risk officer')) return { color: 'text-amber-500 border-amber-500/20 bg-amber-500/10', Icon: ShieldAlert };
    return { color: 'text-cyan-500 border-cyan-500/20 bg-cyan-500/10', Icon: Search }; // Analysts
  };

  const scrollToBottom = () => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
    setUnseenTurns(0);
  };

  const isLive = run.status === 'RUNNING';

  return (
    <div className="flex flex-col h-full bg-zinc-950 border border-slate-800 rounded-lg overflow-hidden relative">
      <div className="p-3 border-b border-slate-800 bg-zinc-900 flex justify-between items-center z-10">
        <div className="flex items-center gap-3">
          <h3 className="font-semibold text-slate-200">AI Debate: {run.instrumentId}</h3>
          {isLive && (
            <div className="flex items-center gap-1.5 px-2 py-0.5 rounded-full bg-rose-500/10 border border-rose-500/20">
              <div className="w-2 h-2 rounded-full bg-rose-500 animate-pulse" />
              <span className="text-[10px] font-bold tracking-wider text-rose-500">LIVE STREAM</span>
            </div>
          )}
        </div>
        <span className={`px-2 py-1 text-xs font-mono rounded ${
            run.status === 'COMPLETED' ? 'bg-emerald-500/20 text-emerald-400' :
            run.status === 'PENDING_APPROVAL' ? 'bg-amber-500/20 text-amber-400 animate-pulse' :
            'bg-cyan-500/20 text-cyan-400'
        }`}>
            {run.status}
        </span>
      </div>
      
      <div 
        ref={scrollContainerRef}
        role="region"
        aria-label={`AI debate transcript for ${run.instrumentId}`}
        aria-live="polite"
        tabIndex={0}
        className="flex-1 overflow-y-auto p-4 space-y-4 font-mono text-sm relative scroll-smooth focus-visible:ring-1 focus-visible:ring-cyan-500/30 focus:outline-none"
      >
        {run.turns.map((turn, idx) => {
          const { color, Icon } = getRoleInfo(turn.role);
          return (
            <div key={idx} className={`p-4 rounded-md border ${color}`}>
              <div className="flex items-center gap-2 font-bold mb-3 uppercase opacity-90">
                <Icon size={16} aria-hidden="true" />
                {turn.role}
              </div>
              <div className="prose prose-invert prose-sm max-w-none prose-pre:bg-black/50 prose-pre:border prose-pre:border-slate-800">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>
                  {turn.content}
                </ReactMarkdown>
                {turn.isStreaming && (
                  <span className="inline-block w-2 h-4 bg-cyan-400 animate-pulse ml-1 align-middle" />
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
