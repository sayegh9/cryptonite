"use client";

import { useEffect } from "react";
import type { ShadowRoute, ShadowStats } from "@/lib/mission/types";
import { SHADOW_DAYS, useShadowStore } from "@/lib/stores/useShadowStore";

const pct = (v: number | null | undefined) => (v == null || !Number.isFinite(v) ? "—" : `${(v * 100).toFixed(1)}%`);
const ms = (v: number | null | undefined) => (v == null || !Number.isFinite(v) ? "—" : v >= 1000 ? `${(v / 1000).toFixed(1)} s` : `${Math.round(v)} ms`);
const usd = (v: string | null | undefined) => (v == null || !Number.isFinite(Number(v)) ? "—" : `$${Number(v).toFixed(4)}`);

function outcomeText(s: ShadowStats): string | null {
  const o = s.outcomes;
  if (!o || !o.labelled) return null;
  if (o.shadow_would_skip && o.debated) {
    const k = o.shadow_would_skip;
    return `${o.labelled} labelled (${o.horizon}): agreement ${pct(o.agreement_rate ?? null)}; shadow would have skipped ${k.n} (${k.wins} won, ${k.losses} lost)`;
  }
  if (o.primary && o.shadow) {
    return `${o.labelled} labelled (${o.horizon}): primary ${o.primary.wins}W/${o.primary.losses}L, shadow ${o.shadow.wins}W/${o.shadow.losses}L`;
  }
  return null;
}

function RouteConfig({ route }: { route: ShadowRoute }) {
  const rows: [string, string][] = [
    ["Primary", route.primary_model],
    ["Fallback", route.fallback_model ? `${route.fallback_model} (${route.fallback_policy})` : `none (${route.fallback_policy})`],
    ["Budget priority", route.priority],
    ["Shadow model", `${route.shadow_model}${route.shadow_paid ? " (paid)" : " (free)"}`],
    ["Sample rate", pct(route.shadow_sample_rate)],
    ["Updated", route.updated_at ? new Date(route.updated_at).toISOString().slice(0, 16).replace("T", " ") : "—"],
  ];
  return (
    <dl className="mt-1 grid grid-cols-[max-content_1fr] gap-x-3 gap-y-0.5 rounded border border-zinc-800 bg-zinc-900/40 p-2" data-testid={`shadow-config-${route.role}`}>
      {rows.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-zinc-500">{k}</dt>
          <dd className="break-all text-zinc-200">{v}</dd>
        </div>
      ))}
    </dl>
  );
}

/**
 * Shadow mode (F2, read-only): candidate models that answer a sample of a role's live calls after the
 * primary. Their answers are logged and never acted on. Routes are edited in SQL (ai_model_routes).
 */
export default function ShadowPanel() {
  const { role, days, openRoute, report, loading, error, setRole, setDays, setOpenRoute, load } = useShadowStore();

  useEffect(() => {
    void load();
  }, [role, days, load]);

  const roles = Array.from(new Set([...(report?.routes ?? []).map((r) => r.role), ...(report?.models ?? []).map((m) => m.role), ...(role ? [role] : [])])).sort();
  const routes = (report?.routes ?? []).filter((r) => !role || r.role === role);
  const models = report?.models ?? [];

  return (
    <section aria-label="Shadow models" className="rounded border border-zinc-800 bg-zinc-950 p-3 font-mono text-2xs text-zinc-300" data-testid="shadow-panel">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <h3 className="text-2xs font-bold uppercase tracking-wider text-zinc-400">Shadow models</h3>
        <label htmlFor="shadow-role" className="ml-auto text-zinc-500">Role</label>
        <select
          id="shadow-role"
          value={role ?? ""}
          onChange={(e) => setRole(e.target.value || null)}
          className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-zinc-100"
        >
          <option value="">All roles</option>
          {roles.map((r) => (
            <option key={r} value={r}>{r}</option>
          ))}
        </select>
        <label htmlFor="shadow-days" className="text-zinc-500">Window</label>
        <select
          id="shadow-days"
          value={days}
          onChange={(e) => setDays(Number(e.target.value))}
          className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-zinc-100"
        >
          {SHADOW_DAYS.map((d) => (
            <option key={d} value={d}>{d === 1 ? "24 h" : `${d} days`}</option>
          ))}
        </select>
      </div>

      {error && <p role="alert" className="text-amber-300">{error}</p>}

      {report && report.available !== false && (
        <>
          <h4 className="mb-1 text-zinc-400">Routes with a shadow</h4>
          {routes.length === 0 ? (
            <p className="text-zinc-500" data-testid="shadow-routes-empty">No route has a shadow model.</p>
          ) : (
            <ul className="mb-3 space-y-1" aria-label="Shadow routes">
              {routes.map((r) => (
                <li key={r.role} data-testid={`shadow-route-${r.role}`}>
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-bold text-zinc-100">{r.role}</span>
                    <span className="text-zinc-400">{r.shadow_model} at {pct(r.shadow_sample_rate)}</span>
                    {r.active ? (
                      <span className="rounded border border-emerald-800 px-1 text-emerald-300">active</span>
                    ) : (
                      <span className="rounded border border-zinc-700 px-1 text-zinc-400" title={r.shadow_paid && !report.allow_paid ? "Paid shadow models need AI_SHADOW_ALLOW_PAID" : "Sample rate is 0"}>
                        {r.shadow_paid && !report.allow_paid ? "inactive: paid not allowed" : "inactive"}
                      </span>
                    )}
                    <button
                      type="button"
                      aria-expanded={openRoute === r.role}
                      onClick={() => setOpenRoute(openRoute === r.role ? null : r.role)}
                      className="rounded border border-zinc-700 px-2 py-0.5 text-zinc-300 hover:bg-zinc-900"
                    >
                      {openRoute === r.role ? "Hide config" : "View config"}
                    </button>
                  </div>
                  {openRoute === r.role && <RouteConfig route={r} />}
                </li>
              ))}
            </ul>
          )}

          {models.length === 0 ? (
            <p className="text-zinc-500" data-testid="shadow-results-empty">No shadow results in this window.</p>
          ) : (
            <table className="w-full text-left tabular-nums" aria-label="Shadow results">
              <thead className="text-2xs text-zinc-500">
                <tr>
                  <th className="py-1 font-normal">Role</th>
                  <th className="py-1 font-normal">Shadow model</th>
                  <th className="py-1 font-normal">Calls</th>
                  <th className="py-1 font-normal">Agreement</th>
                  <th className="py-1 font-normal">Latency p50 / p95 (shadow vs primary)</th>
                  <th className="py-1 font-normal">Errors</th>
                  <th className="py-1 font-normal">Skipped</th>
                  <th className="py-1 font-normal">Cost</th>
                </tr>
              </thead>
              <tbody>
                {models.map((m) => {
                  const outcome = outcomeText(m);
                  return (
                    <tr key={`${m.role}|${m.shadow_model}`} className="border-t border-zinc-900 align-top" data-testid={`shadow-row-${m.role}`}>
                      <td className="py-1">{m.role}</td>
                      <td className="py-1 text-zinc-400">
                        {m.shadow_model}
                        {outcome && <div className="text-2xs text-zinc-500" data-testid={`shadow-outcome-${m.role}`}>{outcome}</div>}
                      </td>
                      <td className="py-1">{m.ok}</td>
                      <td className="py-1" title={m.key_field ? `on ${m.key_field}` : "free-text role: no key field"}>
                        {m.agreement.rate == null ? "—" : `${pct(m.agreement.rate)} (${m.agreement.agree}/${m.agreement.n})`}
                      </td>
                      <td className="py-1">
                        {ms(m.latency_ms.shadow_p50)} / {ms(m.latency_ms.shadow_p95)}
                        <span className="text-zinc-500"> vs {ms(m.latency_ms.primary_p50)} / {ms(m.latency_ms.primary_p95)}</span>
                      </td>
                      <td className={`py-1 ${m.errors ? "text-red-300" : ""}`}>{m.errors}</td>
                      <td className="py-1 text-zinc-400" title={Object.entries(m.skip_reasons).map(([k, v]) => `${k}: ${v}`).join(", ")}>{m.skipped}</td>
                      <td className="py-1">{usd(m.cost_usd.shadow)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </>
      )}
      {loading && !report && <p className="text-zinc-500">Loading…</p>}
      <p className="mt-2 text-2xs text-zinc-500">
        Shadow answers are logged only, never acted on. Routes are edited in SQL (ai_model_routes.shadow_model, shadow_sample_rate 0–1); paid shadow models also need AI_SHADOW_ALLOW_PAID.
      </p>
    </section>
  );
}
