"use client";

import React from "react";
import {
  Search,
  X,
  Sliders,
  Cpu,
  ShieldAlert,
  ArrowRight,
  Sparkles,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { SettingCard, type RegistryEntry } from "../controls/SettingCard";
import type { RouteView, AgentConfig, SettingsSection } from "../types";

export interface SettingsGlobalSearchProps {
  query: string;
  entries: RegistryEntry[];
  routes: RouteView[];
  agents: Record<string, AgentConfig>;
  onClear: () => void;
  onNavigateSection: (section: SettingsSection, highlightKey?: string) => void;
  onReloadRegistry: () => Promise<void>;
}

export const SettingsGlobalSearch: React.FC<SettingsGlobalSearchProps> = ({
  query,
  entries,
  routes,
  agents,
  onClear,
  onNavigateSection,
  onReloadRegistry,
}) => {
  const q = query.trim().toLowerCase();

  // 1. Matching Registry Entries
  const matchingEntries = entries.filter((e) => {
    return (
      e.key.toLowerCase().includes(q) ||
      e.label.toLowerCase().includes(q) ||
      e.note.toLowerCase().includes(q) ||
      e.adr.toLowerCase().includes(q) ||
      e.tier.toLowerCase().includes(q)
    );
  });

  // 2. Matching Routes
  const matchingRoutes = routes.filter((r) => {
    return (
      r.role.toLowerCase().includes(q) ||
      (r.effective.primary_model || "").toLowerCase().includes(q) ||
      (r.effective.fallback_model || "").toLowerCase().includes(q)
    );
  });

  // 3. Matching Agents
  const matchingAgents = Object.entries(agents).filter(([name, cfg]) => {
    return (
      name.toLowerCase().includes(q) ||
      (cfg.model_name || "").toLowerCase().includes(q) ||
      (cfg.system_instructions || "").toLowerCase().includes(q)
    );
  });

  // 4. Desk Preferences matches
  const preferenceTerms = [
    { label: "Theme Mode", keywords: ["theme", "dark", "light", "system", "contrast", "appearance"] },
    { label: "Interface Density", keywords: ["density", "compact", "comfortable", "spacing", "desk"] },
    { label: "Audio Alerts & Synthesizer Chimes", keywords: ["audio", "volume", "sound", "chime", "alert", "fill", "verdict"] },
    { label: "Quote Currency & P&L Formatting", keywords: ["currency", "usdt", "usd", "pnl", "bps", "percentage", "dollar"] },
    { label: "Decimal Precision", keywords: ["precision", "decimals", "auto", "rounding"] },
    { label: "Workstation Defaults & Timezone", keywords: ["workspace", "landing", "timezone", "utc", "local", "clock"] },
  ];

  const matchingPrefs = preferenceTerms.filter((p) =>
    p.label.toLowerCase().includes(q) || p.keywords.some((k) => k.includes(q))
  );

  const totalMatches =
    matchingEntries.length +
    matchingRoutes.length +
    matchingAgents.length +
    matchingPrefs.length;

  return (
    <div className="space-y-6 max-w-5xl">
      {/* Search Header Banner */}
      <div className="flex flex-wrap items-center justify-between gap-4 border-b border-white/10 pb-4">
        <div>
          <h2 className="text-xl font-bold tracking-tight text-white flex items-center gap-2">
            <Search className="w-5 h-5 text-cyan-400" aria-hidden="true" />
            Filtered Settings: <span className="text-cyan-300 font-mono text-base">"{query}"</span>
          </h2>
          <p className="text-xs text-zinc-400 mt-1" role="status" aria-live="polite">
            Found {totalMatches} matching configuration item{totalMatches === 1 ? "" : "s"} across all platform categories.
          </p>
        </div>
        <Button
          type="button"
          onClick={onClear}
          variant="outline"
          className="border-white/10 bg-zinc-900/60 hover:bg-zinc-800 text-zinc-300 text-xs gap-1.5"
        >
          <X className="w-3.5 h-3.5" aria-hidden="true" />
          Clear Filter
        </Button>
      </div>

      {totalMatches === 0 && (
        <div className="rounded-xl border border-white/10 bg-zinc-950/40 p-12 text-center space-y-3" role="status" aria-live="polite">
          <p className="text-zinc-400 text-sm">
            No settings match <span className="text-zinc-200 font-mono">"{query}"</span>.
          </p>
          <Button
            type="button"
            onClick={onClear}
            className="bg-cyan-600 hover:bg-cyan-500 text-zinc-950 font-bold text-xs"
          >
            Clear Search Filter
          </Button>
        </div>
      )}

      {/* Desk Preferences Matches */}
      {matchingPrefs.length > 0 && (
        <section className="space-y-3">
          <h3 className="text-xs font-bold uppercase tracking-wider text-cyan-400 flex items-center gap-2">
            <Sliders className="w-3.5 h-3.5" aria-hidden="true" />
            Matching Desk Preferences ({matchingPrefs.length})
          </h3>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            {matchingPrefs.map((p) => (
              <div
                key={p.label}
                className="rounded-lg border border-white/10 bg-zinc-950/60 p-3.5 flex items-center justify-between gap-3"
              >
                <div>
                  <span className="text-xs font-semibold text-zinc-200 block">{p.label}</span>
                  <span className="text-[10px] text-zinc-400">
                    Matches: {p.keywords.filter((k) => k.includes(q)).join(", ") || "title"}
                  </span>
                </div>
                <Button
                  type="button"
                  size="sm"
                  onClick={() => onNavigateSection("preferences")}
                  className="bg-zinc-800 hover:bg-zinc-700 text-zinc-200 text-[11px] gap-1"
                >
                  Configure
                  <ArrowRight className="w-3 h-3" aria-hidden="true" />
                </Button>
              </div>
            ))}
          </div>
        </section>
      )}

      {/* Registry Entries Matches */}
      {matchingEntries.length > 0 && (
        <section className="space-y-3">
          <h3 className="text-xs font-bold uppercase tracking-wider text-cyan-400 flex items-center gap-2">
            <ShieldAlert className="w-3.5 h-3.5" aria-hidden="true" />
            Matching Registry Settings ({matchingEntries.length})
          </h3>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {matchingEntries.map((entry) => (
              <SettingCard
                key={entry.key}
                entry={entry}
                allEntries={entries}
                onSaved={() => void onReloadRegistry()}
                highlight={true}
              />
            ))}
          </div>
        </section>
      )}

      {/* AI Routes Matches */}
      {matchingRoutes.length > 0 && (
        <section className="space-y-3">
          <h3 className="text-xs font-bold uppercase tracking-wider text-purple-400 flex items-center gap-2">
            <Cpu className="w-3.5 h-3.5" aria-hidden="true" />
            Matching AI Model Routes ({matchingRoutes.length})
          </h3>
          <div className="space-y-2">
            {matchingRoutes.map((r) => (
              <div
                key={r.role}
                className="rounded-lg border border-white/10 bg-zinc-950/60 p-3.5 flex flex-wrap items-center justify-between gap-3"
              >
                <div>
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-xs font-bold text-zinc-100">{r.role}</span>
                    {r.protected && (
                      <span className="text-[9px] px-1.5 py-0.5 rounded border border-rose-900/60 bg-rose-950/40 text-rose-300">
                        PROTECTED
                      </span>
                    )}
                  </div>
                  <div className="text-[11px] text-zinc-400 font-mono mt-0.5">
                    {r.effective.primary_model}
                    {r.effective.fallback_model ? ` → ${r.effective.fallback_model}` : ""}
                  </div>
                </div>
                <Button
                  type="button"
                  size="sm"
                  onClick={() => onNavigateSection("ai")}
                  className="bg-zinc-800 hover:bg-zinc-700 text-zinc-200 text-[11px] gap-1"
                >
                  View in AI Gateway
                  <ArrowRight className="w-3 h-3" aria-hidden="true" />
                </Button>
              </div>
            ))}
          </div>
        </section>
      )}

      {/* AI Agents Matches */}
      {matchingAgents.length > 0 && (
        <section className="space-y-3">
          <h3 className="text-xs font-bold uppercase tracking-wider text-emerald-400 flex items-center gap-2">
            <Sparkles className="w-3.5 h-3.5" aria-hidden="true" />
            Matching Agent Personas ({matchingAgents.length})
          </h3>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            {matchingAgents.map(([name, cfg]) => (
              <div
                key={name}
                className="rounded-lg border border-white/10 bg-zinc-950/60 p-3.5 flex items-center justify-between gap-3"
              >
                <div>
                  <span className="text-xs font-bold capitalize text-zinc-100 block">{name}</span>
                  <span className="text-[10px] text-zinc-400 font-mono">
                    Model: {cfg.model_name || "gemini-1.5-pro"} · Temp: {cfg.temperature ?? 0.7}
                  </span>
                </div>
                <Button
                  type="button"
                  size="sm"
                  onClick={() => onNavigateSection("ai")}
                  className="bg-zinc-800 hover:bg-zinc-700 text-zinc-200 text-[11px] gap-1"
                >
                  Edit Persona
                  <ArrowRight className="w-3 h-3" aria-hidden="true" />
                </Button>
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  );
};
