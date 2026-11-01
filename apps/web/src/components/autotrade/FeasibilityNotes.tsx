import type { Feasibility } from "@/lib/autotrade/types";

const LABEL = { spot: "Spot", margin: "Margin", perp: "Perp" } as const;
const usd = (n: number) => `$${Math.round(n).toLocaleString("en-US")}`;

/** H3: an amber line per market whose entries the risk engine's minimum order would refuse. A warning, never a block. */
export function FeasibilityNotes({ feasibility, budget, riskPct }: { feasibility?: Feasibility; budget: string; riskPct: string }) {
  const rows = (Object.keys(LABEL) as (keyof typeof LABEL)[]).flatMap((m) => {
    const f = feasibility?.[m];
    return f && f.code !== "OK" ? [[m, f] as const] : [];
  });
  if (rows.length === 0) return null;
  const b = Number(budget);
  return (
    <div className="space-y-2" data-testid="feasibility-notes">
      {rows.map(([m, f]) => (
        <p key={m} role="alert" data-testid="feasibility-warning" className="rounded-lg border border-amber-800/60 bg-amber-950/20 p-3 text-xs text-amber-200">
          {LABEL[m]} entries will be refused:{" "}
          {f.code === "ORDER_MIN_UNREACHABLE"
            ? `at ${usd(b)} budget the position limit caps an order at ${usd(f.max_notional)}`
            : `at ${usd(b)} budget and ${riskPct}% risk an order sizes to about ${usd(f.risk_notional)}`}
          ; the minimum is {usd(f.min_notional)}.
          {f.suggested_budget_usd != null && ` Raise the budget to about ${usd(f.suggested_budget_usd)} or more.`}
        </p>
      ))}
    </div>
  );
}
