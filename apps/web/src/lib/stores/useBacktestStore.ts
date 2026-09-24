import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import { Strategy, BacktestRun } from '@/lib/types';

interface BacktestState {
  activeRun: BacktestRun | null;
  strategies: Strategy[];
  selectedSlug: string;
  setActiveRun: (run: BacktestRun | null) => void;
  setStrategies: (strategies: Strategy[]) => void;
  setSelectedSlug: (slug: string) => void;
}

export const useBacktestStore = create<BacktestState>()(persist((set) => ({
  activeRun: null,
  strategies: [],
  selectedSlug: "ema-cross",
  setActiveRun: (run) => set({ activeRun: run }),
  setStrategies: (strategies) => set({ strategies }),
  setSelectedSlug: (slug) => set({ selectedSlug: slug }),
}), { name: 'backtest-storage' }));
