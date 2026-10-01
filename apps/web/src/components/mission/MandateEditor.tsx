"use client";

import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { createMandate, demoteMandate, editMandate } from "@/lib/mission/client";
import { dispatchMandateUpdated } from "@/lib/mission/events";
import type { Mandate } from "@/lib/mission/types";

interface Form {
  name: string;
  capital_budget_usd: string;
  instruments: string;
  max_position_pct: string;
  max_drawdown_pct: string;
  allow_perps: boolean;
  max_leverage: string;
  active_hours_utc: string;
  veto_window_s: string;
}

const EMPTY: Form = {
  name: "",
  capital_budget_usd: "10000",
  instruments: "BTCUSDT, ETHUSDT",
  max_position_pct: "10",
  max_drawdown_pct: "20",
  allow_perps: false,
  max_leverage: "1",
  active_hours_utc: "",
  veto_window_s: "600",
};

const canon = (s: string) => {
  const u = s.trim().toUpperCase();
  return u.includes(".") ? u : `${u}.BINANCE`;
};
const instrumentsOf = (s: string) => Array.from(new Set(s.split(",").map((x) => x.trim()).filter(Boolean).map(canon))).sort();

function toForm(m: Mandate): Form {
  return {
    name: m.name,
    capital_budget_usd: m.capital_budget_usd,
    instruments: m.instruments.map((i) => i.replace(".BINANCE", "")).join(", "),
    max_position_pct: m.max_position_pct,
    max_drawdown_pct: m.max_drawdown_pct,
    allow_perps: m.allow_perps,
    max_leverage: String(m.max_leverage),
    active_hours_utc: m.active_hours_utc ?? "",
    veto_window_s: String(m.veto_window_s),
  };
}

/** Mirrors the server's risk_adding_change: any edit that gives the AI more room needs TOTP. */
export function addsRisk(old: Mandate | null, f: Form): string[] {
  if (!old) return ["a new mandate"];
  const why: string[] = [];
  if (Number(f.capital_budget_usd) > Number(old.capital_budget_usd)) why.push("a larger budget");
  if (Number(f.max_position_pct) > Number(old.max_position_pct)) why.push("a larger position cap");
  if (Number(f.max_drawdown_pct) > Number(old.max_drawdown_pct)) why.push("a looser hard stop");
  if (f.allow_perps && !old.allow_perps) why.push("perps");
  if (Number(f.max_leverage) > old.max_leverage) why.push("more leverage");
  if (instrumentsOf(f.instruments).some((i) => !old.instruments.includes(i))) why.push("new instruments");
  if ((f.active_hours_utc || "") !== (old.active_hours_utc || "")) why.push("different active hours");
  if (Number(f.veto_window_s) < old.veto_window_s) why.push("a shorter veto window");
  return why;
}

/** Mandate editor (§4.4, §7.1): the fields the dispatcher-side mandate check enforces. */
export function MandateEditor({ mandates, selectedId, onSelect, onSaved }: {
  mandates: Mandate[];
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  onSaved: () => void;
}) {
  const current = useMemo(() => mandates.find((m) => m.id === selectedId) ?? null, [mandates, selectedId]);
  const [form, setForm] = useState<Form>(EMPTY);
  const [totp, setTotp] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setForm(current ? toForm(current) : EMPTY);
    setTotp("");
    setError(null);
  }, [current]);

  const reasons = addsRisk(current, form);
  const needsTotp = reasons.length > 0;
  const set = (k: keyof Form) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setForm((f) => ({ ...f, [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value }));

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const body: Record<string, unknown> = {
      name: form.name.trim(),
      capital_budget_usd: form.capital_budget_usd,
      instruments: instrumentsOf(form.instruments),
      max_position_pct: form.max_position_pct,
      max_drawdown_pct: form.max_drawdown_pct,
      max_leverage: Number(form.max_leverage),
      veto_window_s: Number(form.veto_window_s),
      ...(form.active_hours_utc ? { active_hours_utc: form.active_hours_utc } : current?.active_hours_utc ? { clear_active_hours: true } : {}),
      ...(current ? { allow_perps: form.allow_perps } : {}),
      ...(needsTotp ? { totp_code: totp } : {}),
    };
    try {
      const saved = current ? await editMandate(current.id, body) : await createMandate(body);
      toast.success(current ? `Saved ${saved.name}` : `Created ${saved.name} at L1 Copilot`);
      dispatchMandateUpdated({ mandateId: saved.id, change: current ? "edited" : "created" });
      onSelect(saved.id);
      setTotp("");
      onSaved();
    } catch (err: any) {
      setError(err?.message || "Save failed");
    } finally {
      setBusy(false);
    }
  };

  const input = "w-full rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-zinc-100";
  return (
    <section aria-label="Mandate editor" className="rounded border border-zinc-800 bg-zinc-950 p-3 font-mono text-[11px] text-zinc-300">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <label htmlFor="mandate-select" className="text-[10px] font-bold uppercase tracking-wider text-zinc-400">
          Mandate
        </label>
        <select
          id="mandate-select"
          value={selectedId ?? ""}
          onChange={(e) => onSelect(e.target.value || null)}
          className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-zinc-100"
        >
          <option value="">+ New mandate</option>
          {mandates.map((m) => (
            <option key={m.id} value={m.id}>
              {m.name} ({m.level_name}, {m.status})
            </option>
          ))}
        </select>
      </div>

      <form onSubmit={submit} className="grid grid-cols-1 gap-3 md:grid-cols-2">
        <label className="space-y-1">
          <span className="text-zinc-400">Name</span>
          <input className={input} value={form.name} onChange={set("name")} required maxLength={80} />
        </label>
        <label className="space-y-1">
          <span className="text-zinc-400">Budget (USD)</span>
          <input className={input} type="number" min="1" step="any" value={form.capital_budget_usd} onChange={set("capital_budget_usd")} required />
        </label>
        <label className="space-y-1 md:col-span-2">
          <span className="text-zinc-400">Instruments (comma separated; spot until L3)</span>
          <input className={input} value={form.instruments} onChange={set("instruments")} required />
        </label>
        <label className="space-y-1">
          <span className="text-zinc-400">Max position (% of budget, 1–25)</span>
          <input className={input} type="number" min="1" max="25" step="0.1" value={form.max_position_pct} onChange={set("max_position_pct")} required />
        </label>
        <label className="space-y-1">
          <span className="text-zinc-400">Hard stop (% drawdown of budget, ≤ 20)</span>
          <input className={input} type="number" min="0.1" max="20" step="0.1" value={form.max_drawdown_pct} onChange={set("max_drawdown_pct")} required />
        </label>
        <label className="space-y-1">
          <span className="text-zinc-400">Active hours UTC (HH:MM-HH:MM; empty = L2 timeouts mean no)</span>
          <input className={input} value={form.active_hours_utc} onChange={set("active_hours_utc")} placeholder="08:00-22:00" pattern="\d{2}:\d{2}-\d{2}:\d{2}" />
        </label>
        <label className="space-y-1">
          <span className="text-zinc-400">L2 veto window (seconds)</span>
          <input className={input} type="number" min="60" max="3600" value={form.veto_window_s} onChange={set("veto_window_s")} />
        </label>
        <fieldset className="space-y-1 md:col-span-2" disabled={!current || current.autonomy_level < 3}>
          <legend className="text-zinc-400">Perpetuals (O1: only after the mandate earned L3, max 5×)</legend>
          <label className="mr-4 inline-flex items-center gap-1.5">
            <input type="checkbox" checked={form.allow_perps} onChange={set("allow_perps")} /> allow perps
          </label>
          <label className="inline-flex items-center gap-1.5">
            leverage <input className="w-14 rounded border border-zinc-700 bg-zinc-900 px-1" type="number" min="1" max="5" value={form.max_leverage} onChange={set("max_leverage")} />
          </label>
        </fieldset>

        {needsTotp && (
          <div className="rounded border border-amber-700 bg-amber-950/20 p-2 md:col-span-2" data-testid="mandate-totp">
            <p className="mb-1 text-amber-200">Adds risk ({reasons.join(", ")}): enter your TOTP code.</p>
            <label className="inline-flex items-center gap-2">
              <span>TOTP</span>
              <input
                aria-label="TOTP code"
                inputMode="numeric"
                autoComplete="one-time-code"
                maxLength={6}
                value={totp}
                onChange={(e) => setTotp(e.target.value.replace(/\D/g, ""))}
                className="w-24 rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-zinc-100"
              />
            </label>
          </div>
        )}
        {error && (
          <p role="alert" className="text-red-300 md:col-span-2">
            {error}
          </p>
        )}
        <div className="flex flex-wrap gap-2 md:col-span-2">
          <button
            type="submit"
            disabled={busy || (needsTotp && totp.length !== 6)}
            className="rounded bg-cyan-600 px-3 py-1.5 text-[11px] font-bold text-zinc-950 disabled:opacity-50"
          >
            {current ? "Save mandate" : "Create mandate (starts at L1)"}
          </button>
          {current && current.autonomy_level > 1 && (
            <button
              type="button"
              disabled={busy}
              onClick={async () => {
                try {
                  await demoteMandate(current.id);
                  dispatchMandateUpdated({ mandateId: current.id, change: "demoted" });
                  onSaved();
                } catch (err: any) {
                  setError(err?.message || "Demotion failed");
                }
              }}
              className="rounded border border-zinc-700 px-3 py-1.5 text-[11px] font-bold text-zinc-300"
            >
              Demote one level
            </button>
          )}
        </div>
      </form>
    </section>
  );
}
