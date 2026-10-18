import { create } from "zustand";
import { persist } from "zustand/middleware";
import type { WorkspaceMode } from "@/lib/navigation";

export type DeskTheme = "dark" | "light" | "system";
export type DeskDensity = "compact" | "comfortable";
export type QuoteCurrency = "USD" | "USDT";
export type PnlFormat = "pct" | "usd" | "bps";
export type DecimalPrecision = "auto" | "2" | "4";
export type DeskTimezone = "UTC" | "local";
export type ChimeKind = "tradeFill" | "debateVerdict" | "riskBreach" | "killSwitch";

export interface ChimeEvents {
  tradeFill: boolean;
  debateVerdict: boolean;
  riskBreach: boolean;
  killSwitch: boolean;
}

export interface DeskPreferences {
  theme: DeskTheme;
  density: DeskDensity;
  audioAlerts: boolean;
  audioVolume: number; // 0-100
  chimeEvents: ChimeEvents;
  quoteCurrency: QuoteCurrency;
  pnlFormat: PnlFormat;
  decimalPrecision: DecimalPrecision;
  defaultWorkspace: WorkspaceMode;
  timezone: DeskTimezone;
}

export interface DeskPreferencesState extends DeskPreferences {
  setTheme: (theme: DeskTheme) => void;
  setDensity: (density: DeskDensity) => void;
  setAudioAlerts: (audioAlerts: boolean) => void;
  setAudioVolume: (volume: number) => void;
  setChimeEvent: (event: keyof ChimeEvents, enabled: boolean) => void;
  setQuoteCurrency: (currency: QuoteCurrency) => void;
  setPnlFormat: (format: PnlFormat) => void;
  setDecimalPrecision: (precision: DecimalPrecision) => void;
  setDefaultWorkspace: (workspace: WorkspaceMode) => void;
  setTimezone: (timezone: DeskTimezone) => void;
  updatePreferences: (partial: Partial<DeskPreferences>) => void;
  resetPreferences: () => void;
}

export const DEFAULT_DESK_PREFERENCES: DeskPreferences = {
  theme: "dark",
  density: "compact",
  audioAlerts: true,
  audioVolume: 70,
  chimeEvents: {
    tradeFill: true,
    debateVerdict: true,
    riskBreach: true,
    killSwitch: true,
  },
  quoteCurrency: "USDT",
  pnlFormat: "pct",
  decimalPrecision: "auto",
  defaultWorkspace: "mission",
  timezone: "UTC",
};

export function resolveTheme(theme: DeskTheme): "dark" | "light" {
  if (theme === "system") {
    if (typeof window !== "undefined" && window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) {
      return "dark";
    }
    return "light";
  }
  return theme;
}

export function applyDeskDOMPreferences(prefs: Pick<DeskPreferences, "theme" | "density">) {
  if (typeof document === "undefined") return;
  const resolved = resolveTheme(prefs.theme);
  document.documentElement.dataset.theme = resolved;
  document.documentElement.dataset.density = prefs.density;
  document.documentElement.classList.toggle("dark", resolved === "dark");
}

let sharedAudioCtx: AudioContext | null = null;

function getAudioContext(): AudioContext | null {
  if (typeof window === "undefined") return null;
  const AudioCtx = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
  if (!AudioCtx) return null;
  if (!sharedAudioCtx || sharedAudioCtx.state === "closed") {
    try {
      sharedAudioCtx = new AudioCtx();
    } catch {
      return null;
    }
  }
  if (sharedAudioCtx.state === "suspended") {
    void sharedAudioCtx.resume().catch(() => {});
  }
  return sharedAudioCtx;
}

export function playSynthChime(kind: ChimeKind = "tradeFill") {
  if (typeof window === "undefined") return;
  try {
    const prefs = useDeskPreferences.getState();
    if (!prefs.audioAlerts) return;
    if (kind && !prefs.chimeEvents[kind]) return;

    const ctx = getAudioContext();
    if (!ctx) return;

    const now = ctx.currentTime;
    const vol = Math.max(0, Math.min(100, prefs.audioVolume)) / 100;
    const masterGainValue = 0.15 * vol;
    if (masterGainValue <= 0.0001) return;

    const masterGain = ctx.createGain();
    masterGain.gain.setValueAtTime(masterGainValue, now);
    masterGain.connect(ctx.destination);

    if (kind === "tradeFill") {
      // Crisp institutional two-tone ascending chime (D5 -> A5)
      const osc1 = ctx.createOscillator();
      const osc2 = ctx.createOscillator();
      const g1 = ctx.createGain();
      const g2 = ctx.createGain();

      osc1.type = "sine";
      osc1.frequency.setValueAtTime(587.33, now); // D5
      g1.gain.setValueAtTime(0.7, now);
      g1.gain.exponentialRampToValueAtTime(0.001, now + 0.18);
      osc1.connect(g1);
      g1.connect(masterGain);

      osc2.type = "sine";
      osc2.frequency.setValueAtTime(880.0, now + 0.08); // A5
      g2.gain.setValueAtTime(0, now);
      g2.gain.setValueAtTime(0.9, now + 0.08);
      g2.gain.exponentialRampToValueAtTime(0.001, now + 0.35);
      osc2.connect(g2);
      g2.connect(masterGain);

      osc1.start(now);
      osc1.stop(now + 0.2);
      osc2.start(now + 0.08);
      osc2.stop(now + 0.38);
    } else if (kind === "debateVerdict") {
      // Harmonic resonance triad (A4 -> C#5 -> E5)
      const freqs = [440, 554.37, 659.25];
      freqs.forEach((freq, idx) => {
        const osc = ctx.createOscillator();
        const g = ctx.createGain();
        const startTime = now + idx * 0.06;
        osc.type = "triangle";
        osc.frequency.setValueAtTime(freq, startTime);
        g.gain.setValueAtTime(0, now);
        g.gain.setValueAtTime(0.6, startTime);
        g.gain.exponentialRampToValueAtTime(0.001, startTime + 0.4);
        osc.connect(g);
        g.connect(masterGain);
        osc.start(startTime);
        osc.stop(startTime + 0.45);
      });
    } else if (kind === "riskBreach") {
      // Cautionary dual minor tone pulse (440Hz -> 370Hz)
      const osc = ctx.createOscillator();
      const g = ctx.createGain();
      osc.type = "sine";
      osc.frequency.setValueAtTime(440, now);
      osc.frequency.setValueAtTime(370, now + 0.12);
      g.gain.setValueAtTime(0.8, now);
      g.gain.exponentialRampToValueAtTime(0.01, now + 0.11);
      g.gain.setValueAtTime(0.8, now + 0.12);
      g.gain.exponentialRampToValueAtTime(0.001, now + 0.35);
      osc.connect(g);
      g.connect(masterGain);
      osc.start(now);
      osc.stop(now + 0.38);
    } else if (kind === "killSwitch") {
      // Authoritative descending tone (330Hz -> 165Hz)
      const osc = ctx.createOscillator();
      const g = ctx.createGain();
      osc.type = "sawtooth";
      osc.frequency.setValueAtTime(330, now);
      osc.frequency.exponentialRampToValueAtTime(165, now + 0.4);
      g.gain.setValueAtTime(0.5, now);
      g.gain.exponentialRampToValueAtTime(0.001, now + 0.45);
      osc.connect(g);
      g.connect(masterGain);
      osc.start(now);
      osc.stop(now + 0.48);
    }
  } catch {
    // Non-fatal if audio context cannot play in current environment
  }
}

export const useDeskPreferences = create<DeskPreferencesState>()(
  persist(
    (set, get) => ({
      ...DEFAULT_DESK_PREFERENCES,

      setTheme: (theme) => {
        set({ theme });
        applyDeskDOMPreferences({ theme, density: get().density });
      },

      setDensity: (density) => {
        set({ density });
        applyDeskDOMPreferences({ theme: get().theme, density });
      },

      setAudioAlerts: (audioAlerts) => set({ audioAlerts }),

      setAudioVolume: (audioVolume) =>
        set({ audioVolume: Math.max(0, Math.min(100, Math.round(audioVolume))) }),

      setChimeEvent: (event, enabled) =>
        set((state) => ({
          chimeEvents: {
            ...state.chimeEvents,
            [event]: enabled,
          },
        })),

      setQuoteCurrency: (quoteCurrency) => set({ quoteCurrency }),

      setPnlFormat: (pnlFormat) => set({ pnlFormat }),

      setDecimalPrecision: (decimalPrecision) => set({ decimalPrecision }),

      setDefaultWorkspace: (defaultWorkspace) => set({ defaultWorkspace }),

      setTimezone: (timezone) => set({ timezone }),

      updatePreferences: (partial) => {
        set((state) => {
          const next = { ...state, ...partial };
          applyDeskDOMPreferences(next);
          return next;
        });
      },

      resetPreferences: () => {
        set(DEFAULT_DESK_PREFERENCES);
        applyDeskDOMPreferences(DEFAULT_DESK_PREFERENCES);
      },
    }),
    {
      name: "cryptonite:desk-prefs",
      onRehydrateStorage: () => (state) => {
        if (state) {
          applyDeskDOMPreferences(state);
        }
      },
    },
  ),
);
