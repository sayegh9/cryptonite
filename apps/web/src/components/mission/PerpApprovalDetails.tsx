"use client";

import { metricOrNull } from "@/lib/freshness";

/** The perp entry fields a Brain perp card shows, from an approval's payload (a debate's perp entry, auto-trade Phase B):
 * side, the setup grade and its reasons, the leverage the dry run allows and its binding ceiling, the liquidation price
 * and the loss at the stop. The dry run ran when the debate proposed the entry; the orchestrator sizes it again. */
export function PerpApprovalDetails({ payload }: { payload: Record<string, unknown> | null | undefined }) {
  const p = payload ?? {};
  const plan = (p.perp_plan ?? null) as Record<string, unknown> | null;
  if (!plan || typeof plan !== "object") return null;
  const grade = (p.grade ?? {}) as { grade?: string; reasons?: unknown[] };
  const reasons = (Array.isArray(grade.reasons) ? grade.reasons : Array.isArray(p.grade_reasons) ? p.grade_reasons : []).map(String);
  const side = typeof p.perp_side === "string" ? p.perp_side : plan.side === "BUY" ? "LONG" : "SHORT";
  const leverage = metricOrNull(plan.leverage);
  const liq = metricOrNull(plan.liquidation_price);
  const loss = metricOrNull(plan.loss_at_stop_usd);
  const lossPct = metricOrNull(plan.loss_at_stop_pct);
  const usd = (v: number) => `$${v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  return (
    <dl data-testid="perp-approval" className="mt-1 grid grid-cols-[auto_1fr] gap-x-2 gap-y-0.5 text-2xs">
      <dt className="text-zinc-500">Side</dt>
      <dd data-testid="perp-side" className={side === "LONG" ? "font-bold text-emerald-300" : "font-bold text-red-300"}>
        {side} · ISOLATED
      </dd>
      <dt className="text-zinc-500">Grade</dt>
      <dd data-testid="perp-grade" className="text-zinc-200">
        {String(p.setup_grade ?? grade.grade ?? "ungraded")}
        {reasons.length > 0 && <span className="text-zinc-400">: {reasons.slice(0, 6).join("; ")}</span>}
      </dd>
      <dt className="text-zinc-500">Leverage</dt>
      <dd data-testid="perp-leverage" className={leverage !== null && leverage >= 8 ? "font-bold text-red-300" : "text-zinc-200"}>
        {leverage !== null ? `${leverage}x` : "unknown"} (binding: {String(plan.binding_ceiling ?? "unknown")})
      </dd>
      <dt className="text-zinc-500">Liquidation</dt>
      <dd data-testid="perp-liquidation" className="text-zinc-200">
        {liq !== null ? liq.toLocaleString("en-US", { maximumFractionDigits: 2 }) : "none"}
      </dd>
      <dt className="text-zinc-500">Loss at stop</dt>
      <dd data-testid="perp-loss" className="text-zinc-200">
        {loss !== null ? usd(loss) : "unknown"}
        {lossPct !== null && ` (${lossPct.toFixed(2)}% of budget)`}
      </dd>
      <dd className="col-span-2 text-zinc-500">
        Dry run when the debate proposed it; the orchestrator sizes it again at execution.
      </dd>
    </dl>
  );
}
