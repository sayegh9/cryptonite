"use client";

import { useEffect, useRef, useState } from "react";
import { CheckCircle2, CircleDot, Cpu, Loader2, ScrollText, ShieldAlert, Wrench, X } from "lucide-react";
import { fetchDecisionTrace } from "@/lib/brain/client";
import type { DecisionTrace, DecisionTraceRow, OrderCardWidget } from "@/lib/brain/types";
import { useBrainStore } from "@/lib/stores/useBrainStore";

function Step({ icon, title, meta, children }: { icon: React.ReactNode; title: string; meta?: string; children?: React.ReactNode }) {
  return (
    <li className="relative border-l border-zinc-800 pb-4 pl-5 last:pb-0">
      <span className="absolute -left-[7px] top-0.5 flex h-3.5 w-3.5 items-center justify-center rounded-full bg-zinc-950">{icon}</span>
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-2xs font-bold text-zinc-200">{title}</span>
        {meta && <span className="text-2xs text-zinc-500">{meta}</span>}
      </div>
      {children && <div className="mt-1 text-2xs text-zinc-400">{children}</div>}
    </li>
  );
}

function callMeta(row: DecisionTraceRow): string {
  return [row.model, `$${Number(row.cost_usd).toFixed(4)}`, row.latency_ms != null ? `${(row.latency_ms / 1000).toFixed(1)} s` : null]
    .filter(Boolean)
    .join(" · ");
}

function outputText(output: unknown): string {
  if (output == null) return "";
  const text = typeof output === "string" ? output : JSON.stringify(output, null, 1);
  return text.length > 600 ? `${text.slice(0, 600)}…` : text;
}

/** Decision Trace drawer (blueprint §7.1): trigger → tool calls → model output → dispatcher result. */
export function DecisionTraceDrawer() {
  const traceId = useBrainStore((s) => s.traceDrawerId);
  const close = useBrainStore((s) => s.closeTrace);
  const messages = useBrainStore((s) => s.messages);
  const [data, setData] = useState<DecisionTrace | null>(null);
  const [error, setError] = useState<string | null>(null);
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!traceId) return;
    let cancelled = false;
    setData(null);
    setError(null);
    fetchDecisionTrace(traceId)
      .then((d) => !cancelled && setData(d))
      .catch((e) => !cancelled && setError(e.message));
    closeRef.current?.focus();
    return () => {
      cancelled = true;
    };
  }, [traceId]);

  useEffect(() => {
    if (!traceId) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && close();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [traceId, close]);

  if (!traceId) return null;

  const cards: OrderCardWidget[] = messages.flatMap((m) =>
    m.widgets.filter((w): w is OrderCardWidget => w.kind === "order_card" && w.trace_id === traceId),
  );
  const trace = data?.trace;

  return (
    <div className="fixed inset-0 z-[60] flex justify-end bg-black/50" onClick={close}>
      <aside
        role="dialog"
        aria-modal="true"
        aria-label="Decision trace"
        data-testid="decision-trace-drawer"
        onClick={(e) => e.stopPropagation()}
        className="flex h-full w-full max-w-md flex-col border-l border-zinc-800 bg-zinc-950 font-mono shadow-2xl"
      >
        <header className="flex items-center justify-between border-b border-zinc-800 px-4 py-3">
          <div className="flex items-center gap-2">
            <ScrollText className="h-4 w-4 text-cyan-400" />
            <h2 className="text-xs font-bold uppercase tracking-wider text-zinc-100">Decision trace</h2>
          </div>
          <button ref={closeRef} type="button" onClick={close} aria-label="Close decision trace" className="text-zinc-400 hover:text-zinc-100">
            <X className="h-4 w-4" />
          </button>
        </header>
        <div className="flex-1 overflow-y-auto p-4">
          <p className="mb-3 break-all text-2xs text-zinc-500">#{traceId}</p>
          {!data && !error && (
            <p role="status" className="flex items-center gap-1 text-2xs text-zinc-400">
              <Loader2 className="h-3 w-3 animate-spin" /> Loading trace…
            </p>
          )}
          {error && (
            <p role="alert" className="text-2xs text-rose-400">
              Trace unavailable: {error}
            </p>
          )}
          {trace && (
            <ol aria-label="Decision timeline">
              <Step icon={<CircleDot className="h-3 w-3 text-zinc-400" />} title="Trigger" meta={new Date(trace.created_at).toLocaleString()}>
                Operator message in session {trace.session_id ?? "—"}; role <span className="text-zinc-200">{trace.role}</span>
                {data?.parent ? ` (child of ${data.parent.role})` : ""}.
              </Step>
              {trace.tool_calls.map((c, i) => (
                <Step
                  key={i}
                  icon={c.status === "DENIED" ? <ShieldAlert className="h-3 w-3 text-amber-400" /> : <Wrench className="h-3 w-3 text-cyan-400" />}
                  title={`Tool · ${c.tool}`}
                  meta={`${c.risk ?? ""} ${c.status === "DENIED" ? "held for approval" : c.status}`.trim()}
                >
                  {c.args && Object.keys(c.args).length > 0 && (
                    <pre className="whitespace-pre-wrap break-all text-2xs text-zinc-500">{JSON.stringify(c.args)}</pre>
                  )}
                </Step>
              ))}
              <Step icon={<Cpu className="h-3 w-3 text-purple-400" />} title={`Model output · ${trace.status}`} meta={callMeta(trace)}>
                {trace.error ? <span className="text-rose-400">{trace.error}</span> : <pre className="whitespace-pre-wrap">{outputText(trace.output)}</pre>}
              </Step>
              {data?.children.map((c) => (
                <Step key={c.id} icon={<Cpu className="h-3 w-3 text-zinc-500" />} title={`Follow-up · ${c.role}`} meta={callMeta(c)} />
              ))}
              {cards.length > 0 ? (
                cards.map((card) => (
                  <Step
                    key={card.id}
                    icon={<CheckCircle2 className={`h-3 w-3 ${card.status === "APPROVED" ? "text-emerald-400" : "text-zinc-500"}`} />}
                    title="Dispatcher"
                    meta={card.status}
                  >
                    {card.status === "APPROVED"
                      ? `Paper command ${card.command_id} queued for GuardedOrderDispatcher.`
                      : card.status === "PENDING_APPROVAL"
                        ? "Waiting for your approval; nothing was sent."
                        : card.status === "REJECTED"
                          ? "You rejected the card; nothing was sent."
                          : "The paper order API refused the order."}
                  </Step>
                ))
              ) : (
                <Step icon={<CheckCircle2 className="h-3 w-3 text-zinc-600" />} title="Dispatcher" meta="no order">
                  This decision proposed no order.
                </Step>
              )}
            </ol>
          )}
        </div>
      </aside>
    </div>
  );
}
