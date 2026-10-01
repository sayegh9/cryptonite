import { create } from 'zustand';
import { persist } from 'zustand/middleware';

export interface DebateTurn {
  runId: string;
  role: string;
  content: string;
  timestamp: number;
  instrumentId?: string;
  isStreaming?: boolean;
}

export interface DebateRun {
  runId: string;
  instrumentId: string;
  // NO_TRADE: the verdict was HOLD, so there is nothing to approve
  status: 'PENDING' | 'RUNNING' | 'COMPLETED' | 'PENDING_APPROVAL' | 'FAILED' | 'REJECTED' | 'NO_TRADE';
  decision?: string;
  timestamp?: number;
  turns: DebateTurn[];
}

interface DebateState {
  activeDebates: Record<string, DebateRun>; // Keyed by runId
  addTurn: (turn: DebateTurn) => void;
  addDebateRun: (run: DebateRun) => void;
  updateStatus: (runId: string, instrumentId: string, status: DebateRun['status'], decision?: string) => void;
  approveDebate: (runId: string, overrides?: any) => Promise<void>;
  rejectDebate: (runId: string) => Promise<void>;
  fetchDecisionData: (runId: string) => Promise<any>;
  removeDebate: (runId: string) => void;
  clearCompleted: () => void;
  setDebates: (debates: Record<string, DebateRun>) => void;
  clearAll: () => void;
}

export const useDebateStore = create<DebateState>()(persist((set) => ({
  activeDebates: {},
  removeDebate: (runId) => set((state) => {
    const next = { ...state.activeDebates };
    delete next[runId];
    return { activeDebates: next };
  }),
  setDebates: (debates) => set({ activeDebates: debates }),
  clearCompleted: () => set((state) => {
    const next: Record<string, DebateRun> = {};
    for (const [id, run] of Object.entries(state.activeDebates)) {
      if (run.status !== 'COMPLETED' && run.status !== 'REJECTED' && run.status !== 'FAILED' && run.status !== 'NO_TRADE') {
        next[id] = run;
      }
    }
    return { activeDebates: next };
  }),
  clearAll: () => set({ activeDebates: {} }),
  addDebateRun: (run) => set((state) => ({
    activeDebates: {
      ...state.activeDebates,
      [run.runId]: run,
    }
  })),
  addTurn: (turn) => set((state) => {
    const existing = state.activeDebates[turn.runId];
    let instId = turn.instrumentId || (turn as any).instrument_id || existing?.instrumentId;
    if (!instId || instId === 'UNKNOWN') {
      const match = turn.content?.match(/\b(BTCUSDT|ETHUSDT|SOLUSDT|XRPUSDT|BNBUSDT|DOGEUSDT|ADAUSDT|AVAXUSDT|SUIUSDT|LINKUSDT|BTC|ETH|SOL|XRP|BNB|DOGE)\b/i);
      instId = match ? (match[1].toUpperCase().includes('USDT') ? match[1].toUpperCase() + '.BINANCE' : match[1].toUpperCase() + 'USDT.BINANCE') : 'BTCUSDT.BINANCE';
    }

    const run = existing || { runId: turn.runId, instrumentId: instId, status: 'RUNNING', turns: [] };
    return {
      activeDebates: {
        ...state.activeDebates,
        [turn.runId]: {
          ...run,
          instrumentId: run.instrumentId === 'UNKNOWN' ? instId : run.instrumentId,
          turns: [...run.turns, turn]
        }
      }
    };
  }),
  updateStatus: (runId, instrumentId, status, decision) => set((state) => {
    const existing = state.activeDebates[runId];
    const resolvedInst = instrumentId && instrumentId !== 'UNKNOWN' ? instrumentId : (existing?.instrumentId || 'BTCUSDT.BINANCE');
    const run = existing || { runId, instrumentId: resolvedInst, status: 'RUNNING', turns: [] };
    return {
      activeDebates: {
        ...state.activeDebates,
        [runId]: {
          ...run,
          instrumentId: resolvedInst,
          status,
          ...(decision ? { decision } : {})
        }
      }
    };
  }),
  approveDebate: async (runId, overrides) => {
    const csrf = typeof window !== 'undefined' ? localStorage.getItem('csrf_token') : null;

    // In simulation mode, complete immediately in store
    if (runId.startsWith('demo-') || runId.startsWith('sim-') || !csrf) {
      set((state) => ({
        activeDebates: {
          ...state.activeDebates,
          [runId]: { ...state.activeDebates[runId], status: 'COMPLETED' }
        }
      }));
      return;
    }

    try {
        const res = await fetch(`/api/v1/agents/debates/${runId}/approve`, {
            method: 'POST',
            headers: { 
              'Content-Type': 'application/json',
              'X-CSRF-Token': csrf
            },
            body: overrides ? JSON.stringify(overrides) : undefined,
            credentials: 'include',
        });
        if (!res.ok) {
            const detail = await res.text();
            let parsedMsg = `Approval failed (${res.status})`;
            try {
              const j = JSON.parse(detail);
              if (j.detail) parsedMsg = j.detail;
            } catch {
              if (detail) parsedMsg = detail;
            }
            if (res.status === 409 && parsedMsg.includes('FAILED')) {
              set((state) => ({
                activeDebates: {
                  ...state.activeDebates,
                  [runId]: { ...state.activeDebates[runId], status: 'FAILED' }
                }
              }));
            }
            const err: any = new Error(parsedMsg);
            err.status = res.status;
            throw err;
        }
        set((state) => ({
            activeDebates: {
                ...state.activeDebates,
                [runId]: { ...state.activeDebates[runId], status: 'COMPLETED' }
            }
        }));
    } catch (e: any) {
        if (e.status !== 409) {
          set((state) => ({
              activeDebates: {
                  ...state.activeDebates,
                  [runId]: { ...state.activeDebates[runId], status: 'COMPLETED' }
              }
          }));
        }
        if (e.status !== 401 && e.status !== 409) {
          console.error('Failed to approve debate', e);
        }
        throw e;
    }
  },
  rejectDebate: async (runId) => {
    const csrf = typeof window !== 'undefined' ? localStorage.getItem('csrf_token') : null;

    if (runId.startsWith('demo-') || runId.startsWith('sim-') || !csrf) {
      set((state) => ({
        activeDebates: {
          ...state.activeDebates,
          [runId]: { ...state.activeDebates[runId], status: 'COMPLETED', decision: 'REJECTED' }
        }
      }));
      return;
    }

    try {
        const res = await fetch(`/api/v1/agents/debates/${runId}/reject`, {
            method: 'POST',
            headers: { 
              'Content-Type': 'application/json',
              'X-CSRF-Token': csrf
            },
            credentials: 'include',
        });
        if (!res.ok) {
            const detail = await res.text();
            let parsedMsg = `Rejection failed (${res.status})`;
            try {
              const j = JSON.parse(detail);
              if (j.detail) parsedMsg = j.detail;
            } catch {
              if (detail) parsedMsg = detail;
            }
            // If it's already failed or completed, just mark it as handled locally
            if (res.status === 400 && parsedMsg.includes("invalid status for rejection")) {
                set((state) => ({
                    activeDebates: {
                        ...state.activeDebates,
                        [runId]: { ...state.activeDebates[runId], status: 'COMPLETED', decision: 'REJECTED' }
                    }
                }));
                return;
            }
            const err: any = new Error(parsedMsg);
            err.status = res.status;
            throw err;
        }
        set((state) => ({
            activeDebates: {
                ...state.activeDebates,
                [runId]: { ...state.activeDebates[runId], status: 'COMPLETED', decision: 'REJECTED' }
            }
        }));
    } catch (e: any) {
        set((state) => ({
            activeDebates: {
                ...state.activeDebates,
                [runId]: { ...state.activeDebates[runId], status: 'COMPLETED', decision: 'REJECTED' }
            }
        }));
        if (e.status !== 401) {
          console.error('Failed to reject debate', e);
        }
        throw e;
    }
  },
  fetchDecisionData: async (runId: string) => {
    try {
        const res = await fetch(`/api/v1/agents/debates/${runId}/decision`);
        if (!res.ok) return null;
        const data = await res.json();
        return data;
    } catch (e) {
        console.error('Failed to fetch decision data', e);
        return null;
    }
  },
}), { name: 'debate-storage' }));

import { useEffect } from 'react';
import { CryptoniteStreamClient } from '../ws';

const RUN_STATUSES: DebateRun['status'][] = ['PENDING', 'RUNNING', 'COMPLETED', 'PENDING_APPROVAL', 'FAILED', 'REJECTED', 'NO_TRADE'];

/** Status of a run from its DEBATE_COMPLETED event: the worker's status when sent, else from the verdict. */
export function completedStatus(msg: { status?: string; decision?: unknown }): DebateRun['status'] {
  const status = String(msg.status ?? '').toUpperCase();
  if ((RUN_STATUSES as string[]).includes(status)) return status as DebateRun['status'];
  const verdict = String(msg.decision ?? '').toUpperCase();
  if (verdict === 'HOLD' || verdict === 'NEUTRAL' || verdict === 'NO_TRADE') return 'NO_TRADE';
  // Autonomous execution is frozen (A0.6): a finished debate with a trade waits for the operator
  return 'PENDING_APPROVAL';
}

export const useDebateStream = (client: CryptoniteStreamClient | null) => {
  const addTurn = useDebateStore((state) => state.addTurn);
  const updateStatus = useDebateStore((state) => state.updateStatus);

  useEffect(() => {
    if (!client) return;

    const unsub = client.subscribe((msg: any) => {
      if (msg.type === 'DEBATE_TURN') {
        const inst = msg.instrument_id || msg.instrumentId || msg.symbol;
        addTurn({
          runId: msg.run_id,
          role: msg.role,
          content: msg.content,
          instrumentId: inst,
          timestamp: msg.timestamp || Date.now(),
        });
        // Status comes from the worker (a lite debate may finish without a Chief Risk Officer turn)
        if (msg.status === 'PENDING_APPROVAL' || msg.status === 'NO_TRADE') {
          updateStatus(msg.run_id, inst || 'BTCUSDT.BINANCE', msg.status);
        }
      } else if (msg.type === 'DEBATE_COMPLETED') {
        updateStatus(msg.run_id, msg.instrument_id || msg.instrumentId || 'BTCUSDT.BINANCE', completedStatus(msg), msg.decision);
      }
    });

    return () => unsub();
  }, [client, addTurn, updateStatus]);
};
