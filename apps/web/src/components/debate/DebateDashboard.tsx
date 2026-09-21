import React from 'react';
import { useDebateStore } from '@/lib/stores/useDebateStore';
import { DebateTranscriptViewer } from './DebateTranscriptViewer';
import { InteractiveModeToggle } from './InteractiveModeToggle';

export const DebateDashboard: React.FC = () => {
  const activeDebates = useDebateStore((state) => state.activeDebates);
  const debatesList = Object.values(activeDebates);

  return (
    <div className="flex flex-col h-full bg-zinc-950 p-4 overflow-y-auto">
      <div className="mb-6">
        <h2 className="text-xl font-bold text-zinc-100 mb-2 uppercase tracking-widest">AI Adversarial Debates</h2>
        <p className="text-sm text-zinc-400">
          Live feed of autonomous TradingAgents debating market intelligence, risk, and portfolio sizing.
        </p>
      </div>

      {debatesList.length === 0 ? (
        <div className="flex-1 flex items-center justify-center border border-dashed border-zinc-800 rounded-lg">
          <p className="text-zinc-600 font-mono text-sm">Waiting for signals from Scanner to trigger debates...</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
          {debatesList.map((run) => (
            <div key={run.runId} className="h-[600px] flex flex-col">
              <DebateTranscriptViewer runId={run.runId} />
              <InteractiveModeToggle runId={run.runId} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
};
