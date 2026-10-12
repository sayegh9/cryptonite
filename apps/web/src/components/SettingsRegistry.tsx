"use client";

import React, { useEffect, useState } from "react";
import { fetchApi } from "@/lib/api";
import { toast } from "sonner";
import { Button } from "./ui/button";
import { dispatchSwitchWorkspace } from "@/lib/navigation";

/** Platform settings (ADR 0007): every setting with its tier, apply mode and where it lives, and the health
 *  warnings. The server enforces the tier rules; this panel shows them and sends each change to the server. */
const TIMEOUT_MS = 10_000;
const MIN_REASON_CHARS = 5;

interface RegistryEntry {
  key: string;
  label: string;
  default: string;
  minimum: string | null;
  maximum: string | null;
  apply: "hot" | "restart" | "deploy";
  tier: string;
  writable: boolean;
  adr: string;
  store: string;
  note: string;
  decimals: number;
  value: string | null;
  value_source: "api" | "service" | "code" | "default";
  effective: string | null; // what the service reports it applies, when it reports one
}

interface Warning {
  code: string;
  severity: "critical" | "warn" | "info";
  text: string;
}

interface EnvHealth {
  environment_mode: string;
  kill_switch_active: boolean;
  warnings: Warning[];
}

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
];

const TIER_TEXT: Record<string, string> = {
  "T/L": "tighter is free, looser needs TOTP",
  TOTP: "TOTP for every change",
  Session: "operator session",
  G: "changed by ADR only",
  R: "read-only",
  Fixed: "fixed in code",
};

const APPLY_TEXT: Record<RegistryEntry["apply"], string> = {
  hot: "applies within seconds",
  restart: "applies on service restart",
  deploy: "applies on deploy",
};

const WARNING_CLASS: Record<Warning["severity"], string> = {
  critical: "border-red-900/60 bg-red-950/40 text-red-300",
  warn: "border-amber-900/60 bg-amber-950/30 text-amber-200",
  info: "border-cyan-900/60 bg-cyan-950/30 text-cyan-200",
};

const INPUT_CLASS = "mt-1 block rounded border border-white/10 bg-zinc-950 px-2 py-1 font-mono text-sm text-zinc-100";

/** A value with at most `decimals` decimal places, no sign. The server enforces the same rule. */
const decimalPattern = (decimals: number) =>
  decimals === 0 ? /^\d+$/ : new RegExp(`^\\d+(\\.\\d{1,${decimals}})?$`);
const stepFor = (decimals: number) => (1 / 10 ** decimals).toFixed(decimals);

function sourceText(entry: RegistryEntry): string {
  if (entry.value !== null) return entry.value;
  if (entry.value_source === "default") return "not set";
  return entry.value_source === "service" ? "read by the service" : "set in code";
}

function boundsText(entry: RegistryEntry): string {
  const parts: string[] = [];
  if (entry.minimum !== null) parts.push(`min ${entry.minimum}`);
  if (entry.maximum !== null) parts.push(`max ${entry.maximum}`);
  return parts.join(" · ");
}

const Chip = ({ children }: { children: React.ReactNode }) => (
  <span className="rounded border border-white/10 px-1.5 py-0.5 text-zinc-400">{children}</span>
);

/** A numeric change: the server decides the tier rule; the form asks for TOTP when the value goes up. */
const NumericEditor: React.FC<{ entry: RegistryEntry; onSaved: () => void }> = ({ entry, onSaved }) => {
  const current = entry.value ?? entry.default;
  const [amount, setAmount] = useState(current);
  const [reason, setReason] = useState("");
  const [totp, setTotp] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => setAmount(current), [current]);

  const next = Number(amount);
  const inBounds =
    decimalPattern(entry.decimals).test(amount.trim()) &&
    (entry.minimum === null || next >= Number(entry.minimum)) &&
    (entry.maximum === null || next <= Number(entry.maximum));
  const changed = inBounds && next !== Number(current);
  const raising = changed && next > Number(current);
  const canSave =
    changed && reason.trim().length >= MIN_REASON_CHARS && (!raising || /^\d{6}$/.test(totp)) && !saving;

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      await fetchApi(`/settings/registry/${entry.key}`, {
        method: "PUT",
        headers: { "X-CSRF-Token": localStorage.getItem("csrf_token") || "" },
        body: JSON.stringify({ value: amount.trim(), reason: reason.trim(), totp_code: raising ? totp : null }),
      });
      toast.success(`${entry.label} saved`);
      setReason("");
      setTotp("");
      onSaved();
    } catch (err: any) {
      setError(String(err?.message || "The change was not saved.").replace(/^TOTP_REQUIRED:\s*/, ""));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="mt-3 space-y-2 rounded border border-white/10 bg-zinc-900/40 p-3">
      <div className="flex flex-wrap items-end gap-3">
        <label className="text-[11px] text-zinc-400">
          New value
          <input
            type="number"
            inputMode="decimal"
            step={stepFor(entry.decimals)}
            min={entry.minimum ?? undefined}
            max={entry.maximum ?? undefined}
            value={amount}
            onChange={(e) => setAmount(e.target.value)}
            className={`${INPUT_CLASS} w-32`}
          />
        </label>
        {raising && (
          <label className="text-[11px] text-amber-200">
            Your TOTP code (a higher value needs it)
            <input
              inputMode="numeric"
              autoComplete="one-time-code"
              maxLength={6}
              value={totp}
              onChange={(e) => setTotp(e.target.value.replace(/\D/g, ""))}
              className={`${INPUT_CLASS} w-32`}
            />
          </label>
        )}
      </div>
      <label className="block text-[11px] text-zinc-400">
        Reason (audited, at least {MIN_REASON_CHARS} characters)
        <input
          type="text"
          maxLength={300}
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          className={`${INPUT_CLASS} w-full`}
        />
      </label>
      {amount.trim() !== "" && !inBounds && (
        <p className="text-[11px] text-red-300">
          Enter a number{boundsText(entry) ? ` (${boundsText(entry)})` : ""} with at most {entry.decimals} decimal
          places.
        </p>
      )}
      {error && (
        <p role="alert" className="text-[11px] text-red-300">
          {error}
        </p>
      )}
      <Button
        onClick={() => void save()}
        disabled={!canSave}
        className="bg-cyan-600 text-xs font-bold uppercase tracking-wider text-zinc-950 hover:bg-cyan-500 disabled:opacity-40"
      >
        {saving ? "Saving…" : raising ? "Save with TOTP" : "Save"}
      </Button>
    </div>
  );
};

const SettingRow: React.FC<{ entry: RegistryEntry; onSaved: () => void }> = ({ entry, onSaved }) => (
  <li className="border-b border-white/5 py-3 last:border-0">
    <div className="flex flex-wrap items-baseline justify-between gap-2">
      <div>
        <div className="text-sm text-zinc-100">{entry.label}</div>
        <div className="font-mono text-[11px] text-zinc-500">{entry.key}</div>
      </div>
      <div className="text-right">
        <div className="font-mono text-sm text-cyan-300">{sourceText(entry)}</div>
        <div className="text-[11px] text-zinc-500">
          default {entry.default}
          {boundsText(entry) ? ` · ${boundsText(entry)}` : ""}
        </div>
        {entry.effective !== null && (
          <div className="text-[11px] text-zinc-500" title="The value the service reported it applies">
            applied now {entry.effective}
          </div>
        )}
      </div>
    </div>
    <div className="mt-2 flex flex-wrap gap-2 text-[11px]">
      <Chip>
        {entry.tier}: {TIER_TEXT[entry.tier] ?? entry.tier}
      </Chip>
      <Chip>{APPLY_TEXT[entry.apply]}</Chip>
      <Chip>{/^\d+$/.test(entry.adr) ? `ADR ${entry.adr}` : entry.adr}</Chip>
      <Chip>{entry.store}</Chip>
    </div>
    {entry.note && <p className="mt-1 text-[11px] text-zinc-500">{entry.note}</p>}
    {entry.writable && <NumericEditor entry={entry} onSaved={onSaved} />}
  </li>
);

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
        <p className="text-[11px] text-zinc-500">
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
          <section key={group.title} className="space-y-1">
            <h3 className="text-xs font-bold uppercase tracking-widest text-zinc-300">{group.title}</h3>
            {group.hint && <p className="text-[11px] text-zinc-500">{group.hint}</p>}
            {group.opens === "operator" && (
              <Button
                onClick={() => dispatchSwitchWorkspace({ mode: "operator" })}
                className="bg-zinc-800 text-xs uppercase tracking-wider text-zinc-100 hover:bg-zinc-700"
              >
                Open Operator
              </Button>
            )}
            <ul>
              {rows.map((entry) => (
                <SettingRow key={entry.key} entry={entry} onSaved={() => void load()} />
              ))}
            </ul>
          </section>
        );
      })}
    </div>
  );
};
