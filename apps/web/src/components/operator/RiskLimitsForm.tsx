"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  CONFIGURABLE_LIMITS,
  fetchEnforcedLimits,
  fetchRiskLimits,
  isEnforcedLimitsValid,
  loosenedLimits,
  proposedValue,
  updateRiskLimits,
  type ConfigurableLimit,
  type RiskLimitsConfig,
} from "@/lib/limits";

const LABELS: Record<ConfigurableLimit, string> = {
  max_daily_loss_pct: "Max daily loss",
  max_order_equity_pct: "Max order size",
  cross_mandate_exposure_pct: "Cross-mandate BTC exposure",
};

type FieldErrors = Partial<Record<ConfigurableLimit | "form", string>>;

/** The server's 422 detail is a list of {loc: ["body", field], msg}; anything else is a form-level error. */
function serverErrors(err: { status?: number; message?: string; data?: { detail?: unknown } }): FieldErrors {
  const detail = err?.data?.detail;
  if (Array.isArray(detail)) {
    const out: FieldErrors = {};
    for (const d of detail as { loc?: unknown[]; msg?: string }[]) {
      const field = (Array.isArray(d.loc) ? d.loc[d.loc.length - 1] : null) as string | null;
      const key = field && (CONFIGURABLE_LIMITS as readonly string[]).includes(field) ? (field as ConfigurableLimit) : "form";
      out[key] = [out[key], d.msg || "invalid"].filter(Boolean).join("; ");
    }
    return out;
  }
  return { form: typeof detail === "string" ? detail : err?.status ? `HTTP ${err.status}: ${err.message ?? ""}` : err?.message || "unreachable" };
}

const emptyInputs = (): Record<ConfigurableLimit, string> => ({ max_daily_loss_pct: "", max_order_equity_pct: "", cross_mandate_exposure_pct: "" });

/**
 * F7: operator overrides of the paper limits. Values can only be tightened below the code defaults;
 * tightening is applied directly, loosening (a higher value, or clearing back to the default) asks for
 * the operator's TOTP. The server enforces the same rules and its errors are shown as they come.
 */
export function RiskLimitsForm({ onSaved }: { onSaved?: () => void }) {
  const [config, setConfig] = useState<RiskLimitsConfig | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [inputs, setInputs] = useState<Record<ConfigurableLimit, string>>(emptyInputs);
  const [reason, setReason] = useState("");
  const [totp, setTotp] = useState("");
  const [busy, setBusy] = useState(false);
  const [errors, setErrors] = useState<FieldErrors>({});
  const [saved, setSaved] = useState<string | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  // Monotonic "save generation": an in-flight GET from an older save must never win over a newer one.
  const genRef = useRef(0);

  // On unmount, stop the poll AND invalidate any in-flight check so it cannot setState afterwards.
  useEffect(() => () => {
    genRef.current++;
    if (pollRef.current !== null) clearInterval(pollRef.current);
  }, []);

  const load = useCallback(() => {
    fetchRiskLimits()
      .then((c) => {
        setConfig(c);
        setLoadError(null);
        setInputs({
          max_daily_loss_pct: c.overrides.max_daily_loss_pct ?? "",
          max_order_equity_pct: c.overrides.max_order_equity_pct ?? "",
          cross_mandate_exposure_pct: c.overrides.cross_mandate_exposure_pct ?? "",
        });
      })
      .catch((err: { status?: number; message?: string }) => setLoadError(err?.status ? `HTTP ${err.status}` : err?.message || "unreachable"));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const loosened = useMemo(() => (config ? loosenedLimits(config, inputs) : []), [config, inputs]);
  const needsTotp = loosened.length > 0;

  if (loadError) {
    return <p className="text-xs font-mono text-zinc-400" data-testid="risk-limits-unavailable">Configurable limits unavailable ({loadError}).</p>;
  }
  if (!config) return <p className="text-xs font-mono text-zinc-500">loading…</p>;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErrors({});
    setSaved(null);
    try {
      const value = (k: ConfigurableLimit) => (inputs[k].trim() === "" ? null : inputs[k].trim());
      const res = await updateRiskLimits({
        environment: config.environment,
        account_id: config.account_id,
        max_daily_loss_pct: value("max_daily_loss_pct"),
        max_order_equity_pct: value("max_order_equity_pct"),
        cross_mandate_exposure_pct: value("cross_mandate_exposure_pct"),
        reason,
        ...(needsTotp ? { totp_code: totp } : {}),
      });
      const savedVer = res.override_version ?? 0;
      setTotp("");
      setReason("");
      // Poll GET /control/enforced-limits until the orchestrator reports this exact
      // override version as enforced. Until then (stale/missing/error) the honest
      // status is "Saved. Not yet enforced"; there is no fabricated enforced_at.
      // Each save takes a fresh generation; a check superseded by a later save is inert.
      if (pollRef.current !== null) clearInterval(pollRef.current);
      const gen = ++genRef.current;
      let attempts = 0;
      const maxAttempts = 60; // ~2 min at 2s intervals
      let resolved = false;
      const stop = () => { if (pollRef.current !== null) { clearInterval(pollRef.current); pollRef.current = null; } };
      const pending = (enforced: number) => setSaved(`Saved. Not yet enforced (orchestrator v${enforced}, override v${savedVer}).`);
      const check = async () => {
        if (resolved) { stop(); return; } // already settled: wind the poll down
        attempts++;
        if (genRef.current !== gen) { resolved = true; return; } // superseded by a newer save (or unmount)
        let enforced = 0;
        try {
          const state = await fetchEnforcedLimits();
          if (genRef.current !== gen) { resolved = true; return; } // old in-flight response: drop it
          const enforcedAt = state.enforced_at;
          enforced = state.effective_version ?? 0;
          // Only claim "enforced" with a complete, fresh, temporally consistent telemetry
          // report and an exact version match on a real reported enforced_at.
          if (isEnforcedLimitsValid(state) && enforced > 0 && savedVer > 0 && enforced === savedVer && enforcedAt) {
            resolved = true;
            stop();
            setSaved(`Enforced since ${new Date(enforcedAt).toLocaleString()}.`);
            return;
          }
        } catch {
          if (genRef.current !== gen) { resolved = true; return; }
          enforced = 0; // unavailable telemetry fails closed to pending
        }
        if (!resolved) {
          pending(enforced);
          if (attempts >= maxAttempts) { resolved = true; stop(); }
        }
      };
      pending(0);
      void check().catch(() => undefined); // first poll fires immediately; never blocks the POST returning
      pollRef.current = setInterval(check, 2000);
      load();
      onSaved?.();
    } catch (err) {
      setErrors(serverErrors(err as { status?: number; message?: string; data?: { detail?: unknown } }));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit} className="space-y-3 text-xs font-mono" data-testid="risk-limits-form" aria-label="Configurable risk limits">
      <table className="w-full tabular-nums">
        <thead>
          <tr className="text-zinc-500 text-left">
            <th className="font-normal pb-1">Limit (% of equity)</th>
            <th className="font-normal pb-1 text-right">Current</th>
            <th className="font-normal pb-1 text-right">Default</th>
            <th className="font-normal pb-1 text-right">New</th>
          </tr>
        </thead>
        <tbody>
          {CONFIGURABLE_LIMITS.map((k) => {
            const next = proposedValue(inputs[k], config.defaults[k]);
            const direction = !Number.isFinite(next) ? "" : next > Number(config.effective[k]) ? "loosen" : next < Number(config.effective[k]) ? "tighten" : "";
            return (
              <tr key={k} className="border-t border-white/5 text-zinc-200 align-top">
                <td className="py-1.5">
                  <label htmlFor={`limit-${k}`}>{LABELS[k]}</label>
                  {errors[k] && <p className="text-red-400 mt-0.5" role="alert" data-testid={`risk-limit-error-${k}`}>{errors[k]}</p>}
                </td>
                <td className="py-1.5 text-right" data-testid={`risk-limit-current-${k}`}>{config.effective[k]}%</td>
                <td className="py-1.5 text-right text-zinc-400">{config.defaults[k]}%</td>
                <td className="py-1.5 text-right">
                  <input
                    id={`limit-${k}`}
                    inputMode="decimal"
                    placeholder={`${config.defaults[k]} (default)`}
                    value={inputs[k]}
                    onChange={(e) => setInputs((prev) => ({ ...prev, [k]: e.target.value.replace(/[^0-9.]/g, "") }))}
                    className="w-24 rounded border border-white/10 bg-zinc-900 px-2 py-0.5 text-right text-zinc-100"
                  />
                  {direction && <span className={`ml-1 ${direction === "loosen" ? "text-amber-400" : "text-emerald-400"}`}>{direction}</span>}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <label className="block">
        <span className="text-zinc-400">Reason (audited)</span>
        <input
          aria-label="Reason"
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          className="mt-1 w-full rounded border border-white/10 bg-zinc-900 px-2 py-1 text-zinc-100"
          placeholder="Why this change"
        />
      </label>
      {needsTotp && (
        <div className="rounded border border-amber-700 bg-amber-950/20 p-2" data-testid="risk-limits-totp">
          <p className="mb-1 text-amber-200">Loosens {loosened.map((k) => LABELS[k]).join(", ")} toward the default: enter your TOTP code.</p>
          <label className="flex items-center gap-2">
            <span>TOTP</span>
            <input
              aria-label="TOTP code"
              inputMode="numeric"
              maxLength={6}
              value={totp}
              onChange={(e) => setTotp(e.target.value.replace(/\D/g, ""))}
              className="w-24 rounded border border-white/10 bg-zinc-900 px-2 py-0.5 text-zinc-100"
            />
          </label>
        </div>
      )}
      {errors.form && <p className="text-red-400" role="alert" data-testid="risk-limits-error">{errors.form}</p>}
      {saved && <p className="text-emerald-400" role="status" data-testid="risk-limits-saved">{saved}</p>}
      <div className="flex items-center justify-between">
        <p className="text-2xs text-zinc-500">
          Values can only be tightened below the defaults. Empty = default. Min order size ({config.min_order_equity_pct}%) and the account
          backstop are not configurable.
        </p>
        <button
          type="submit"
          disabled={busy || reason.trim().length < 5 || (needsTotp && totp.length !== 6)}
          className="ml-3 shrink-0 rounded bg-amber-600 px-3 py-1 font-bold text-black disabled:opacity-40"
        >
          {busy ? "Saving…" : needsTotp ? "Loosen with TOTP" : "Save"}
        </button>
      </div>
      {config.updated_by && (
        <p className="text-2xs text-zinc-500">
          Last change by {config.updated_by}
          {config.updated_at ? ` at ${new Date(config.updated_at).toLocaleString()}` : ""}
          {config.reason ? `: ${config.reason}` : ""}
        </p>
      )}
    </form>
  );
}
