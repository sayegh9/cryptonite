"use client";

import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { decideApproval } from "@/lib/mission/client";
import { dispatchMandateUpdated } from "@/lib/mission/events";
import { dispatchBrainOpenTrace } from "@/lib/brain/events";
import type { Approval, Mandate } from "@/lib/mission/types";
import { ApprovalRiskContext, sideOf, type ApprovalRiskInput } from "@/components/ApprovalRiskContext";
import { metricOrNull } from "@/lib/freshness";
import { useMandateStore } from "@/lib/stores/useMandateStore";

/** Risk context inputs from an approval's command payload and its mandate (unknown -> null). */
function contextOf(a: Approval, mandates: Mandate[]): ApprovalRiskInput {
  const p = a.payload ?? {};
  const price = metricOrNull(p.price);
  const qty = metricOrNull(p.quantity);
  const stopPrice = metricOrNull(p.stop_price);
  const stopLossPct = metricOrNull(p.stop_loss_pct); // percent, e.g. 5 = 5%
  const m = mandates.find((x) => x.id === a.mandate_id);
  const budget = metricOrNull(m?.capital_budget_usd);
  const exposure = metricOrNull(m?.exposure_usd);
  return {
    symbol: typeof p.symbol === "string" ? p.symbol : typeof p.instrument_id === "string" ? p.instrument_id.split(".")[0] : null,
    side: sideOf(p.side ?? p.recommendation),
    decisionPrice: price,
    notionalUsd: price !== null && qty !== null ? price * qty : null,
    stopPct: stopLossPct !== null ? stopLossPct / 100 : stopPrice !== null && price ? Math.abs(price - stopPrice) / price : null,
    roomUsd: budget !== null && exposure !== null ? Math.max(0, budget - exposure) : null,
    requestedPct: metricOrNull(p.allocation_pct),
    similar: a.similar_decisions ?? null,
  };
}

const clock = (s: number) => `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;

const DONE_TEXT: Record<string, string> = {
  APPROVED: "Approved · command queued",
  VETOED: "Vetoed",
  EXPIRED: "Expired: timeout means no",
  EXPIRED_PROCEEDED: "Timed out: proceeding",
  CANCELLED: "Cancelled",
};

/**
 * Pending approvals with countdowns (§7.1, D8). ADD_RISK and REDUCE_RISK differ in colour and label.
 * Countdowns are announced to screen readers at 60 s and 10 s.
 */
export function ApprovalQueue({ approvals, onDecided }: { approvals: Approval[]; onDecided: () => void }) {
  const offset = useMandateStore((s) => s.clockOffsetMs);
  const markDecided = useMandateStore((s) => s.markDecided);
  const mandates = useMandateStore((s) => s.mandates);
  const [now, setNow] = useState(() => Date.now());
  const [busy, setBusy] = useState<string | null>(null);
  const [announcement, setAnnouncement] = useState("");
  // decisions made here stay visible after the refresh drops them from the pending list
  const [decided, setDecided] = useState<Record<string, Approval>>({});
  const announced = useRef(new Set<string>());

  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);

  const left = (a: Approval) => Math.max(0, Math.floor((new Date(a.expires_at).getTime() - (now + offset)) / 1000));

  useEffect(() => {
    for (const a of approvals) {
      if (a.status !== "PENDING") continue;
      const s = left(a);
      for (const mark of [60, 10]) {
        const key = `${a.id}:${mark}`;
        if (s <= mark && s > 0 && !announced.current.has(key)) {
          announced.current.add(key);
          setAnnouncement(`${a.mandate ?? "Mandate"}: ${a.action_class === "ADD_RISK" ? "add-risk" : "reduce-risk"} approval, ${s} seconds left`);
        }
      }
    }
  });

  const decide = async (a: Approval, decision: "APPROVE" | "VETO") => {
    setBusy(a.id);
    try {
      const res = await decideApproval(a.id, decision);
      markDecided(a.id, res.status);
      setDecided((d) => ({ ...d, [a.id]: { ...a, status: res.status, command_id: res.command_id } }));
      toast.success(`${decision === "APPROVE" ? "Approved" : "Vetoed"}: ${a.summary}`);
      dispatchMandateUpdated({ mandateId: a.mandate_id, change: "decided" });
      onDecided();
    } catch (e: any) {
      toast.error(e?.message || "Decision failed");
      onDecided();
    } finally {
      setBusy(null);
    }
  };

  const shown = [...approvals.map((a) => decided[a.id] ?? a), ...Object.values(decided).filter((d) => !approvals.some((a) => a.id === d.id))];

  return (
    <section aria-label="Pending approvals" className="rounded border border-zinc-800 bg-zinc-950 p-3 font-mono text-[11px]">
      <h3 className="mb-2 text-[10px] font-bold uppercase tracking-wider text-zinc-400">Approval queue</h3>
      <p aria-live="polite" className="sr-only" data-testid="approval-announcer">
        {announcement}
      </p>
      {shown.length === 0 && <p className="text-zinc-500">Nothing waiting for you.</p>}
      <ul className="space-y-2">
        {shown.map((a) => {
          const add = a.action_class === "ADD_RISK";
          const pending = a.status === "PENDING";
          const s = left(a);
          return (
            <li
              key={a.id}
              data-testid="approval-row"
              className={`rounded border p-2 ${add ? "border-amber-700 bg-amber-950/20" : "border-emerald-800 bg-emerald-950/20"}`}
            >
              <div className="flex items-center justify-between gap-2">
                <span className={`text-[10px] font-bold ${add ? "text-amber-300" : "text-emerald-300"}`}>
                  {add ? "ADDS RISK" : "REDUCES RISK"} · {a.mandate} · L{a.level}
                </span>
                {pending && (
                  <span role="timer" aria-label={`Time left for ${a.summary}`} className={`tabular-nums ${s <= 60 ? "text-red-300" : "text-zinc-300"}`}>
                    ⏱ {clock(s)}
                  </span>
                )}
              </div>
              <p className="mt-1 text-zinc-100">{a.summary}</p>
              <p className="text-[10px] text-zinc-500">
                At timeout: {a.timeout_policy === "DENY" ? "no (nothing happens)" : "proceeds"}
                {a.trace_id && (
                  <>
                    {" · "}
                    <button type="button" className="underline hover:text-zinc-300" onClick={() => dispatchBrainOpenTrace(a.trace_id!)}>
                      why? trace #{a.trace_id.slice(0, 8)}
                    </button>
                  </>
                )}
              </p>
              {pending && a.command_type === "SUBMIT_ORDER" && <ApprovalRiskContext {...contextOf(a, mandates)} />}
              {pending ? (
                <div className="mt-2 flex gap-2">
                  <button
                    type="button"
                    disabled={busy === a.id || s === 0}
                    onClick={() => decide(a, "APPROVE")}
                    aria-label={`Approve: ${a.summary}`}
                    className={`flex-1 rounded border px-2 py-1 text-[11px] font-bold disabled:opacity-50 ${
                      add ? "border-amber-600 bg-amber-600/20 text-amber-100" : "border-emerald-600 bg-emerald-600/20 text-emerald-100"
                    }`}
                  >
                    Approve
                  </button>
                  <button
                    type="button"
                    disabled={busy === a.id || s === 0}
                    onClick={() => decide(a, "VETO")}
                    aria-label={`Veto: ${a.summary}`}
                    className="flex-1 rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-[11px] font-bold text-zinc-300 disabled:opacity-50"
                  >
                    Veto
                  </button>
                </div>
              ) : (
                <p role="status" className="mt-2 text-[10px] font-bold text-zinc-300">
                  {DONE_TEXT[a.status] ?? a.status}
                  {a.status === "APPROVED" && a.command_id ? ` · ${a.command_id.slice(0, 8)}` : ""}
                </p>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
