"use client";

import type { LeveragePlan, SetupGrade } from "@/lib/brain/types";
import { HIGH_LEVERAGE_CONFIRM, leverageTier, pct, pillsWith, TIER_CLASSES, usd } from "@/lib/leverage";

/** The liquidation-buffer ceiling: the stop may use at most half of the distance to liquidation (ADR 0009) */
const BUFFER_LIMIT_PCT = 50;

const GRADE_CLASSES: Record<SetupGrade, string> = {
  A: "border-emerald-700 text-emerald-300",
  B: "border-cyan-700 text-cyan-300",
  C: "border-amber-700 text-amber-300",
  NO_TRADE: "border-rose-700 text-rose-300",
};

/**
 * The order's prices on one axis: liquidation, stop, entry and target. The liquidation marker is drawn in the
 * liquidation colour so the gap between the stop and it (the buffer) is visible at a glance.
 */
function PriceLadder({ plan, target }: { plan: LeveragePlan; target: number | null }) {
  const entry = Number(plan.entry_price);
  const stop = plan.stop_price != null ? Number(plan.stop_price) : null;
  const liq = plan.liquidation_price != null ? Number(plan.liquidation_price) : null;
  const points = [entry, stop, liq, target].filter((v): v is number => v != null && Number.isFinite(v) && v > 0);
  if (points.length < 2 || !Number.isFinite(entry)) return null;
  const lo = Math.min(...points);
  const hi = Math.max(...points);
  const span = hi - lo || 1;
  const x = (v: number) => 4 + ((v - lo) / span) * 312;
  const marks: { v: number | null; label: string; cls: string }[] = [
    { v: liq, label: "liq", cls: "fill-leverage-liquidation stroke-leverage-liquidation" },
    { v: stop, label: "stop", cls: "fill-rose-400 stroke-rose-400" },
    { v: entry, label: "entry", cls: "fill-sky-400 stroke-sky-400" },
    { v: target, label: "target", cls: "fill-emerald-400 stroke-emerald-400" },
  ];
  return (
    <svg viewBox="0 0 320 30" className="mt-1.5 h-8 w-full" role="img" data-testid="perp-plan-ladder"
      aria-label={`Liquidation ${liq ?? "none"}, stop ${stop ?? "none"}, entry ${entry}, target ${target ?? "none"}`}>
      <line x1="4" y1="10" x2="316" y2="10" className="stroke-zinc-700" strokeWidth="1" />
      {liq != null && stop != null && (
        <line x1={x(Math.min(liq, stop))} y1="10" x2={x(Math.max(liq, stop))} y2="10" className="stroke-leverage-liquidation" strokeWidth="3" opacity="0.35" />
      )}
      {marks.map((m) => m.v != null && (
        <g key={m.label} className={m.cls}>
          <line x1={x(m.v)} y1="4" x2={x(m.v)} y2="16" strokeWidth="2" />
          <text x={Math.min(300, Math.max(2, x(m.v) - 8))} y="27" fontSize="8" stroke="none">{m.label}</text>
        </g>
      ))}
    </svg>
  );
}

/** `7x ISOLATED`, coloured by tier */
export function LeveragePill({ leverage, testId }: { leverage: number; testId?: string }) {
  return (
    <span
      data-testid={testId}
      className={`rounded border px-1.5 py-0.5 text-2xs font-black tracking-wider ${TIER_CLASSES[leverageTier(leverage)]}`}
    >
      {leverage}x ISOLATED
    </span>
  );
}

/**
 * A perp entry's plan as the server worked it out (leverage plan 4.3): margin, notional, liquidation price and the share
 * of its distance the stop uses, the loss at the stop and if liquidated, funding per 8 h, the system's setup grade, and
 * the leverage pills. A pill above the recommended leverage is disabled with the ceiling that stops it; picking one
 * asks the server for that plan again. Once the card is decided the pills are gone and the approved leverage stays.
 */
export function PerpPlanPanel({
  plan, grade, gradeReasons, picked, onPick, locked, lockedLeverage, target = null,
}: {
  plan: LeveragePlan | null;
  grade: SetupGrade | null | undefined;
  gradeReasons: string[];
  picked: number | null;
  onPick: (leverage: number | null) => void;
  /** The card is no longer pending: no pills */
  locked: boolean;
  lockedLeverage?: number | null;
  /** The card's take-profit, drawn on the price ladder */
  target?: number | null;
}) {
  const recommended = plan?.allowed_max_leverage ?? null;
  const buffer = plan?.buffer_used_pct != null ? Number(plan.buffer_used_pct) : null;
  const bufferTier = buffer == null ? null : buffer > BUFFER_LIMIT_PCT * 0.8 ? "high" : buffer > BUFFER_LIMIT_PCT * 0.5 ? "mid" : "low";
  const funding = plan?.funding_8h_usd != null ? Number(plan.funding_8h_usd) : null;
  return (
    <div className="mt-2 rounded border border-zinc-800 bg-zinc-950/70 p-2 text-2xs" data-testid="perp-plan">
      <div className="mb-1.5 flex flex-wrap items-center gap-2">
        {locked && lockedLeverage ? (
          <LeveragePill leverage={lockedLeverage} testId="perp-plan-pill" />
        ) : plan && plan.ok ? (
          <LeveragePill leverage={plan.leverage} testId="perp-plan-pill" />
        ) : (
          <span className="rounded border border-zinc-700 px-1.5 py-0.5 text-2xs font-black tracking-wider text-zinc-400">
            ISOLATED
          </span>
        )}
        {plan && plan.ok && !locked && (
          <span className="text-zinc-400" data-testid="perp-plan-label" title="The ceiling that set the leverage">
            {plan.leverage_label}
          </span>
        )}
        {grade && (
          <span
            data-testid="perp-plan-grade"
            className={`rounded border px-1.5 py-0.5 font-bold ${GRADE_CLASSES[grade]}`}
            title="The system's setup grade (never the model's): it caps the leverage"
          >
            Grade {grade === "NO_TRADE" ? "NO TRADE" : grade}
          </span>
        )}
      </div>

      {plan && !plan.ok && (
        <p role="status" className="mb-1.5 text-rose-300" data-testid="perp-plan-refused">
          No leverage fits: {plan.reject}
          {plan.binding_ceiling ? ` (${plan.binding_ceiling})` : ""}
        </p>
      )}

      {plan && plan.ok && (
        <>
          <dl className="grid grid-cols-3 gap-2">
            <div>
              <dt className="text-zinc-500">Margin</dt>
              <dd className="text-zinc-100" data-testid="perp-plan-margin">{usd(plan.margin_usd)}</dd>
            </div>
            <div>
              <dt className="text-zinc-500">Notional</dt>
              <dd className="text-zinc-100" data-testid="perp-plan-notional">{usd(plan.notional_usd)}</dd>
            </div>
            <div>
              <dt className="text-zinc-500">Liquidation</dt>
              <dd className="text-leverage-liquidation" data-testid="perp-plan-liq">
                {plan.liquidation_price ? Number(plan.liquidation_price).toLocaleString("en-US", { maximumFractionDigits: 2 }) : "none"}
                {plan.liquidation_distance_pct ? ` (${pct(plan.liquidation_distance_pct)} away)` : ""}
              </dd>
            </div>
            <div>
              <dt className="text-zinc-500">Loss at stop</dt>
              <dd className="text-rose-300" data-testid="perp-plan-loss-stop">
                {usd(plan.loss_at_stop_usd)}
                {plan.loss_at_stop_pct ? ` (${pct(plan.loss_at_stop_pct, 2)} of budget)` : ""}
              </dd>
            </div>
            <div>
              <dt className="text-zinc-500">Loss if liquidated</dt>
              <dd className="text-leverage-liquidation" data-testid="perp-plan-loss-liq">{usd(plan.loss_if_liquidated_usd)}</dd>
            </div>
            <div>
              <dt className="text-zinc-500">Funding / 8h</dt>
              <dd className={funding == null ? "text-zinc-500" : funding < 0 ? "text-rose-300" : "text-emerald-300"} data-testid="perp-plan-funding">
                {funding == null ? "unknown" : `${funding < 0 ? "pays" : "earns"} ${usd(Math.abs(funding), 3)}`}
              </dd>
            </div>
          </dl>
          <PriceLadder plan={plan} target={target} />
          {buffer != null && bufferTier && (
            <div className="mt-1.5" data-testid="perp-plan-buffer">
              <div className="mb-0.5 flex justify-between text-zinc-400">
                <span>Stop uses {pct(buffer, 0)} of the distance to liquidation</span>
                <span>limit {BUFFER_LIMIT_PCT}%</span>
              </div>
              <div className="relative h-1.5 w-full overflow-hidden rounded-full bg-zinc-800" aria-hidden>
                <div
                  className={`h-full ${bufferTier === "high" ? "bg-leverage-high" : bufferTier === "mid" ? "bg-leverage-mid" : "bg-leverage-low"}`}
                  style={{ width: `${Math.min(100, buffer).toFixed(1)}%` }}
                />
                <div className="absolute top-0 h-full w-px bg-leverage-liquidation" style={{ left: `${BUFFER_LIMIT_PCT}%` }} />
              </div>
            </div>
          )}
        </>
      )}

      {gradeReasons.length > 0 && (
        <ul className="mt-1.5 list-disc pl-4 text-zinc-400" data-testid="perp-plan-grade-reasons">
          {gradeReasons.slice(0, 6).map((r) => (
            <li key={r}>{r}</li>
          ))}
        </ul>
      )}

      {!locked && plan && (
        <div className="mt-2 flex flex-wrap items-center gap-1" role="group" aria-label="Leverage">
          {pillsWith(recommended).map((lev) => {
            const above = recommended != null && lev > recommended;
            const selected = plan.ok ? plan.leverage === lev : picked === lev;
            const reason = above
              ? `Above the ${plan.binding_ceiling ?? "plan"} ceiling (${recommended}x)`
              : lev >= HIGH_LEVERAGE_CONFIRM ? "Needs a second confirmation" : `Plan at ${lev}x`;
            return (
              <button
                key={lev}
                type="button"
                data-testid={`leverage-pill-${lev}`}
                disabled={above}
                aria-pressed={selected}
                title={reason}
                onClick={() => onPick(lev)}
                className={`rounded border px-1.5 py-0.5 font-bold disabled:cursor-not-allowed disabled:opacity-40 ${
                  selected ? TIER_CLASSES[leverageTier(lev)] : "border-zinc-700 text-zinc-300 hover:bg-zinc-800"
                }`}
              >
                {lev}x{lev === recommended ? " ★" : ""}
              </button>
            );
          })}
          {recommended != null && recommended > 0 && (
            <span className="ml-1 text-zinc-500">★ recommended: the highest the ceilings allow</span>
          )}
        </div>
      )}
    </div>
  );
}
