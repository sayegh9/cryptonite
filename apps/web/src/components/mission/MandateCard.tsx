"use client";

import { useState } from "react";
import { toast } from "sonner";
import { pauseMandate, resumeMandate } from "@/lib/mission/client";
import { dispatchMandateUpdated } from "@/lib/mission/events";
import type { Mandate } from "@/lib/mission/types";
import { staleMarkText } from "@/lib/mission/marks";

const usd = (v: string | number) => `$${Number(v).toLocaleString(undefined, { maximumFractionDigits: 0 })}`;

function Meter({ label, value, max, text, tone }: { label: string; value: number; max: number; text: string; tone: string }) {
  const pct = max > 0 ? Math.min(100, Math.max(0, (value / max) * 100)) : 0;
  return (
    <div>
      <div className="flex justify-between text-2xs text-zinc-500">
        <span>{label}</span>
        <span className="text-zinc-300">{text}</span>
      </div>
      <div
        role="meter"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={max}
        aria-valuenow={Number(value.toFixed(2))}
        aria-valuetext={text}
        className="mt-0.5 h-1.5 w-full overflow-hidden rounded bg-zinc-800"
      >
        <div className={`h-full ${tone}`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

/**
 * Display tiers for the distance from the mark to the liquidation price (colour only; the exit monitor's own
 * thresholds, which act, are relative to the stop): under 5% liquidation colour, under 10% high, else low.
 */
function liqTone(distancePct: number): string {
  if (distancePct < 5) return "text-leverage-liquidation";
  if (distancePct < 10) return "text-leverage-high";
  return "text-leverage-low";
}

/** The mandate's perp positions: side, leverage, margin mode, notional and the distance to liquidation */
function PerpPositions({ mandate: m }: { mandate: Mandate }) {
  const perps = m.positions.filter((p) => p.instrument_id.split(".")[0].endsWith("-PERP"));
  if (perps.length === 0) return null;
  return (
    <div className="mt-2 rounded border border-zinc-800 p-2" data-testid="mandate-perps">
      <p className="mb-1 text-zinc-500">
        Perps · exposure {usd(m.perp_exposure_usd ?? "0")}
      </p>
      <ul className="space-y-0.5">
        {perps.map((p) => {
          const dist = p.liquidation_distance_pct != null ? Number(p.liquidation_distance_pct) : null;
          return (
            <li key={`${p.instrument_id}:${p.side}`} className="flex flex-wrap justify-between gap-x-2" data-testid="mandate-perp-position">
              <span className="text-zinc-200">
                {p.instrument_id.split(".")[0]} {p.side} {p.leverage ?? 1}x {p.margin_mode ?? ""} · {usd(p.notional_usd ?? "0")}
              </span>
              <span className={dist == null ? "text-zinc-500" : liqTone(dist)}>
                {p.liquidation_price
                  ? `liq ${Number(p.liquidation_price).toLocaleString(undefined, { maximumFractionDigits: 2 })}${dist != null ? ` · ${dist.toFixed(1)}% away` : ""}`
                  : "no liquidation price"}
              </span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

const STATUS_STYLE: Record<string, string> = {
  ACTIVE: "border-emerald-700 text-emerald-300",
  PAUSED: "border-zinc-600 text-zinc-300",
  HALTED: "border-red-700 bg-red-950/50 text-red-300",
  ARCHIVED: "border-zinc-700 text-zinc-500",
};

/** One mandate (§7.1): budget used, drawdown vs the hard stop, level, halted state with resume. */
export function MandateCard({ mandate: m, onChanged }: { mandate: Mandate; onChanged: () => void }) {
  const [totp, setTotp] = useState("");
  const [resuming, setResuming] = useState(false);
  const [busy, setBusy] = useState(false);
  const dd = Number(m.drawdown_pct);
  const hardStop = Number(m.max_drawdown_pct);
  const used = Number(m.budget_used_pct);
  const halted = m.status === "HALTED";

  const run = async (fn: () => Promise<unknown>, change: "paused" | "resumed", ok: string) => {
    setBusy(true);
    try {
      await fn();
      toast.success(ok);
      dispatchMandateUpdated({ mandateId: m.id, change });
      setResuming(false);
      setTotp("");
      onChanged();
    } catch (e: any) {
      toast.error(e?.message || "Request failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <article
      data-testid="mandate-card"
      aria-label={`Mandate ${m.name}`}
      className={`rounded border bg-zinc-950 p-3 font-mono text-2xs ${halted ? "border-red-800" : "border-zinc-800"}`}
    >
      <header className="mb-2 flex items-center justify-between gap-2">
        <div className="min-w-0">
          <h3 className="truncate text-xs font-bold text-zinc-100">{m.name}</h3>
          <p className="truncate text-2xs text-zinc-500">{m.instruments.join(", ")}</p>
        </div>
        <div className="flex flex-none items-center gap-1.5">
          <span className="rounded border border-cyan-800 px-1.5 py-0.5 text-2xs font-bold text-cyan-300" data-testid="mandate-level">
            {m.level_name}
          </span>
          <span className={`rounded border px-1.5 py-0.5 text-2xs font-bold ${STATUS_STYLE[m.status] || ""}`} data-testid="mandate-status">
            {m.status}
          </span>
        </div>
      </header>

      {halted && (
        <p className="mb-2 rounded border border-red-900 bg-red-950/40 px-2 py-1 text-red-200" data-testid="mandate-halted-reason">
          Halted: {m.halted_reason || "hard stop"}. Reduce-only until you resume it.
        </p>
      )}

      <div className="space-y-2">
        <Meter
          label={`Budget used — ${m.name}`}
          value={used}
          max={100}
          text={`${usd(m.exposure_usd)} of ${usd(m.capital_budget_usd)} (${used.toFixed(1)}%)`}
          tone="bg-cyan-600"
        />
        {m.marks_stale && (
          <p className="text-amber-400" data-testid="mandate-marks-stale">
            {staleMarkText(m.marks_age_s)}
          </p>
        )}
        <Meter
          label={`Drawdown vs hard stop — ${m.name}`}
          value={dd}
          max={hardStop}
          text={`${dd.toFixed(2)}% of the ${hardStop}% hard stop`}
          tone={dd >= hardStop / 2 ? "bg-red-500" : dd >= hardStop / 4 ? "bg-amber-500" : "bg-emerald-500"}
        />
      </div>

      <dl className="mt-2 grid grid-cols-3 gap-2 text-2xs">
        <div>
          <dt className="text-zinc-500">Equity</dt>
          <dd className="text-zinc-100">{usd(m.equity)}</dd>
        </div>
        <div>
          <dt className="text-zinc-500">High-water</dt>
          <dd className="text-zinc-100">{usd(m.hwm)}</dd>
        </div>
        <div>
          <dt className="text-zinc-500">Open / pending</dt>
          <dd className="text-zinc-100">
            {m.positions.length} pos · {m.pending_approvals} appr
          </dd>
        </div>
      </dl>
      <PerpPositions mandate={m} />
      {m.pending_proposal && (
        <p className="mt-2 text-2xs text-cyan-300">Promotion to L{m.pending_proposal.to_level} proposed: confirm in Autonomy &amp; Evals.</p>
      )}

      <div className="mt-3 flex flex-wrap gap-2">
        {m.status === "ACTIVE" && (
          <button
            type="button"
            disabled={busy}
            onClick={() => run(() => pauseMandate(m.id), "paused", `${m.name} paused`)}
            className="rounded border border-zinc-700 px-2 py-1 text-2xs font-bold text-zinc-300 hover:bg-zinc-900"
          >
            Pause (reduce-only)
          </button>
        )}
        {(halted || m.status === "PAUSED") && !resuming && (
          <button
            type="button"
            onClick={() => setResuming(true)}
            className="rounded border border-amber-700 bg-amber-900/20 px-2 py-1 text-2xs font-bold text-amber-200"
          >
            Resume… 🔐
          </button>
        )}
        {resuming && (
          <form
            className="flex items-center gap-2"
            aria-label={`Resume ${m.name}`}
            onSubmit={(e) => {
              e.preventDefault();
              void run(() => resumeMandate(m.id, totp), "resumed", `${m.name} resumed at ${m.level_name}`);
            }}
          >
            <label className="text-2xs text-zinc-400" htmlFor={`totp-${m.id}`}>
              TOTP
            </label>
            <input
              id={`totp-${m.id}`}
              inputMode="numeric"
              autoComplete="one-time-code"
              pattern="\d{6}"
              maxLength={6}
              value={totp}
              onChange={(e) => setTotp(e.target.value.replace(/\D/g, ""))}
              className="w-20 rounded border border-zinc-700 bg-zinc-900 px-1.5 py-0.5 text-zinc-100"
            />
            <button type="submit" disabled={busy || totp.length !== 6} className="rounded bg-amber-600 px-2 py-0.5 text-2xs font-bold text-zinc-950 disabled:opacity-50">
              Resume
            </button>
          </form>
        )}
      </div>
    </article>
  );
}
