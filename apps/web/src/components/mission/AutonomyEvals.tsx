"use client";

import { useEffect, useState } from "react";
import { CheckCircle2, XCircle } from "lucide-react";
import { toast } from "sonner";
import { confirmPromotion, declinePromotion, evaluateAutonomy } from "@/lib/mission/client";
import { dispatchMandateUpdated } from "@/lib/mission/events";
import type { GateCheck, Mandate } from "@/lib/mission/types";
import { useAutonomyStore } from "@/lib/stores/useAutonomyStore";
import { dispatchSwitchWorkspace } from "@/lib/navigation";
import OutcomesByRegime from "./OutcomesByRegime";
import ReliabilityChart from "./ReliabilityChart";
import { fmtGateActual } from "@/lib/format";

const CHECK_LABEL: Record<string, string> = {
  days_at_level: "Days at this level",
  closed_trades: "Closed trades",
  honest_paper: "Honest paper fills",
  max_drawdown_pct: "Max drawdown",
  mandate_breaches: "Mandate breaches",
  sortino_vs_btc_hold: "Sortino vs BTC hold",
  calibration_error_pp: "Calibration error",
  veto_rate_pct: "Operator veto rate",
  expectancy_lower_bound: "Expectancy lower bound",
  sortino_diff_lower_bound: "Sortino − BTC hold, lower bound",
  calibration_ece_ci: "Calibration error interval",
};

const intervalText = (iv: GateCheck["interval"]) =>
  Array.isArray(iv) && iv.length === 2 ? `90% CI [${iv[0]}, ${iv[1]}]` : null;

/** Autonomy & Evals (§4.5, §7.1): earned vs requested level, the gate with live numbers, TOTP promotion. */
export function AutonomyEvals({ mandates }: { mandates: Mandate[] }) {
  const { mandateId, setMandateId, view, evals, calibrationNote, calibration, byRegime, error, load, setView, showEvents, setShowEvents } = useAutonomyStore();
  const [totp, setTotp] = useState("");
  const [busy, setBusy] = useState(false);
  const id = mandateId && mandates.some((m) => m.id === mandateId) ? mandateId : mandates[0]?.id ?? null;

  useEffect(() => {
    if (id) void load(id);
  }, [id, load]);

  const act = async (fn: () => Promise<any>, ok: string, promoted = false) => {
    if (!id) return;
    setBusy(true);
    try {
      setView(await fn());
      toast.success(ok);
      if (promoted) dispatchMandateUpdated({ mandateId: id, change: "promoted" });
      setTotp("");
    } catch (e: any) {
      toast.error(e?.message || "Request failed");
    } finally {
      setBusy(false);
    }
  };

  if (!mandates.length) {
    return (
      <div className="p-4 font-mono text-xs text-zinc-400 space-y-3">
        <p>No mandates yet. The autonomy gate and the shadow results are about a mandate, so create one first.</p>
        <button
          type="button"
          onClick={() => dispatchSwitchWorkspace({ mode: "mission", view: "mandates" })}
          className="rounded bg-cyan-600 px-3 py-1.5 text-2xs font-bold uppercase tracking-wider text-zinc-950 hover:bg-cyan-500"
        >
          Open the mandate editor
        </button>
      </div>
    );
  }

  return (
    <section aria-label="Autonomy and evals" className="space-y-3 font-mono text-2xs text-zinc-300">
      <div className="flex items-center gap-2">
        <label htmlFor="autonomy-mandate" className="text-2xs font-bold uppercase tracking-wider text-zinc-400">
          Mandate
        </label>
        <select
          id="autonomy-mandate"
          value={id ?? ""}
          onChange={(e) => setMandateId(e.target.value)}
          className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-zinc-100"
        >
          {mandates.map((m) => (
            <option key={m.id} value={m.id}>
              {m.name}
            </option>
          ))}
        </select>
        <button
          type="button"
          disabled={busy || !id}
          onClick={() => act(() => evaluateAutonomy(id!), "Gate evaluated")}
          className="rounded border border-zinc-700 px-2 py-1 text-2xs font-bold text-zinc-300 hover:bg-zinc-900"
        >
          Evaluate gate now
        </button>
      </div>
      {error && <p role="alert" className="text-red-300">{error}</p>}

      {view && (
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
          <div className="rounded border border-zinc-800 bg-zinc-950 p-3">
            <dl className="mb-3 grid grid-cols-2 gap-2">
              <div>
                <dt className="text-2xs text-zinc-500">Earned level</dt>
                <dd className="text-sm font-bold text-cyan-300" data-testid="autonomy-earned">{view.level_name}</dd>
              </div>
              <div>
                <dt className="text-2xs text-zinc-500">Requested (system proposal)</dt>
                <dd className="text-sm font-bold text-zinc-100" data-testid="autonomy-requested">
                  {view.requested_level ? `L${view.requested_level}` : "none"}
                </dd>
              </div>
            </dl>
            <h3 className="mb-1 text-2xs font-bold uppercase tracking-wider text-zinc-400">
              {view.gate.target_level ? `Gate to L${view.gate.target_level}` : "Top of the ladder"} ·{" "}
              <span className={view.gate.passed ? "text-emerald-300" : "text-zinc-400"}>{view.gate.passed ? "PASSED" : "not passed"}</span>
            </h3>
            <table className="w-full text-left" aria-label="Promotion gate checklist">
              <thead className="text-2xs text-zinc-500">
                <tr>
                  <th className="py-1 font-normal">Check</th>
                  <th className="py-1 font-normal">Required</th>
                  <th className="py-1 font-normal">Now</th>
                  <th className="py-1 font-normal">
                    <span className="sr-only">Result</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {view.gate.checks.map((c) => {
                  const iv = intervalText(c.interval);
                  return (
                    <tr
                      key={c.name}
                      className={`border-t border-zinc-900 ${c.informational ? "text-zinc-400" : ""}`}
                      data-testid={c.informational ? "gate-check-info" : "gate-check"}
                    >
                      <td className="py-1">
                        {CHECK_LABEL[c.name] ?? c.name}
                        {c.informational && (
                          <span
                            className="ml-1 rounded border border-sky-800 bg-sky-950/40 px-1 text-2xs text-sky-300"
                            title="Reported only: does not block promotion until the operator makes it required (D6)"
                          >
                            informational
                          </span>
                        )}
                      </td>
                      <td className="py-1 text-zinc-400">{c.required}</td>
                      <td className="py-1 tabular-nums">
                        {fmtGateActual(c.name, c.actual, view.gate.metrics)}
                        {iv && <div className="text-2xs text-zinc-500" data-testid="gate-check-interval">{iv}</div>}
                      </td>
                      <td className="py-1">
                        {c.passed ? (
                          <span className="inline-flex items-center gap-1 text-emerald-300"><CheckCircle2 className="h-3 w-3" aria-hidden="true" /> pass</span>
                        ) : c.informational ? (
                          <span className="inline-flex items-center gap-1 text-amber-300"><XCircle className="h-3 w-3" aria-hidden="true" /> below (not blocking)</span>
                        ) : (
                          <span className="inline-flex items-center gap-1 text-red-300"><XCircle className="h-3 w-3" aria-hidden="true" /> fail</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {view.gate.checks.some((c) => c.informational) && (
              <p className="mt-1 text-2xs text-zinc-500">Informational checks are shown with their 90% interval and do not affect PASSED.</p>
            )}

            {view.proposal && (
              <form
                className="mt-3 rounded border border-cyan-800 bg-cyan-950/20 p-2"
                aria-label="Confirm promotion"
                onSubmit={(e) => {
                  e.preventDefault();
                  void act(() => confirmPromotion(id!, view.proposal!.id, totp), `Promoted to L${view.proposal!.to_level}`, true);
                }}
              >
                <p className="mb-1 text-cyan-200">
                  The system proposes L{view.proposal.from_level} → L{view.proposal.to_level}. Nothing changes until you confirm with TOTP.
                </p>
                <div className="flex items-center gap-2">
                  <input
                    aria-label="TOTP code"
                    inputMode="numeric"
                    autoComplete="one-time-code"
                    maxLength={6}
                    value={totp}
                    onChange={(e) => setTotp(e.target.value.replace(/\D/g, ""))}
                    className="w-24 rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-zinc-100"
                  />
                  <button type="submit" disabled={busy || totp.length !== 6} className="rounded bg-cyan-600 px-2 py-1 text-2xs font-bold text-zinc-950 disabled:opacity-50">
                    Confirm promotion
                  </button>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => act(() => declinePromotion(id!, view.proposal!.id), "Proposal declined")}
                    className="rounded border border-zinc-700 px-2 py-1 text-2xs font-bold text-zinc-300"
                  >
                    Decline
                  </button>
                </div>
              </form>
            )}
          </div>

          <div className="space-y-3">
            <div className="rounded border border-zinc-800 bg-zinc-950 p-3">
              <h3 className="mb-1 text-2xs font-bold uppercase tracking-wider text-zinc-400">Calibration</h3>
              <p className="text-zinc-500" data-testid="calibration-note">{calibrationNote ?? "Not measured yet."}</p>
              {calibration &&
                Object.entries(calibration).map(([role, data]) => <ReliabilityChart key={role} role={role} data={data} />)}
            </div>
            <div className="rounded border border-zinc-800 bg-zinc-950 p-3">
              <OutcomesByRegime data={byRegime} />
            </div>
            <div className="rounded border border-zinc-800 bg-zinc-950 p-3">
              <h3 className="mb-1 text-2xs font-bold uppercase tracking-wider text-zinc-400">Evals (latest per set and model)</h3>
              {evals.length === 0 ? (
                <p className="text-zinc-500">No eval runs recorded.</p>
              ) : (
                <table className="w-full text-left" aria-label="Eval runs">
                  <tbody>
                    {evals.map((r) => (
                      <tr key={r.id} className="border-t border-zinc-900">
                        <td className="py-1">{r.set_version}</td>
                        <td className="py-1 text-zinc-400">{r.model}</td>
                        <td className="py-1">{r.passed === null ? "—" : r.passed ? "pass" : "fail"}</td>
                        <td className="py-1 text-zinc-500">{new Date(r.created_at).toISOString().slice(0, 10)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
            <div className="rounded border border-zinc-800 bg-zinc-950 p-3">
              <button
                type="button"
                aria-expanded={showEvents}
                onClick={() => setShowEvents(!showEvents)}
                className="mb-1 text-2xs font-bold uppercase tracking-wider text-zinc-400"
              >
                Autonomy events {showEvents ? "▾" : "▸"}
              </button>
              {showEvents && (
                <ul className="max-h-60 space-y-1 overflow-y-auto">
                  {view.events.map((e) => (
                    <li key={e.id} className="text-2xs">
                      <span className="text-zinc-500">{new Date(e.created_at).toISOString().slice(0, 16).replace("T", " ")}</span>{" "}
                      <span className="font-bold text-zinc-200">{e.kind}</span>
                      {e.from_level && e.to_level && e.from_level !== e.to_level ? ` L${e.from_level}→L${e.to_level}` : ""} · {e.reason} · {e.actor}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
