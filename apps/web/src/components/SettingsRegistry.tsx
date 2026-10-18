"use client";

import React, { useEffect, useState } from "react";
import { fetchApi } from "@/lib/api";
import { toast } from "sonner";
import { Button } from "./ui/button";
import { dispatchSwitchWorkspace } from "@/lib/navigation";
import { SettingCard, type RegistryEntry } from "./settings/controls/SettingCard";
import type { RouteView, Warning, EnvHealth } from "./settings/types";

const TIMEOUT_MS = 10_000;
const MIN_REASON_CHARS = 5;

const GROUPS: { title: string; hint?: string; opens?: "operator"; match: (key: string) => boolean }[] = [
  { title: "AI spend", match: (k) => k === "LLM_DAILY_BUDGET_USD" },
  {
    title: "Risk limits",
    hint: "Edited in Operator → Risk limits (ADR 0006).",
    opens: "operator",
    match: (k) => k.startsWith("risk."),
  },
  {
    title: "Exit rules",
    hint: "Read by the exit monitor every cycle. Lower is tighter: lowering is free, raising needs TOTP.",
    match: (k) => k.startsWith("exit."),
  },
  { title: "Alerts", match: (k) => k.startsWith("alerts.") },
  { title: "Scanner", match: (k) => k.startsWith("scanner.") },
  { title: "Universe", hint: "Changed through symbol onboarding, not here.", match: (k) => k.startsWith("universe.") },
  { title: "Fixed guardrails", hint: "Set in code. Shown for reference; never editable here.", match: (k) => k.startsWith("guard.") },
  { title: "Authentication & Security", hint: "Session lifetimes and brute-force lockout thresholds.", match: (k) => k.startsWith("auth.") },
];

const WARNING_CLASS: Record<Warning["severity"], string> = {
  critical: "border-red-900/60 bg-red-950/40 text-red-300",
  warn: "border-amber-900/60 bg-amber-950/30 text-amber-200",
  info: "border-cyan-900/60 bg-cyan-950/30 text-cyan-200",
};

const INPUT_CLASS = "mt-1 block rounded border border-white/10 bg-zinc-950 px-2 py-1 font-mono text-sm text-zinc-100";
const ROUTE_FALLBACK_POLICIES = ["", "FAIL_CLOSED", "DEGRADE"];

const RouteEditor: React.FC<{ route: RouteView; onSaved: () => void }> = ({ route, onSaved }) => {
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
      toast.success(`${route.role} route saved`);
      setTotp("");
      setReason("");
      onSaved();
    } catch (err: any) {
      setError(String(err?.message || "The route was not saved.").replace(/^TOTP_REQUIRED:\s*/, ""));
    } finally {
      setSaving(false);
    }
  };

  const hint = (value: string | number | null) => (value === null ? "default" : `default ${value}`);
  return (
    <div className="mt-2 space-y-2 rounded border border-white/10 bg-zinc-900/40 p-3">
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        <label className="text-[11px] text-zinc-400">
          Primary model ({hint(route.default.primary_model)})
          <input value={primary} onChange={(e) => setPrimary(e.target.value)} placeholder={route.default.primary_model ?? ""} className={`${INPUT_CLASS} w-full`} />
        </label>
        <label className="text-[11px] text-zinc-400">
          Fallback model ({hint(route.default.fallback_model)})
          <input value={fallback} onChange={(e) => setFallback(e.target.value)} placeholder={route.default.fallback_model ?? ""} className={`${INPUT_CLASS} w-full`} />
        </label>
        <label className="text-[11px] text-zinc-400">
          Fallback policy ({hint(route.default.fallback_policy)})
          <select value={policy} onChange={(e) => setPolicy(e.target.value)} className={`${INPUT_CLASS} w-full`}>
            {ROUTE_FALLBACK_POLICIES.map((p) => (
              <option key={p} value={p}>{p === "" ? "default" : p}</option>
            ))}
          </select>
        </label>
        <label className="text-[11px] text-zinc-400">
          Daily cap, USD ({hint(route.default.daily_cap_usd)})
          <input type="number" step="0.0001" min="0" value={cap} onChange={(e) => setCap(e.target.value)} className={`${INPUT_CLASS} w-full`} />
        </label>
        <label className="text-[11px] text-zinc-400">
          Max output tokens ({hint(route.default.max_output_tokens)})
          <input type="number" step="1" min="1" value={tokens} onChange={(e) => setTokens(e.target.value)} className={`${INPUT_CLASS} w-full`} />
        </label>
        <label className="text-[11px] text-zinc-400">
          Thinking budget ({hint(route.default.thinking_budget)})
          <input type="number" step="1" min="0" value={thinking} onChange={(e) => setThinking(e.target.value)} className={`${INPUT_CLASS} w-full`} />
        </label>
      </div>
      <p className="text-[11px] text-zinc-400">Leave a field blank to use its default. Local models are allowed only for triage, summarize and the embedder.</p>
      <label className="block text-[11px] text-zinc-400">
        Reason (audited, at least {MIN_REASON_CHARS} characters)
        <input type="text" maxLength={300} value={reason} onChange={(e) => setReason(e.target.value)} className={`${INPUT_CLASS} w-full`} />
      </label>
      <label className="block text-[11px] text-amber-200">
        Your TOTP code (every route change needs it)
        <input inputMode="numeric" autoComplete="one-time-code" maxLength={6} value={totp} onChange={(e) => setTotp(e.target.value.replace(/\D/g, ""))} className={`${INPUT_CLASS} w-32`} />
      </label>
      {error && <p role="alert" className="text-[11px] text-red-300">{error}</p>}
      <Button onClick={() => void save()} disabled={!canSave} className="bg-cyan-600 text-xs font-bold uppercase tracking-wider text-zinc-950 hover:bg-cyan-500 disabled:opacity-40">
        {saving ? "Saving…" : "Save with TOTP"}
      </Button>
    </div>
  );
};

const RoutingSection: React.FC = () => {
  const [routes, setRoutes] = useState<RouteView[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [openRole, setOpenRole] = useState<string | null>(null);

  const load = async () => {
    try {
      const out = await fetchApi("/settings/routes", { timeoutMs: TIMEOUT_MS });
      setRoutes(out?.routes ?? []);
      setLoadError(null);
    } catch (err: any) {
      setLoadError(err?.timeout ? "The request timed out." : err?.message || "The routes could not be loaded.");
    }
  };

  useEffect(() => {
    void load();
  }, []);

  return (
    <section className="space-y-2">
      <h3 className="text-xs font-bold uppercase tracking-widest text-zinc-300">AI model routing</h3>
      <p className="text-[11px] text-zinc-400">
        Which model answers each role, its fallback and its limits. The gateway applies a change within 30 s. Protected trade roles are locked.
      </p>
      {loadError && (
        <div className="flex items-center gap-3 text-sm text-amber-200" role="status">
          Could not load routes: {loadError}
          <Button onClick={() => void load()} className="bg-zinc-800 text-xs uppercase tracking-wider text-zinc-100 hover:bg-zinc-700">Retry</Button>
        </div>
      )}
      {!routes && !loadError && <p className="text-sm text-zinc-400">Loading routes…</p>}
      {routes && (
        <ul>
          {routes.map((route) => (
            <li key={route.role} className="border-b border-white/5 py-2 last:border-0">
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <div>
                  <span className="font-mono text-sm text-zinc-100">{route.role}</span>
                  {route.overridden && <span className="ml-2 rounded border border-cyan-900/60 px-1.5 text-[10px] text-cyan-300">overridden</span>}
                  {route.protected && <span className="ml-2 rounded border border-red-900/60 px-1.5 text-[10px] text-red-300">locked</span>}
                </div>
                <div className="text-right font-mono text-[11px] text-zinc-400">
                  {route.effective.primary_model}
                  {route.effective.fallback_model ? ` → ${route.effective.fallback_model}` : ""}
                  {route.effective.fallback_policy ? ` · ${route.effective.fallback_policy}` : ""}
                </div>
              </div>
              {route.editable && (
                <Button onClick={() => setOpenRole(openRole === route.role ? null : route.role)} className="mt-1 bg-zinc-800 text-[11px] uppercase tracking-wider text-zinc-100 hover:bg-zinc-700">
                  {openRole === route.role ? "Close" : "Edit"}
                </Button>
              )}
              {openRole === route.role && (
                <RouteEditor route={route} onSaved={() => { setOpenRole(null); void load(); }} />
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
};

export const SettingsRegistry: React.FC = () => {
  const [entries, setEntries] = useState<RegistryEntry[] | null>(null);
  const [health, setHealth] = useState<EnvHealth | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  const load = async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const [registry, environment] = await Promise.all([
        fetchApi("/settings/registry", { timeoutMs: TIMEOUT_MS }),
        fetchApi("/settings/environment", { timeoutMs: TIMEOUT_MS }),
      ]);
      setEntries(registry?.settings ?? []);
      setHealth(environment);
    } catch (err: any) {
      if (err?.status === 401 || err?.status === 403) {
        setLoadError("Your operator session is needed to view platform settings.");
      } else {
        setLoadError(err?.timeout ? "The request timed out." : err?.message || "The settings could not be loaded.");
      }
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, []);

  if (loadError) {
    return (
      <div className="space-y-3 p-6 text-sm text-zinc-400" role="status">
        <p className="text-amber-200">Could not load platform settings: {loadError}</p>
        <Button onClick={() => void load()} className="bg-zinc-800 text-xs uppercase tracking-wider text-zinc-100 hover:bg-zinc-700">
          Retry
        </Button>
      </div>
    );
  }
  if (!entries || !health) {
    return <p className="p-6 text-sm text-zinc-400">{loading ? "Loading platform settings…" : "No platform settings returned."}</p>;
  }

  return (
    <div className="space-y-8 p-6 text-white">
      <section className="space-y-2">
        <h3 className="text-xs font-bold uppercase tracking-widest text-zinc-300">Health</h3>
        <p className="text-[11px] text-zinc-400">
          Environment {health.environment_mode} · kill switch {health.kill_switch_active ? "latched" : "clear"}
        </p>
        {health.warnings.length === 0 ? (
          <p className="text-sm text-zinc-400">No warnings.</p>
        ) : (
          health.warnings.map((w) => (
            <div key={w.code} className={`rounded border px-3 py-2 text-sm ${WARNING_CLASS[w.severity]}`}>
              {w.text}
            </div>
          ))
        )}
      </section>

      {GROUPS.map((group) => {
        const rows = entries.filter((entry) => group.match(entry.key));
        if (rows.length === 0) return null;
        return (
          <section key={group.title} className="space-y-3">
            <div>
              <h3 className="text-xs font-bold uppercase tracking-widest text-zinc-300">{group.title}</h3>
              {group.hint && <p className="text-[11px] text-zinc-400 mt-0.5">{group.hint}</p>}
            </div>
            {group.opens === "operator" && (
              <Button
                onClick={() => dispatchSwitchWorkspace({ mode: "operator" })}
                className="bg-zinc-800 text-xs uppercase tracking-wider text-zinc-100 hover:bg-zinc-700"
              >
                Open Operator
              </Button>
            )}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              {rows.map((entry) => (
                <SettingCard key={entry.key} entry={entry} allEntries={entries} onSaved={() => void load()} />
              ))}
            </div>
          </section>
        );
      })}
      <RoutingSection />
    </div>
  );
};
