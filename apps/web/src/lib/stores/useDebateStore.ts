import { create } from 'zustand';

export interface DebateTurn {
  runId: string;
  role: string;
  content: string;
  timestamp: number;
  isStreaming?: boolean;
}

export interface DebateRun {
  runId: string;
  instrumentId: string;
  status: 'PENDING' | 'RUNNING' | 'COMPLETED' | 'PENDING_APPROVAL' | 'FAILED';
  decision?: string;
  turns: DebateTurn[];
}

interface DebateState {
  activeDebates: Record<string, DebateRun>; // Keyed by runId
  addTurn: (turn: DebateTurn) => void;
  updateStatus: (runId: string, instrumentId: string, status: DebateRun['status'], decision?: string) => void;
  approveDebate: (runId: string) => Promise<void>;
  rejectDebate: (runId: string) => Promise<void>;
}

export const useDebateStore = create<DebateState>((set, get) => ({
  activeDebates: {},
  addTurn: (turn) => set((state) => {
    const run = state.activeDebates[turn.runId] || { runId: turn.runId, instrumentId: 'UNKNOWN', status: 'RUNNING', turns: [] };
    return {
      activeDebates: {
        ...state.activeDebates,
        [turn.runId]: {
          ...run,
          turns: [...run.turns, turn]
        }
      }
    };
  }),
  updateStatus: (runId, instrumentId, status, decision) => set((state) => {
    const run = state.activeDebates[runId] || { runId, instrumentId, status: 'RUNNING', turns: [] };
    return {
      activeDebates: {
        ...state.activeDebates,
        [runId]: {
          ...run,
          instrumentId,
          status,
          ...(decision ? { decision } : {})
        }
      }
    };
  }),
  approveDebate: async (runId) => {
    try {
        await fetch(`/api/v1/debates/${runId}/approve`, { method: 'POST' });
        set((state) => ({
            activeDebates: {
                ...state.activeDebates,
                [runId]: { ...state.activeDebates[runId], status: 'COMPLETED' }
            }
        }));
    } catch (e) {
        console.error('Failed to approve debate', e);
    }
  },
  rejectDebate: async (runId) => {
    try {
        await fetch(`/api/v1/debates/${runId}/reject`, { method: 'POST' });
        set((state) => ({
            activeDebates: {
                ...state.activeDebates,
                [runId]: { ...state.activeDebates[runId], status: 'COMPLETED', decision: 'REJECTED' }
            }
        }));
    } catch (e) {
        console.error('Failed to reject debate', e);
    }
  },
}));

import { useEffect } from 'react';
import { CryptoniteStreamClient } from '../ws';

export const useDebateStream = (client: CryptoniteStreamClient | null, instrumentId?: string) => {
  const addTurn = useDebateStore((state) => state.addTurn);
  const updateStatus = useDebateStore((state) => state.updateStatus);

  useEffect(() => {
    if (!client) return;

    const unsub = client.subscribe((msg: any) => {
      // Assuming debate messages come wrapped with channel or type
      if (msg.type === 'DEBATE_TURN') {
        addTurn({
          runId: msg.run_id,
          role: msg.role,
          content: msg.content,
          timestamp: msg.timestamp || Date.now(),
        });
        if (msg.role === 'Chief Risk Officer') {
          updateStatus(msg.run_id, msg.instrument_id || 'UNKNOWN', 'PENDING_APPROVAL');
        }
      } else if (msg.type === 'DEBATE_COMPLETED') {
        updateStatus(msg.run_id, msg.instrument_id || 'UNKNOWN', 'COMPLETED', msg.decision);
      }
    });

    return () => unsub();
  }, [client, addTurn, updateStatus]);
};
