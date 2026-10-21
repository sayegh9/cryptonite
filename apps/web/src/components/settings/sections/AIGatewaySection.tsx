"use client";

import React, { useState } from "react";
import {
  Cpu,
  Save,
  Sparkles,
  Lock,
  DollarSign,
} from "lucide-react";
import { toast } from "sonner";
import { fetchApi } from "@/lib/api";
import { dispatchSwitchWorkspace } from "@/lib/navigation";
import { Button } from "@/components/ui/button";
import type { RegistryEntry } from "../controls/SettingCard";
import { SettingList } from "../controls/SettingList";
import type { RouteView, AgentConfig } from "../types";

const MIN_REASON_CHARS = 5;
const ROUTE_FALLBACK_POLICIES = ["", "FAIL_CLOSED", "DEGRADE"];

const AGENT_DESCRIPTIONS: Record<string, string> = {
  research: "Performs macro analysis, technical momentum screening, and market regime classification.",
  explainer: "Translates complex multi-agent consensus telemetry and mathematical risk rationale into plain text.",
  generator: "Synthesizes quantitative trade candidates, entry trigger levels, and take-profit structures.",
  hunter: "Monitors order flow anomalies, funding rate spikes, and orderbook liquidity vacuums.",
  strategist: "Supervises aggregate portfolio exposure, mandate budget utilization, and hedging strategies.",
};

interface RouteEditorProps {
  route: RouteView;
  onSaved: () => void;
  onClose: () => void;
}

const RouteEditor: React.FC<RouteEditorProps> = ({ route, onSaved, onClose }) => {
  const stored = route.stored;
  const [primary, setPrimary] = useState(stored?.primary_model ?? "");
  const [fallback, setFallback] = useState(stored?.fallback_model ?? "");
  const [policy, setPolicy] = useState(stored?.fallback_policy ?? "");
  const [cap, setCap] = useState(stored?.daily_cap_usd ?? "");
  const [tokens, setTokens] = useState(stored?.max_output_tokens?.toString() ?? "");
  const [thinking, setThinking] = useState(stored?.thinking_budget?.toString() ?? "");
  const [reason, setReason] = useState("");
  const [totp, setTotp] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const blankToNull = (text: string) => (text.trim() === "" ? null : text.trim());
  const canSave = reason.trim().length >= MIN_REASON_CHARS && /^\d{6}$/.test(totp) && !saving;

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      await fetchApi(`/settings/routes/${route.role}`, {
        method: "PUT",
        headers: { "X-CSRF-Token": localStorage.getItem("csrf_token") || "" },
        body: JSON.stringify({
          primary_model: blankToNull(primary),
          fallback_model: blankToNull(fallback),
          fallback_policy: blankToNull(policy),
          daily_cap_usd: blankToNull(cap),
          max_output_tokens: blankToNull(tokens) === null ? null : Number(tokens),
          thinking_budget: blankToNull(thinking) === null ? null : Number(thinking),
          reason: reason.trim(),
          totp_code: totp,
        }),
      });
      toast.success(`${route.role} route updated with TOTP certification`);
      setTotp("");
      setReason("");
      onSaved();
    } catch (err: any) {
      setError(String(err?.message || "The route was not saved.").replace(/^TOTP_REQUIRED:\s*/, ""));
    } finally {
      setSaving(false);
    }
  };

  const hint = (val: string | number | null) => (val === null ? "default" : `default ${val}`);

  return (
    <div className="mt-3 space-y-3 rounded-lg border border-cyan-500/30 bg-zinc-900/80 p-4">
      <div className="flex items-center justify-between border-b border-white/5 pb-2">
        <span className="text-xs font-semibold text-cyan-300">
          Edit Routing Policy for <span className="font-mono">{route.role}</span>
        </span>
        <span className="text-[10px] text-zinc-400">TOTP Required (ADR 0007 §2)</span>
      </div>

      <div className="grid grid-cols-1 @2xl:grid-cols-2 gap-3">
        <label className="text-[11px] text-zinc-400">
          Primary Model ({hint(route.default.primary_model)})
          <input
            value={primary}
            onChange={(e) => setPrimary(e.target.value)}
            placeholder={route.default.primary_model ?? ""}
            className="mt-1 block w-full rounded border border-white/10 bg-zinc-950 px-2 py-1.5 font-mono text-xs text-zinc-100 focus:border-cyan-500 focus:outline-none"
          />
        </label>
        <label className="text-[11px] text-zinc-400">
          Fallback Model ({hint(route.default.fallback_model)})
          <input
            value={fallback}
            onChange={(e) => setFallback(e.target.value)}
            placeholder={route.default.fallback_model ?? ""}
            className="mt-1 block w-full rounded border border-white/10 bg-zinc-950 px-2 py-1.5 font-mono text-xs text-zinc-100 focus:border-cyan-500 focus:outline-none"
          />
        </label>
        <label className="text-[11px] text-zinc-400">
          Fallback Policy ({hint(route.default.fallback_policy)})
          <select
            value={policy}
            onChange={(e) => setPolicy(e.target.value)}
            className="mt-1 block w-full rounded border border-white/10 bg-zinc-950 px-2 py-1.5 font-mono text-xs text-zinc-100 focus:border-cyan-500 focus:outline-none"
          >
            {ROUTE_FALLBACK_POLICIES.map((p) => (
              <option key={p} value={p}>
                {p === "" ? "default" : p}
              </option>
            ))}
          </select>
        </label>
        <label className="text-[11px] text-zinc-400">
          Daily Cap, USD ({hint(route.default.daily_cap_usd)})
          <input
            type="number"
            step="0.0001"
            min="0"
            value={cap}
            onChange={(e) => setCap(e.target.value)}
            className="mt-1 block w-full rounded border border-white/10 bg-zinc-950 px-2 py-1.5 font-mono text-xs text-zinc-100 focus:border-cyan-500 focus:outline-none"
          />
        </label>
        <label className="text-[11px] text-zinc-400">
          Max Output Tokens ({hint(route.default.max_output_tokens)})
          <input
            type="number"
            step="1"
            min="1"
            value={tokens}
            onChange={(e) => setTokens(e.target.value)}
            className="mt-1 block w-full rounded border border-white/10 bg-zinc-950 px-2 py-1.5 font-mono text-xs text-zinc-100 focus:border-cyan-500 focus:outline-none"
          />
        </label>
        <label className="text-[11px] text-zinc-400">
          Thinking Budget ({hint(route.default.thinking_budget)})
          <input
            type="number"
            step="1"
            min="0"
            value={thinking}
            onChange={(e) => setThinking(e.target.value)}
            className="mt-1 block w-full rounded border border-white/10 bg-zinc-950 px-2 py-1.5 font-mono text-xs text-zinc-100 focus:border-cyan-500 focus:outline-none"
          />
        </label>
      </div>

      <label className="block text-[11px] text-zinc-400">
        Reason (audited, at least {MIN_REASON_CHARS} characters)
        <input
          type="text"
          maxLength={300}
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          placeholder="e.g. Elevating thinking budget for volatility analysis"
          className="mt-1 block w-full rounded border border-white/10 bg-zinc-950 px-2 py-1.5 font-mono text-xs text-zinc-100 focus:border-cyan-500 focus:outline-none"
        />
      </label>

      <label className="block text-[11px] text-amber-200">
        Operator TOTP Code (every route alteration requires 2FA authentication)
        <input
          inputMode="numeric"
          autoComplete="one-time-code"
          maxLength={6}
          value={totp}
          onChange={(e) => setTotp(e.target.value.replace(/\D/g, ""))}
          placeholder="123456"
          className="mt-1 block w-32 rounded border border-white/10 bg-zinc-950 px-2 py-1.5 font-mono text-xs text-amber-200 focus:border-amber-400 focus:outline-none"
        />
      </label>

      {error && (
        <p role="alert" className="text-xs text-red-400">
          {error}
        </p>
      )}

      <div className="flex items-center justify-end gap-2 pt-2 border-t border-white/5">
        <Button
          type="button"
          variant="outline"
          onClick={onClose}
          className="text-xs border-white/10 bg-zinc-900 hover:bg-zinc-800 text-zinc-300"
        >
          Cancel
        </Button>
        <Button
          type="button"
          onClick={() => void save()}
          disabled={!canSave}
          className="bg-cyan-600 hover:bg-cyan-500 text-zinc-950 text-xs font-bold uppercase tracking-wider disabled:opacity-40"
        >
          {saving ? "Saving…" : "Save with TOTP"}
        </Button>
      </div>
    </div>
  );
};

export interface AIGatewaySectionProps {
  entries: RegistryEntry[];
  routes: RouteView[];
  agents: Record<string, AgentConfig>;
  loadingAgents: boolean;
  agentsSlow?: boolean;
  agentsLoadError: string | null;
  shadowAllowPaid: boolean;
  onReloadRegistry: () => Promise<void>;
  onReloadRoutes: () => Promise<void>;
  onReloadAgents: () => Promise<void>;
  onReloadShadowPaid: () => Promise<void>;
  highlightedKey?: string;
}

export const AIGatewaySection: React.FC<AIGatewaySectionProps> = ({
  entries,
  routes,
  agents,
  loadingAgents,
  agentsSlow,
  agentsLoadError,
  shadowAllowPaid,
  onReloadRegistry,
  onReloadRoutes,
  onReloadAgents,
  onReloadShadowPaid,
  highlightedKey,
}) => {
  // Shadow mode toggle state
  const [shadowToggle, setShadowToggle] = useState<boolean>(shadowAllowPaid);
  const [shadowReason, setShadowReason] = useState<string>("");
  const [savingShadow, setSavingShadow] = useState<boolean>(false);

  // Sync shadowToggle with prop when prop changes
  React.useEffect(() => {
    setShadowToggle(shadowAllowPaid);
  }, [shadowAllowPaid]);

  // Route filtering & editing state
  const [routeFilter, setRouteFilter] = useState<"all" | "editable" | "locked" | "overridden" | "local">("all");
  const [routeSearch, setRouteSearch] = useState<string>("");
  const [openRouteRole, setOpenRouteRole] = useState<string | null>(null);

  // Agent configuration state
  const [selectedAgent, setSelectedAgent] = useState<string>("research");
  const [editModel, setEditModel] = useState<string>("gemini-1.5-pro");
  const [editTemp, setEditTemp] = useState<number>(0.7);
  const [editPrompt, setEditPrompt] = useState<string>("");
  const [savingAgent, setSavingAgent] = useState<boolean>(false);

  // When selectedAgent or agents change, sync form
  React.useEffect(() => {
    if (selectedAgent && agents[selectedAgent]) {
      const cfg = agents[selectedAgent];
      setEditModel(cfg.model_name || "gemini-1.5-pro");
      setEditTemp(cfg.temperature ?? 0.7);
      setEditPrompt(cfg.system_instructions || "");
    }
  }, [selectedAgent, agents]);

  const handleSaveShadowPolicy = async () => {
    if (shadowReason.trim().length < MIN_REASON_CHARS) {
      toast.error(`Please provide an audited reason (at least ${MIN_REASON_CHARS} characters)`);
      return;
    }
    setSavingShadow(true);
    try {
      await fetchApi("/settings/shadow-paid", {
        method: "PUT",
        headers: { "X-CSRF-Token": localStorage.getItem("csrf_token") || "" },
        body: JSON.stringify({
          allow_paid: shadowToggle,
          reason: shadowReason.trim(),
        }),
      });
      toast.success(
        shadowToggle
          ? "Shadow Mode updated: Paid candidate models enabled"
          : "Shadow Mode updated: Paid models restricted"
      );
      setShadowReason("");
      await onReloadShadowPaid();
    } catch (err: any) {
      toast.error(err?.message || "Failed to update shadow model policy");
    } finally {
      setSavingShadow(false);
    }
  };

  const handleSaveAgent = async () => {
    if (!selectedAgent) return;
    setSavingAgent(true);
    try {
      await fetchApi(`/settings/agents/${selectedAgent}`, {
        method: "PATCH",
        headers: { "X-CSRF-Token": localStorage.getItem("csrf_token") || "" },
        body: JSON.stringify({
          model_name: editModel,
          temperature: editTemp,
          system_instructions: editPrompt,
        }),
      });
      toast.success(`${selectedAgent} agent persona configuration saved!`);
      await onReloadAgents();
    } catch (err: any) {
      toast.error(err?.message || "Failed to save agent configuration");
    } finally {
      setSavingAgent(false);
    }
  };

  // Find daily budget entry
  const dailyBudgetEntry = entries.find((e) => e.key === "LLM_DAILY_BUDGET_USD");

  // Filter routes
  const filteredRoutes = routes.filter((r) => {
    if (routeFilter === "editable" && !r.editable) return false;
    if (routeFilter === "locked" && !r.protected) return false;
    if (routeFilter === "overridden" && !r.overridden) return false;
    if (routeFilter === "local" && !r.local_allowed) return false;
    if (routeSearch.trim()) {
      const q = routeSearch.toLowerCase();
      const matchRole = r.role.toLowerCase().includes(q);
      const matchPrimary = (r.effective.primary_model || "").toLowerCase().includes(q);
      const matchFallback = (r.effective.fallback_model || "").toLowerCase().includes(q);
      if (!matchRole && !matchPrimary && !matchFallback) return false;
    }
    return true;
  });

  return (
    <div className="space-y-8">
      {/* Area 1: Daily Spend & Shadow Mode */}
      <section className="space-y-4">
        <div className="border-b border-white/10 pb-3">
          <h3 className="text-sm font-bold uppercase tracking-wider text-cyan-400 flex items-center gap-2">
            <DollarSign className="w-4 h-4" aria-hidden="true" />
            LLM Spend Governance & Shadow Model Evaluation
          </h3>
          <p className="text-xs text-zinc-400 mt-1">
            Global budget guard and shadow evaluation framework (ADR 0002 / ADR 0008). The gateway throttles non-critical AI calls once the daily cap is exceeded.
          </p>
        </div>

        <div className="grid grid-cols-1 gap-4 @4xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)] @4xl:items-start">
          {dailyBudgetEntry && (
            <SettingList
              entries={[dailyBudgetEntry]}
              allEntries={entries}
              onSaved={() => void onReloadRegistry()}
              highlightedKey={highlightedKey}
            />
          )}

          {/* Shadow Model Card */}
          <div className="rounded-xl border border-white/10 bg-zinc-950/60 p-5 space-y-4 flex flex-col justify-between">
            <div>
              <div className="flex items-center justify-between">
                <span className="font-mono text-xs text-cyan-300">AI_SHADOW_ALLOW_PAID</span>
                <span
                  className={`text-[10px] font-bold px-2 py-0.5 rounded border uppercase tracking-wider ${
                    shadowAllowPaid
                      ? "border-emerald-500/40 bg-emerald-950/40 text-emerald-300"
                      : "border-zinc-700 bg-zinc-900/60 text-zinc-400"
                  }`}
                >
                  {shadowAllowPaid ? "Paid Models Allowed" : "Free Models Only"}
                </span>
              </div>
              <h4 className="text-sm font-semibold text-zinc-100 mt-2">
                Shadow Candidate Model Paid Evaluation
              </h4>
              <p className="text-xs text-zinc-400 mt-1 leading-relaxed">
                Controls whether candidate LLMs in shadow mode (evaluating side-by-side with production models) may incur commercial API costs.
              </p>
            </div>

            <div className="space-y-3 pt-2 border-t border-white/5">
              <label className="flex items-center gap-2.5 cursor-pointer">
                <input
                  type="checkbox"
                  checked={shadowToggle}
                  onChange={(e) => setShadowToggle(e.target.checked)}
                  className="rounded border-zinc-700 bg-zinc-900 text-cyan-500 focus:ring-cyan-500/50 w-4 h-4"
                />
                <span className="text-xs font-medium text-zinc-200">
                  Allow commercial paid models in shadow evaluation
                </span>
              </label>

              {shadowToggle !== shadowAllowPaid && (
                <div className="space-y-2 rounded-lg border border-cyan-500/30 bg-zinc-900/60 p-3">
                  <label className="block text-[11px] text-zinc-400">
                    Audited Reason (min {MIN_REASON_CHARS} chars)
                    <input
                      type="text"
                      value={shadowReason}
                      onChange={(e) => setShadowReason(e.target.value)}
                      placeholder="e.g. Enabling Claude 3.5 Sonnet for shadow benchmark"
                      className="mt-1 block w-full rounded border border-white/10 bg-zinc-950 px-2 py-1 text-xs font-mono text-zinc-100 focus:border-cyan-500 focus:outline-none"
                    />
                  </label>
                  <Button
                    type="button"
                    onClick={() => void handleSaveShadowPolicy()}
                    disabled={savingShadow || shadowReason.trim().length < MIN_REASON_CHARS}
                    className="w-full bg-cyan-600 hover:bg-cyan-500 text-zinc-950 text-xs font-bold uppercase tracking-wider"
                  >
                    {savingShadow ? "Saving Policy…" : "Save Shadow Policy"}
                  </Button>
                </div>
              )}

              <Button
                type="button"
                variant="outline"
                onClick={() => dispatchSwitchWorkspace({ mode: "mission", view: "autonomy" })}
                className="w-full border-white/10 bg-zinc-900/60 hover:bg-zinc-800 text-zinc-300 text-xs gap-1.5"
              >
                <Sparkles className="w-3.5 h-3.5 text-cyan-400" aria-hidden="true" />
                Open Autonomy & Evals in Mission Control
              </Button>
            </div>
          </div>
        </div>
      </section>

      {/* Area 2: AI Model Routing */}
      <section className="space-y-4">
        <div className="border-b border-white/10 pb-3 flex flex-wrap items-center justify-between gap-3">
          <div>
            <h3 className="text-sm font-bold uppercase tracking-wider text-cyan-400 flex items-center gap-2">
              <Cpu className="w-4 h-4" aria-hidden="true" />
              AI Model Routing Policy (ai_model_routes)
            </h3>
            <p className="text-xs text-zinc-400 mt-1">
              Deterministic routing matrix per agent role (ADR 0007 §2). Protected roles are certified and locked to fail-closed behavior.
            </p>
          </div>
          <span className="text-xs text-zinc-400 font-mono">
            {routes.length} Active Roles
          </span>
        </div>

        {/* Filters */}
        <div className="flex flex-wrap items-center justify-between gap-3 bg-zinc-900/40 p-3 rounded-lg border border-white/5">
          <div role="radiogroup" aria-label="Route filter" className="flex flex-wrap items-center gap-1.5">
            {[
              { id: "all" as const, label: "All Roles" },
              { id: "editable" as const, label: "Editable" },
              { id: "locked" as const, label: "Locked / Protected" },
              { id: "overridden" as const, label: "Overridden" },
              { id: "local" as const, label: "Local Allowed" },
            ].map(({ id, label }) => (
              <button
                key={id}
                type="button"
                role="radio"
                aria-checked={routeFilter === id}
                onClick={() => setRouteFilter(id)}
                className={`px-2.5 py-1 rounded text-xs font-medium transition-all ${
                  routeFilter === id
                    ? "bg-cyan-900/50 text-cyan-300 border border-cyan-500/40"
                    : "text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800/50"
                }`}
              >
                {label}
              </button>
            ))}
          </div>

          <div className="w-full @2xl:w-64">
            <input
              type="text"
              aria-label="Filter routes by role or model"
              value={routeSearch}
              onChange={(e) => setRouteSearch(e.target.value)}
              placeholder="Filter by role or model..."
              className="w-full bg-zinc-950 border border-white/10 rounded px-2.5 py-1 text-xs text-zinc-200 placeholder:text-zinc-600 focus:border-cyan-500 focus:outline-none"
            />
          </div>
        </div>

        {/* Routes List */}
        <div className="space-y-3">
          {filteredRoutes.map((route) => {
            const isOpen = openRouteRole === route.role;
            return (
              <div
                key={route.role}
                className={`rounded-xl border p-4 transition-all ${
                  route.protected
                    ? "border-rose-900/40 bg-zinc-950/60"
                    : "border-white/10 bg-zinc-950/60"
                }`}
              >
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-sm font-semibold text-zinc-100">
                      {route.role}
                    </span>
                    {route.protected && (
                      <span className="inline-flex items-center gap-1 text-[10px] font-bold px-1.5 py-0.5 rounded border border-rose-900/60 bg-rose-950/40 text-rose-300">
                        <Lock className="w-2.5 h-2.5" aria-hidden="true" />
                        LOCKED / PROTECTED
                      </span>
                    )}
                    {route.overridden && (
                      <span className="text-[10px] font-bold px-1.5 py-0.5 rounded border border-cyan-900/60 bg-cyan-950/40 text-cyan-300">
                        OVERRIDDEN
                      </span>
                    )}
                    {route.local_allowed && (
                      <span className="text-[10px] font-medium px-1.5 py-0.5 rounded border border-purple-900/60 bg-purple-950/30 text-purple-300">
                        Local Ollama OK
                      </span>
                    )}
                  </div>

                  <div className="text-right font-mono text-xs text-zinc-300 flex flex-wrap items-center gap-2">
                    <span className="text-cyan-400 font-semibold">
                      {route.effective.primary_model || "gemini-1.5-pro"}
                    </span>
                    {route.effective.fallback_model && (
                      <span className="text-zinc-500">
                        → {route.effective.fallback_model}
                      </span>
                    )}
                    {route.effective.fallback_policy && (
                      <span className="text-zinc-400 text-[11px] border border-white/10 px-1 rounded">
                        {route.effective.fallback_policy}
                      </span>
                    )}
                  </div>
                </div>

                {/* Sub-row telemetry */}
                <div className="mt-2.5 flex flex-wrap items-center gap-4 text-[11px] text-zinc-500 font-mono">
                  <span>
                    Daily Cap:{" "}
                    <strong className="text-zinc-300">
                      ${route.effective.daily_cap_usd ?? route.default.daily_cap_usd ?? "unlimited"}
                    </strong>
                  </span>
                  <span>
                    Max Tokens:{" "}
                    <strong className="text-zinc-300">
                      {route.effective.max_output_tokens ?? route.default.max_output_tokens ?? "default"}
                    </strong>
                  </span>
                  <span>
                    Thinking Budget:{" "}
                    <strong className="text-zinc-300">
                      {route.effective.thinking_budget ?? route.default.thinking_budget ?? "0"}
                    </strong>
                  </span>
                  {route.editable && (
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      onClick={() => setOpenRouteRole(isOpen ? null : route.role)}
                      className="ml-auto text-[11px] h-6 px-2.5 border-white/10 bg-zinc-900 hover:bg-zinc-800 text-zinc-300"
                    >
                      {isOpen ? "Close Editor" : "Edit Route"}
                    </Button>
                  )}
                </div>

                {isOpen && (
                  <RouteEditor
                    route={route}
                    onSaved={() => {
                      setOpenRouteRole(null);
                      void onReloadRoutes();
                    }}
                    onClose={() => setOpenRouteRole(null)}
                  />
                )}
              </div>
            );
          })}
        </div>
      </section>

      {/* Area 3: AI Agent Personas & Prompts */}
      <section className="space-y-4">
        <div className="border-b border-white/10 pb-3 flex flex-wrap items-center justify-between gap-3">
          <div>
            <h3 className="text-sm font-bold uppercase tracking-wider text-cyan-400 flex items-center gap-2">
              <Sparkles className="w-4 h-4" aria-hidden="true" />
              AI Agent Personas & System Instructions
            </h3>
            <p className="text-xs text-zinc-400 mt-1">
              Customize model parameters, temperature variance, and system prompt constraints for individual specialized agents.
            </p>
          </div>
        </div>

        {/* Unavailable state when loading or error */}
        {(!selectedAgent || !agents[selectedAgent] || loadingAgents || agentsLoadError) ? (
          <div
            className="rounded-xl border border-white/10 bg-zinc-950/60 p-6 text-sm text-zinc-400 space-y-3"
            role="status"
            data-testid="agent-config-unavailable"
          >
            {loadingAgents ? (
              <p>
                {agentsSlow
                  ? "Still loading agent configurations… this is taking longer than usual."
                  : "Loading agent configurations…"}
              </p>
            ) : agentsLoadError ? (
              <>
                <p className="text-amber-200">Could not load agent configurations: {agentsLoadError}</p>
                <Button
                  onClick={() => void onReloadAgents()}
                  className="bg-zinc-800 hover:bg-zinc-700 text-zinc-100 text-xs uppercase tracking-wider"
                >
                  Retry
                </Button>
              </>
            ) : Object.keys(agents).length === 0 ? (
              <>
                <p>The API returned no agent configurations, so there is nothing to edit yet.</p>
                <Button
                  onClick={() => void onReloadAgents()}
                  className="bg-zinc-800 hover:bg-zinc-700 text-zinc-100 text-xs uppercase tracking-wider"
                >
                  Check again
                </Button>
              </>
            ) : (
              <p>Select an agent to edit its configuration.</p>
            )}
          </div>
        ) : (
          <div className="grid grid-cols-1 @3xl:grid-cols-4 gap-6 rounded-xl border border-white/10 bg-zinc-950/60 p-5">
            {/* Agent Selector Sidebar */}
            <div
              role="tablist"
              aria-label="Specialized agents"
              className="@3xl:col-span-1 space-y-1.5 border-b @3xl:border-b-0 @3xl:border-r border-white/10 pb-4 @3xl:pb-0 @3xl:pr-4"
            >
              <span className="text-[10px] font-semibold uppercase tracking-wider text-zinc-400 block mb-2" aria-hidden="true">
                Specialized Agents
              </span>
              {["research", "explainer", "generator", "hunter", "strategist"].map((name) => {
                const active = selectedAgent === name;
                return (
                  <button
                    key={name}
                    type="button"
                    role="tab"
                    aria-selected={active}
                    onClick={() => setSelectedAgent(name)}
                    className={`w-full text-left px-3 py-2 rounded-lg text-xs capitalize font-medium transition-all ${
                      active
                        ? "bg-cyan-950/50 text-cyan-300 border border-cyan-500/40"
                        : "text-zinc-400 hover:text-zinc-200 hover:bg-zinc-900/60 border border-transparent"
                    }`}
                  >
                    {name}
                  </button>
                );
              })}
            </div>

            {/* Agent Editor Form */}
            <div className="@3xl:col-span-3 space-y-5">
              <div>
                <h4 className="text-base font-bold capitalize text-white flex items-center gap-2">
                  {selectedAgent} Agent
                </h4>
                <p className="text-xs text-zinc-400 mt-0.5">
                  {AGENT_DESCRIPTIONS[selectedAgent] || "Configure agent instructions and model parameters."}
                </p>
              </div>

              <div className="grid grid-cols-1 @2xl:grid-cols-2 gap-4">
                <div className="space-y-1.5">
                  <label className="text-[11px] font-semibold text-zinc-400 uppercase tracking-wider">
                    Model
                  </label>
                  <select
                    aria-label="Model"
                    value={editModel}
                    onChange={(e) => setEditModel(e.target.value)}
                    className="w-full bg-zinc-900 border border-zinc-800 rounded-md p-2 text-xs text-white focus:border-cyan-500 focus:outline-none"
                  >
                    <option value="gemini-2.5-pro">Gemini 2.5 Pro</option>
                    <option value="gemini-2.5-flash">Gemini 2.5 Flash</option>
                    <option value="gemini-1.5-pro">Gemini 1.5 Pro</option>
                    <option value="gemini-1.5-flash">Gemini 1.5 Flash</option>
                    <option value="gemini-exp-1121">Gemini Exp 1121</option>
                  </select>
                </div>

                <div className="space-y-1.5">
                  <div className="flex items-center justify-between">
                    <label className="text-[11px] font-semibold text-zinc-400 uppercase tracking-wider">
                      Temperature
                    </label>
                    <span className="font-mono text-xs text-cyan-300 font-semibold">{editTemp}</span>
                  </div>
                  <input
                    type="range"
                    aria-label="Temperature"
                    min="0"
                    max="2"
                    step="0.1"
                    value={editTemp}
                    onChange={(e) => setEditTemp(parseFloat(e.target.value))}
                    className="w-full mt-2 accent-cyan-500 bg-zinc-800 h-1.5 rounded-lg cursor-pointer"
                  />
                  <div className="flex justify-between text-[10px] text-zinc-400">
                    <span>Deterministic (0.0)</span>
                    <span>Creative (2.0)</span>
                  </div>
                </div>
              </div>

              <div className="space-y-1.5">
                <div className="flex items-center justify-between">
                  <label className="text-[11px] font-semibold text-zinc-400 uppercase tracking-wider">
                    System Instructions
                  </label>
                  <span className="text-[10px] font-mono text-zinc-400">
                    {editPrompt.length} / 5000 characters
                  </span>
                </div>
                <textarea
                  aria-label="System prompt"
                  maxLength={5000}
                  rows={8}
                  value={editPrompt}
                  onChange={(e) => setEditPrompt(e.target.value)}
                  className="w-full bg-zinc-900 border border-zinc-800 rounded-md p-3 text-xs text-zinc-300 font-mono resize-y focus:border-cyan-500 focus:outline-none"
                  placeholder="Enter the agent's base system prompt here..."
                />
              </div>

              <div className="flex justify-end pt-2">
                <Button
                  type="button"
                  onClick={() => void handleSaveAgent()}
                  disabled={savingAgent}
                  className="bg-cyan-600 hover:bg-cyan-500 text-zinc-950 font-bold text-xs gap-2"
                >
                  <Save className="w-3.5 h-3.5" aria-hidden="true" />
                  {savingAgent ? "Saving…" : "Save Configuration"}
                </Button>
              </div>
            </div>
          </div>
        )}
      </section>
    </div>
  );
};
