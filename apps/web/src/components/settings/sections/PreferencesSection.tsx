"use client";

import React from "react";
import {
  Sun,
  Moon,
  Monitor,
  Volume2,
  VolumeX,
  Play,
  RotateCcw,
  Sliders,
  DollarSign,
  LayoutGrid,
} from "lucide-react";
import { toast } from "sonner";
import {
  useDeskPreferences,
  playSynthChime,
  type DeskTheme,
  type DeskDensity,
  type QuoteCurrency,
  type PnlFormat,
  type DecimalPrecision,
  type DeskTimezone,
} from "@/lib/stores/useDeskPreferences";
import type { WorkspaceMode } from "@/lib/navigation";
import { Button } from "@/components/ui/button";

const WORKSPACE_OPTIONS: { id: WorkspaceMode; label: string; hint: string }[] = [
  { id: "mission", label: "Mission Control", hint: "Mandates & autonomy overview" },
  { id: "terminal", label: "Terminal", hint: "Trading charts & execution" },
  { id: "debates", label: "AI Brain", hint: "Consensus debates & thesis audit" },
  { id: "portfolio", label: "Portfolio", hint: "Positions, balances & PnL" },
  { id: "markets", label: "Markets", hint: "Live prices & orderbook depth" },
  { id: "research", label: "Research Lab", hint: "Backtesting & alpha discovery" },
];

export const PreferencesSection: React.FC = () => {
  const prefs = useDeskPreferences();

  const handleTestChime = (kind: "tradeFill" | "debateVerdict" | "riskBreach" | "killSwitch", label: string) => {
    playSynthChime(kind);
    toast.info(`Playing ${label} chime`);
  };

  const handleReset = () => {
    prefs.resetPreferences();
    toast.success("Desk preferences reset to institutional defaults");
  };

  return (
    <div className="space-y-6 max-w-4xl">
      {/* Header Banner */}
      <div className="flex flex-wrap items-center justify-between gap-4 border-b border-white/10 pb-4">
        <div>
          <h2 className="text-xl font-bold tracking-tight text-white flex items-center gap-2">
            <Sliders className="w-5 h-5 text-cyan-400" aria-hidden="true" />
            Desk Preferences
          </h2>
          <p className="text-xs text-zinc-400 mt-1">
            Personal workstation environment stored in your local browser profile. Active in both operator and anonymous mode.
          </p>
        </div>
        <Button
          type="button"
          onClick={handleReset}
          variant="outline"
          className="border-white/10 bg-zinc-900/60 hover:bg-zinc-800 text-zinc-300 text-xs gap-1.5"
        >
          <RotateCcw className="w-3.5 h-3.5 text-zinc-400" aria-hidden="true" />
          Reset Desk Preferences
        </Button>
      </div>

      {/* Grid of Preference Cards */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        {/* Card 1: Theme & Visual Appearance */}
        <div className="rounded-xl border border-white/10 bg-zinc-950/60 p-5 space-y-4">
          <div className="flex items-center justify-between">
            <div>
              <h3 className="text-sm font-semibold text-zinc-100 flex items-center gap-2">
                <Sun className="w-4 h-4 text-amber-400" aria-hidden="true" />
                Workstation Theme
              </h3>
              <p className="text-[11px] text-zinc-400 mt-0.5">
                Visual contrast and ambient display profile
              </p>
            </div>
            <span className="text-[10px] uppercase font-mono px-2 py-0.5 rounded border border-white/10 text-cyan-300 bg-cyan-950/30">
              {prefs.theme}
            </span>
          </div>

          <div aria-label="Workstation Theme" className="grid grid-cols-3 gap-2">
            {[
              { id: "dark" as DeskTheme, label: "Dark", icon: Moon },
              { id: "light" as DeskTheme, label: "Light", icon: Sun },
              { id: "system" as DeskTheme, label: "System", icon: Monitor },
            ].map(({ id, label, icon: Icon }) => {
              const active = prefs.theme === id;
              return (
                <button
                  key={id}
                  type="button"
                  aria-pressed={active}
                  onClick={() => prefs.setTheme(id)}
                  className={`flex flex-col items-center justify-center gap-2 py-3 px-2 rounded-lg border text-xs font-medium transition-all ${
                    active
                      ? "border-cyan-500 bg-cyan-950/40 text-cyan-300 shadow-sm shadow-cyan-950/50"
                      : "border-white/10 bg-zinc-900/40 text-zinc-400 hover:bg-zinc-900 hover:text-zinc-200"
                  }`}
                >
                  <Icon className="w-4 h-4" aria-hidden="true" />
                  <span>{label}</span>
                </button>
              );
            })}
          </div>

          <div className="pt-2 border-t border-white/5">
            <label id="density-label" className="text-[11px] font-semibold text-zinc-300 uppercase tracking-wider block mb-2">
              Interface Density
            </label>
            <div aria-labelledby="density-label" className="grid grid-cols-2 gap-2">
              {[
                { id: "compact" as DeskDensity, label: "Compact", sub: "Trading Desk (12px)" },
                { id: "comfortable" as DeskDensity, label: "Comfortable", sub: "Standard (14px)" },
              ].map(({ id, label, sub }) => {
                const active = prefs.density === id;
                return (
                  <button
                    key={id}
                    type="button"
                    aria-pressed={active}
                    onClick={() => prefs.setDensity(id)}
                    className={`flex flex-col text-left p-2.5 rounded-lg border transition-all ${
                      active
                        ? "border-cyan-500 bg-cyan-950/40 text-cyan-300 shadow-sm"
                        : "border-white/10 bg-zinc-900/40 text-zinc-400 hover:bg-zinc-900 hover:text-zinc-200"
                    }`}
                  >
                    <span className="text-xs font-medium">{label}</span>
                    <span className="text-[10px] text-zinc-400">{sub}</span>
                  </button>
                );
              })}
            </div>
          </div>
        </div>

        {/* Card 2: Number & P&L Formatting */}
        <div className="rounded-xl border border-white/10 bg-zinc-950/60 p-5 space-y-4">
          <div className="flex items-center justify-between">
            <div>
              <h3 className="text-sm font-semibold text-zinc-100 flex items-center gap-2">
                <DollarSign className="w-4 h-4 text-emerald-400" aria-hidden="true" />
                Number & P&L Formatting
              </h3>
              <p className="text-[11px] text-zinc-400 mt-0.5">
                Currency denominations, decimal precision & metrics
              </p>
            </div>
          </div>

          <div className="space-y-3">
            <div>
              <label id="quote-curr-label" className="text-[11px] font-semibold text-zinc-300 uppercase tracking-wider block mb-1.5">
                Quote Currency
              </label>
              <div role="radiogroup" aria-labelledby="quote-curr-label" className="grid grid-cols-2 gap-2">
                {(["USDT", "USD"] as QuoteCurrency[]).map((curr) => {
                  const active = prefs.quoteCurrency === curr;
                  return (
                    <button
                      key={curr}
                      type="button"
                      role="radio"
                      aria-checked={active}
                      onClick={() => prefs.setQuoteCurrency(curr)}
                      className={`py-2 px-3 rounded-lg border text-xs font-mono font-medium transition-all ${
                        active
                          ? "border-cyan-500 bg-cyan-950/40 text-cyan-300"
                          : "border-white/10 bg-zinc-900/40 text-zinc-400 hover:bg-zinc-900 hover:text-zinc-200"
                      }`}
                    >
                      {curr}
                    </button>
                  );
                })}
              </div>
            </div>

            <div>
              <label id="pnl-format-label" className="text-[11px] font-semibold text-zinc-300 uppercase tracking-wider block mb-1.5">
                Primary P&L Metric Display
              </label>
              <div role="radiogroup" aria-labelledby="pnl-format-label" className="grid grid-cols-3 gap-2">
                {[
                  { id: "pct" as PnlFormat, label: "Percentage (%)" },
                  { id: "usd" as PnlFormat, label: "Dollar ($)" },
                  { id: "bps" as PnlFormat, label: "Basis Points (bps)" },
                ].map(({ id, label }) => {
                  const active = prefs.pnlFormat === id;
                  return (
                    <button
                      key={id}
                      type="button"
                      role="radio"
                      aria-checked={active}
                      onClick={() => prefs.setPnlFormat(id)}
                      className={`py-2 px-2 text-center rounded-lg border text-[11px] font-medium transition-all ${
                        active
                          ? "border-cyan-500 bg-cyan-950/40 text-cyan-300"
                          : "border-white/10 bg-zinc-900/40 text-zinc-400 hover:bg-zinc-900 hover:text-zinc-200"
                      }`}
                    >
                      {label}
                    </button>
                  );
                })}
              </div>
            </div>

            <div>
              <label id="decimal-prec-label" className="text-[11px] font-semibold text-zinc-300 uppercase tracking-wider block mb-1.5">
                Decimal Precision
              </label>
              <div role="radiogroup" aria-labelledby="decimal-prec-label" className="grid grid-cols-3 gap-2">
                {[
                  { id: "auto" as DecimalPrecision, label: "Auto (Dynamic)" },
                  { id: "2" as DecimalPrecision, label: "2 Decimals" },
                  { id: "4" as DecimalPrecision, label: "4 Decimals" },
                ].map(({ id, label }) => {
                  const active = prefs.decimalPrecision === id;
                  return (
                    <button
                      key={id}
                      type="button"
                      role="radio"
                      aria-checked={active}
                      onClick={() => prefs.setDecimalPrecision(id)}
                      className={`py-2 px-2 text-center rounded-lg border text-[11px] font-medium transition-all ${
                        active
                          ? "border-cyan-500 bg-cyan-950/40 text-cyan-300"
                          : "border-white/10 bg-zinc-900/40 text-zinc-400 hover:bg-zinc-900 hover:text-zinc-200"
                      }`}
                    >
                      {label}
                    </button>
                  );
                })}
              </div>
            </div>
          </div>
        </div>

        {/* Card 3: Audio Alerts & Synthesizer Chimes */}
        <div className="rounded-xl border border-white/10 bg-zinc-950/60 p-5 space-y-4 md:col-span-2">
          <div className="flex flex-wrap items-center justify-between gap-4 border-b border-white/5 pb-3">
            <div>
              <h3 className="text-sm font-semibold text-zinc-100 flex items-center gap-2">
                {prefs.audioAlerts ? (
                  <Volume2 className="w-4 h-4 text-cyan-400" aria-hidden="true" />
                ) : (
                  <VolumeX className="w-4 h-4 text-zinc-400" aria-hidden="true" />
                )}
                Trading Desk Audio & Synthesized Chimes
              </h3>
              <p className="text-[11px] text-zinc-400 mt-0.5">
                Zero-asset WebAudio synthesized alerts with discrete event profiles
              </p>
            </div>
            <div className="flex items-center gap-3">
              <label className="flex items-center gap-2 cursor-pointer select-none">
                <input
                  type="checkbox"
                  checked={prefs.audioAlerts}
                  onChange={(e) => prefs.setAudioAlerts(e.target.checked)}
                  className="rounded border-zinc-700 bg-zinc-900 text-cyan-500 focus:ring-cyan-500/50 w-4 h-4"
                />
                <span className="text-xs font-medium text-zinc-200">
                  {prefs.audioAlerts ? "Audio Enabled" : "Audio Muted"}
                </span>
              </label>
            </div>
          </div>

          {/* Volume Slider */}
          <div className={`space-y-1.5 ${!prefs.audioAlerts ? "opacity-40 pointer-events-none" : ""}`}>
            <div className="flex items-center justify-between text-xs">
              <span className="text-zinc-400 font-medium">Master Alert Volume</span>
              <span className="font-mono text-cyan-300 font-semibold">{prefs.audioVolume}%</span>
            </div>
            <input
              type="range"
              aria-label="Master Alert Volume"
              min="0"
              max="100"
              step="1"
              value={prefs.audioVolume}
              onChange={(e) => prefs.setAudioVolume(parseInt(e.target.value, 10))}
              className="w-full accent-cyan-500 bg-zinc-800 h-1.5 rounded-lg cursor-pointer"
            />
          </div>

          {/* Individual Event Chimes */}
          <div className={`grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3 pt-2 ${!prefs.audioAlerts ? "opacity-40 pointer-events-none" : ""}`}>
            {[
              {
                id: "tradeFill" as const,
                label: "Trade Fills",
                sub: "Ascending two-tone chime",
                color: "text-emerald-400 border-emerald-500/30",
              },
              {
                id: "debateVerdict" as const,
                label: "Debate Verdict",
                sub: "Harmonic triad chord",
                color: "text-cyan-400 border-cyan-500/30",
              },
              {
                id: "riskBreach" as const,
                label: "Risk Breach",
                sub: "Urgent square warning ping",
                color: "text-amber-400 border-amber-500/30",
              },
              {
                id: "killSwitch" as const,
                label: "Kill Switch",
                sub: "Deep industrial pulse",
                color: "text-rose-400 border-rose-500/30",
              },
            ].map(({ id, label, sub }) => {
              const enabled = prefs.chimeEvents[id];
              return (
                <div
                  key={id}
                  className="rounded-lg border border-white/10 bg-zinc-900/40 p-3 flex flex-col justify-between gap-3"
                >
                  <div>
                    <div className="flex items-center justify-between">
                      <span className="text-xs font-semibold text-zinc-200">{label}</span>
                      <input
                        type="checkbox"
                        aria-label={`Enable ${label} sound chime`}
                        checked={enabled}
                        onChange={(e) => prefs.setChimeEvent(id, e.target.checked)}
                        className="rounded border-zinc-700 bg-zinc-900 text-cyan-500 focus:ring-cyan-500/50 w-3.5 h-3.5"
                      />
                    </div>
                    <p className="text-[10px] text-zinc-400 mt-1">{sub}</p>
                  </div>

                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    disabled={!prefs.audioAlerts || !enabled}
                    onClick={() => handleTestChime(id, label)}
                    className="w-full text-[11px] h-7 border-white/10 bg-zinc-950/60 hover:bg-zinc-800 text-zinc-300 gap-1.5"
                  >
                    <Play className="w-2.5 h-2.5 text-cyan-400 fill-cyan-400" aria-hidden="true" />
                    Test Chime
                  </Button>
                </div>
              );
            })}
          </div>
        </div>

        {/* Card 4: Workstation Defaults & Timezone */}
        <div className="rounded-xl border border-white/10 bg-zinc-950/60 p-5 space-y-4 md:col-span-2">
          <div className="flex items-center justify-between">
            <div>
              <h3 className="text-sm font-semibold text-zinc-100 flex items-center gap-2">
                <LayoutGrid className="w-4 h-4 text-purple-400" aria-hidden="true" />
                Workstation Defaults & Navigation
              </h3>
              <p className="text-[11px] text-zinc-400 mt-0.5">
                Initial landing workspace and time presentation
              </p>
            </div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <label id="landing-workspace-label" className="text-[11px] font-semibold text-zinc-300 uppercase tracking-wider block mb-2">
                Default Landing Workspace
              </label>
              <div role="radiogroup" aria-labelledby="landing-workspace-label" className="grid grid-cols-2 sm:grid-cols-3 gap-2">
                {WORKSPACE_OPTIONS.map(({ id, label }) => {
                  const active = prefs.defaultWorkspace === id;
                  return (
                    <button
                      key={id}
                      type="button"
                      role="radio"
                      aria-checked={active}
                      onClick={() => prefs.setDefaultWorkspace(id)}
                      className={`text-left p-2 rounded-lg border text-xs font-medium transition-all ${
                        active
                          ? "border-cyan-500 bg-cyan-950/40 text-cyan-300 shadow-sm"
                          : "border-white/10 bg-zinc-900/40 text-zinc-400 hover:bg-zinc-900 hover:text-zinc-200"
                      }`}
                    >
                      {label}
                    </button>
                  );
                })}
              </div>
            </div>

            <div>
              <label id="timezone-label" className="text-[11px] font-semibold text-zinc-300 uppercase tracking-wider block mb-2">
                Timestamp Display Timezone
              </label>
              <div role="radiogroup" aria-labelledby="timezone-label" className="grid grid-cols-2 gap-2">
                {[
                  { id: "UTC" as DeskTimezone, label: "UTC (Institutional Standard)", sub: "Consistent across all execution logs" },
                  { id: "local" as DeskTimezone, label: "Local Time (Browser)", sub: "Matches your system clock" },
                ].map(({ id, label, sub }) => {
                  const active = prefs.timezone === id;
                  return (
                    <button
                      key={id}
                      type="button"
                      role="radio"
                      aria-checked={active}
                      onClick={() => prefs.setTimezone(id)}
                      className={`flex flex-col text-left p-2.5 rounded-lg border transition-all ${
                        active
                          ? "border-cyan-500 bg-cyan-950/40 text-cyan-300 shadow-sm"
                          : "border-white/10 bg-zinc-900/40 text-zinc-400 hover:bg-zinc-900 hover:text-zinc-200"
                      }`}
                    >
                      <span className="text-xs font-medium">{label}</span>
                      <span className="text-[10px] text-zinc-400 mt-0.5">{sub}</span>
                    </button>
                  );
                })}
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};
