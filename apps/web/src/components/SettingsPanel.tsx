"use client";

import React, { useEffect, useState, useCallback } from "react";
import {
  Sliders,
  Cpu,
  ShieldAlert,
  Scale,
  Search,
  Bell,
  Fingerprint,
  Activity,
  RotateCcw,
  Download,
  KeyRound,
  X,
} from "lucide-react";
import { toast } from "sonner";
import { fetchApi } from "@/lib/api";
import { Button } from "./ui/button";
import { OperatorLoginModal } from "@/components/operator/OperatorLoginModal";
import { useSessionStore } from "@/lib/stores/useSessionStore";
import { useSlowFlag } from "@/lib/hooks/useSlowFlag";
import { useDeskPreferences } from "@/lib/stores/useDeskPreferences";
import { SETTINGS_SECTIONS, getLastDispatchedSettingsSection, type SettingsSection } from "@/lib/navigation";

// Section Views
import { PreferencesSection } from "./settings/sections/PreferencesSection";
import { AIGatewaySection } from "./settings/sections/AIGatewaySection";
import { ExitRulesSection } from "./settings/sections/ExitRulesSection";
import { RiskGuardrailsSection } from "./settings/sections/RiskGuardrailsSection";
import { ScannerUniverseSection } from "./settings/sections/ScannerUniverseSection";
import { AlertsRoutingSection } from "./settings/sections/AlertsRoutingSection";
import { SecurityPostureSection } from "./settings/sections/SecurityPostureSection";
import { SystemAuditSection } from "./settings/sections/SystemAuditSection";
import { SettingsGlobalSearch } from "./settings/sections/SettingsGlobalSearch";

import type {
  RegistryEntry,
  RouteView,
  AgentConfig,
  EnvHealth,
  AuditEvent,
} from "./settings/types";

const SETTINGS_TIMEOUT_MS = 10_000;

const SECTION_ICONS: Record<SettingsSection, React.ComponentType<{ className?: string }>> = {
  preferences: Sliders,
  ai: Cpu,
  exit: ShieldAlert,
  risk: Scale,
  scanner: Search,
  alerts: Bell,
  security: Fingerprint,
  system: Activity,
};

export const SettingsPanel: React.FC = () => {
  const [activeSection, setActiveSection] = useState<SettingsSection>(() => {
    return getLastDispatchedSettingsSection() || "preferences";
  });
  const [searchQuery, setSearchQuery] = useState<string>("");
  const [showLoginModal, setShowLoginModal] = useState<boolean>(false);
  const [highlightedKey, setHighlightedKey] = useState<string | undefined>(undefined);

  // Server-side loaded data
  const [entries, setEntries] = useState<RegistryEntry[]>([]);
  const [routes, setRoutes] = useState<RouteView[]>([]);
  const [agents, setAgents] = useState<Record<string, AgentConfig>>({});
  const [envHealth, setEnvHealth] = useState<EnvHealth | null>(null);
  const [auditEvents, setAuditEvents] = useState<AuditEvent[]>([]);
  const [shadowPaid, setShadowPaid] = useState<boolean>(false);

  // Loading and error states
  const [loading, setLoading] = useState<boolean>(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loadingAudit, setLoadingAudit] = useState<boolean>(false);

  const session = useSessionStore((s) => s.status);
  const sessionUser = useSessionStore((s) => s.username);
  const agentsSlow = useSlowFlag(loading && Object.keys(agents).length === 0);

  // Listen for navigation events from CommandPalette or custom dispatchers
  useEffect(() => {
    const handleSettingsSection = (e: Event) => {
      const custom = e as CustomEvent<{ section?: SettingsSection }>;
      if (custom.detail?.section) {
        setActiveSection(custom.detail.section);
        setSearchQuery("");
      }
    };
    window.addEventListener("cryptonite:settings-section", handleSettingsSection);
    return () => window.removeEventListener("cryptonite:settings-section", handleSettingsSection);
  }, []);

  // Data reload helpers
  const loadRegistry = useCallback(async () => {
    try {
      const res = await fetchApi("/settings/registry", { timeoutMs: SETTINGS_TIMEOUT_MS });
      if (res?.settings) setEntries(res.settings);
    } catch (err: any) {
      if (err?.status === 401 || err?.status === 403) {
        void useSessionStore.getState().check();
      }
    }
  }, []);

  const loadRoutes = useCallback(async () => {
    try {
      const res = await fetchApi("/settings/routes", { timeoutMs: SETTINGS_TIMEOUT_MS });
      if (res?.routes) setRoutes(res.routes);
    } catch (err: any) {
      if (err?.status === 401 || err?.status === 403) {
        void useSessionStore.getState().check();
      }
    }
  }, []);

  const loadAgents = useCallback(async () => {
    try {
      const res = await fetchApi("/settings/agents", { timeoutMs: SETTINGS_TIMEOUT_MS });
      if (res) setAgents(res);
    } catch (err: any) {
      if (err?.status === 401 || err?.status === 403) {
        void useSessionStore.getState().check();
      }
    }
  }, []);

  const loadEnvironment = useCallback(async () => {
    try {
      const res = await fetchApi("/settings/environment", { timeoutMs: SETTINGS_TIMEOUT_MS });
      if (res) setEnvHealth(res);
    } catch (err: any) {
      if (err?.status === 401 || err?.status === 403) {
        void useSessionStore.getState().check();
      }
    }
  }, []);

  const loadShadowPaid = useCallback(async () => {
    try {
      const res = await fetchApi("/settings/shadow-paid", { timeoutMs: SETTINGS_TIMEOUT_MS });
      if (res) setShadowPaid(Boolean(res.allow_paid));
    } catch {
      // Non-critical
    }
  }, []);

  const loadAudit = useCallback(async () => {
    setLoadingAudit(true);
    try {
      const res = await fetchApi("/settings/audit", { timeoutMs: SETTINGS_TIMEOUT_MS });
      if (res?.events) setAuditEvents(res.events);
    } catch {
      // Non-critical
    } finally {
      setLoadingAudit(false);
    }
  }, []);

  const loadAllServerData = useCallback(async () => {
    if (session === "anonymous") return;
    setLoading(true);
    setLoadError(null);
    try {
      await Promise.allSettled([
        loadRegistry(),
        loadRoutes(),
        loadAgents(),
        loadEnvironment(),
        loadShadowPaid(),
        loadAudit(),
      ]);
    } catch (err: any) {
      if (err?.status === 401 || err?.status === 403) {
        void useSessionStore.getState().check();
        setLoadError("The settings were refused. Your operator session may have ended.");
      } else {
        setLoadError(err?.message || "Failed to load platform settings.");
      }
    } finally {
      setLoading(false);
    }
  }, [session, loadRegistry, loadRoutes, loadAgents, loadEnvironment, loadShadowPaid, loadAudit]);

  // Load when session is authenticated or refreshed
  useEffect(() => {
    if (session !== "anonymous") {
      void loadAllServerData();
    }
  }, [session, loadAllServerData]);

  // Export JSON snapshot
  const handleExportJson = () => {
    const snapshot = {
      exported_at: new Date().toISOString(),
      operator_session: {
        status: session,
        username: sessionUser,
      },
      desk_preferences: useDeskPreferences.getState(),
      registry_entries: entries,
      model_routes: routes,
      agent_configs: agents,
      environment_health: envHealth,
    };

    const blob = new Blob([JSON.stringify(snapshot, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `cryptonite-settings-${new Date().toISOString().slice(0, 10)}.json`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
    toast.success("Settings snapshot exported to JSON file");
  };

  // Quick navigation from search results
  const handleNavigateFromSearch = (section: SettingsSection, key?: string) => {
    setActiveSection(section);
    setSearchQuery("");
    setHighlightedKey(key);
  };

  const isOperatorGated = activeSection !== "preferences" && session === "anonymous";

  return (
    <div className="flex flex-col h-full bg-zinc-950 text-white rounded-xl border border-white/10 overflow-hidden shadow-2xl">
      {/* Top Command Header */}
      <header className="border-b border-white/10 bg-zinc-900/60 p-4 sm:p-5 backdrop-blur-md">
        <div className="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-4">
          {/* Title & Subtitle */}
          <div>
            <div className="flex items-center gap-2.5">
              <span className="p-1.5 rounded-lg bg-cyan-950/60 border border-cyan-500/40 text-cyan-400">
                <Sliders className="w-5 h-5" aria-hidden="true" />
              </span>
              <h1 className="text-xl font-bold tracking-tight text-white font-mono uppercase">
                System Settings
              </h1>
            </div>
            <p className="text-xs text-zinc-400 mt-1">
              Platform governance, AI routing, risk thresholds & operator desk preferences
            </p>
          </div>

          {/* Search Input & Action Buttons */}
          <div className="flex flex-wrap items-center gap-2.5">
            {/* Global Search Input */}
            <div className="relative min-w-[240px] sm:min-w-[320px]">
              <Search className="w-3.5 h-3.5 text-zinc-400 absolute left-3 top-1/2 -translate-y-1/2 pointer-events-none" aria-hidden="true" />
              <input
                type="text"
                aria-label="Filter settings"
                placeholder="Filter settings by name, key (e.g. exit.stop_loss_pct), or ADR..."
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                className="w-full bg-zinc-950/80 border border-white/10 rounded-lg pl-8 pr-8 py-1.5 text-xs text-zinc-100 placeholder:text-zinc-500 focus:border-cyan-500 focus:ring-1 focus:ring-cyan-500/40 focus:outline-none transition-all font-mono"
              />
              {searchQuery && (
                <button
                  type="button"
                  onClick={() => setSearchQuery("")}
                  className="absolute right-2.5 top-1/2 -translate-y-1/2 p-1 min-w-[24px] min-h-[24px] flex items-center justify-center text-zinc-500 hover:text-zinc-300"
                  aria-label="Clear search"
                >
                  <X className="w-3.5 h-3.5" aria-hidden="true" />
                </button>
              )}
            </div>

            {/* Session Status Indicator */}
            <div
              className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg border text-xs font-medium font-mono ${
                session === "authenticated"
                  ? "border-emerald-500/40 bg-emerald-950/40 text-emerald-300"
                  : session === "anonymous"
                  ? "border-amber-500/40 bg-amber-950/30 text-amber-300"
                  : "border-zinc-700 bg-zinc-900 text-zinc-400"
              }`}
            >
              <span
                aria-hidden="true"
                className={`w-2 h-2 rounded-full ${
                  session === "authenticated"
                    ? "bg-emerald-400 animate-pulse"
                    : session === "anonymous"
                    ? "bg-amber-400"
                    : "bg-zinc-500"
                }`}
              />
              <span>
                {session === "authenticated"
                  ? `Operator: ${sessionUser || "Active"}`
                  : session === "anonymous"
                  ? "Anonymous (Desk Only)"
                  : "Verifying Session..."}
              </span>
            </div>

            {/* Authenticate Button (if anonymous) */}
            {session === "anonymous" && (
              <Button
                type="button"
                size="sm"
                onClick={() => setShowLoginModal(true)}
                className="bg-cyan-600 hover:bg-cyan-500 text-zinc-950 font-bold text-xs uppercase tracking-wider gap-1.5"
              >
                <KeyRound className="w-3.5 h-3.5" aria-hidden="true" />
                Authenticate as Operator
              </Button>
            )}

            {/* Refresh Button */}
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={loading || session === "anonymous"}
              onClick={() => void loadAllServerData()}
              className="border-white/10 bg-zinc-900/60 hover:bg-zinc-800 text-zinc-300 text-xs gap-1.5"
              title="Refresh all platform settings"
            >
              <RotateCcw className={`w-3.5 h-3.5 text-zinc-400 ${loading ? "animate-spin" : ""}`} aria-hidden="true" />
              <span className="hidden sm:inline">Refresh</span>
            </Button>

            {/* Export JSON Button */}
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={handleExportJson}
              className="border-white/10 bg-zinc-900/60 hover:bg-zinc-800 text-zinc-300 text-xs gap-1.5"
              title="Export complete settings configuration snapshot as JSON"
            >
              <Download className="w-3.5 h-3.5 text-zinc-400" aria-hidden="true" />
              <span className="hidden sm:inline">Export JSON</span>
            </Button>
          </div>
        </div>

        {/* Mobile Horizontal Section Navigation (< md) */}
        <div
          role="tablist"
          aria-label="Settings categories"
          className="flex md:hidden items-center gap-1.5 overflow-x-auto pt-3 mt-3 border-t border-white/5 scrollbar-none"
        >
          {SETTINGS_SECTIONS.map((sec) => {
            const Icon = SECTION_ICONS[sec.id];
            const active = activeSection === sec.id && !searchQuery.trim();
            return (
              <button
                key={sec.id}
                type="button"
                role="tab"
                aria-selected={active}
                onClick={() => {
                  setActiveSection(sec.id);
                  setSearchQuery("");
                }}
                className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs whitespace-nowrap font-medium transition-all ${
                  active
                    ? "bg-cyan-900/50 text-cyan-300 border border-cyan-500/40"
                    : "text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800/40 border border-transparent"
                }`}
              >
                <Icon className="w-3.5 h-3.5" aria-hidden="true" />
                <span>{sec.shortLabel}</span>
              </button>
            );
          })}
        </div>
      </header>

      {/* Main Body: Desktop Sidebar + Content Area */}
      <div className="flex flex-1 min-h-0 overflow-hidden">
        {/* Desktop Sidebar (md+) */}
        <aside
          aria-label="Navigation Categories"
          className="hidden md:flex flex-col w-64 lg:w-72 border-r border-white/10 bg-zinc-900/20 overflow-y-auto p-3 space-y-1"
        >
          <div className="px-3 py-2 text-[10px] font-mono font-semibold uppercase tracking-wider text-zinc-400" aria-hidden="true">
            Navigation Categories
          </div>

          {SETTINGS_SECTIONS.map((sec) => {
            const Icon = SECTION_ICONS[sec.id];
            const active = activeSection === sec.id && !searchQuery.trim();
            const isGated = sec.id !== "preferences" && session === "anonymous";

            return (
              <button
                key={sec.id}
                type="button"
                aria-current={active ? "page" : undefined}
                onClick={() => {
                  setActiveSection(sec.id);
                  setSearchQuery("");
                }}
                className={`w-full flex items-start gap-3 p-3 rounded-xl text-left transition-all ${
                  active
                    ? "bg-cyan-950/40 text-cyan-300 border border-cyan-500/40 shadow-sm shadow-cyan-950/30"
                    : "text-zinc-400 hover:text-zinc-200 hover:bg-zinc-900/50 border border-transparent"
                }`}
              >
                <Icon className={`w-4 h-4 mt-0.5 shrink-0 ${active ? "text-cyan-400" : "text-zinc-400"}`} aria-hidden="true" />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center justify-between gap-1">
                    <span className="text-xs font-semibold text-zinc-200 truncate">
                      {sec.label}
                    </span>
                    {isGated && (
                      <span className="text-[9px] font-mono text-amber-400/80 uppercase">
                        2FA
                      </span>
                    )}
                  </div>
                  <p className="text-[10px] text-zinc-400 line-clamp-1 mt-0.5">
                    {sec.description}
                  </p>
                </div>
              </button>
            );
          })}
        </aside>

        {/* Content Pane */}
        <main className="flex-1 min-w-0 overflow-y-auto p-4 sm:p-6 lg:p-8">
          {/* Global Search Results View */}
          {searchQuery.trim() ? (
            <SettingsGlobalSearch
              query={searchQuery}
              entries={entries}
              routes={routes}
              agents={agents}
              onClear={() => setSearchQuery("")}
              onNavigateSection={handleNavigateFromSearch}
              onReloadRegistry={loadRegistry}
            />
          ) : isOperatorGated ? (
            /* Operator Gate Card for Protected Sections when Anonymous */
            <div className="flex flex-col items-center justify-center min-h-[420px] rounded-2xl border border-white/10 bg-zinc-950/60 p-8 text-center max-w-md mx-auto my-12 shadow-xl backdrop-blur-sm">
              <div className="p-4 bg-cyan-950/40 border border-cyan-500/40 rounded-full mb-4">
                <ShieldAlert className="w-10 h-10 text-cyan-400" aria-hidden="true" />
              </div>
              <h2 className="text-lg font-bold uppercase tracking-wider font-mono text-zinc-100">
                Operator Access Required
              </h2>
              <p className="text-xs text-zinc-400 mt-2 leading-relaxed">
                Viewing system risk limits, exit thresholds, AI routes, and platform health requires an active operator session.
              </p>
              <p className="text-[11px] text-zinc-500 mt-2">
                Desk preferences remain accessible without signing in.
              </p>
              <Button
                type="button"
                onClick={() => setShowLoginModal(true)}
                className="w-full bg-cyan-600 hover:bg-cyan-500 text-zinc-950 font-bold uppercase tracking-wider text-xs py-2.5 mt-6"
              >
                Authenticate as Operator
              </Button>
            </div>
          ) : (
            <>
              {/* Anonymous Banner when viewing preferences */}
              {session === "anonymous" && activeSection === "preferences" && (
                <div className="mb-6 flex flex-wrap items-center justify-between gap-3 p-3.5 rounded-xl border border-cyan-500/30 bg-cyan-950/20 text-xs">
                  <div className="flex items-center gap-2.5 text-cyan-300">
                    <ShieldAlert className="w-4 h-4 shrink-0 text-cyan-400" aria-hidden="true" />
                    <span>
                      Browsing as Anonymous. Desk preferences are active and saved in your local browser profile. Authenticate to unlock server-side platform settings.
                    </span>
                  </div>
                  <Button
                    type="button"
                    size="sm"
                    onClick={() => setShowLoginModal(true)}
                    className="bg-cyan-600 hover:bg-cyan-500 text-zinc-950 text-xs font-bold shrink-0"
                  >
                    Authenticate as Operator
                  </Button>
                </div>
              )}

              {/* 1. preferences: Desk Preferences */}
              {activeSection === "preferences" && <PreferencesSection />}

              {/* 2. ai: AI Gateway & Routing */}
              {activeSection === "ai" && (
                <AIGatewaySection
                  entries={entries}
                  routes={routes}
                  agents={agents}
                  loadingAgents={loading}
                  agentsSlow={agentsSlow}
                  agentsLoadError={loadError}
                  shadowAllowPaid={shadowPaid}
                  onReloadRegistry={loadRegistry}
                  onReloadRoutes={loadRoutes}
                  onReloadAgents={loadAgents}
                  onReloadShadowPaid={loadShadowPaid}
                  highlightedKey={highlightedKey}
                />
              )}

              {/* 3. exit: Exit Rules & Stop Loss */}
              {activeSection === "exit" && (
                <ExitRulesSection
                  entries={entries}
                  onReload={loadRegistry}
                  highlightedKey={highlightedKey}
                  onSelectKey={(k) => setHighlightedKey(k)}
                />
              )}

              {/* 4. risk: Risk & Guardrails */}
              {activeSection === "risk" && (
                <RiskGuardrailsSection
                  entries={entries}
                  onReload={loadRegistry}
                  highlightedKey={highlightedKey}
                />
              )}

              {/* 5. scanner: Scanner & Universe */}
              {activeSection === "scanner" && (
                <ScannerUniverseSection
                  entries={entries}
                  onReload={loadRegistry}
                  highlightedKey={highlightedKey}
                />
              )}

              {/* 6. alerts: Alerts & Integrations */}
              {activeSection === "alerts" && (
                <AlertsRoutingSection
                  entries={entries}
                  onReload={loadRegistry}
                  highlightedKey={highlightedKey}
                />
              )}

              {/* 7. security: Security & Session Policy */}
              {activeSection === "security" && (
                <SecurityPostureSection
                  entries={entries}
                  envHealth={envHealth}
                  onReload={loadRegistry}
                  highlightedKey={highlightedKey}
                />
              )}

              {/* 8. system: System Health & Audit Log */}
              {activeSection === "system" && (
                <SystemAuditSection
                  envHealth={envHealth}
                  auditEvents={auditEvents}
                  loadingAudit={loadingAudit}
                  onReloadAudit={loadAudit}
                  onReloadHealth={loadEnvironment}
                />
              )}
            </>
          )}
        </main>
      </div>

      {/* Operator Login Modal */}
      {showLoginModal && (
        <OperatorLoginModal
          onSuccess={() => {
            setShowLoginModal(false);
            void loadAllServerData();
          }}
          onClose={() => setShowLoginModal(false)}
        />
      )}
    </div>
  );
};
