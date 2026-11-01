"use client";

import { useEffect } from "react";
import { X } from "lucide-react";
import { dispatchBrainAsk } from "@/lib/brain/events";
import type { Setup } from "@/lib/stores/useSetupsStore";
import { formatAge, formatPrice, reasonText, symbolOf } from "./format";

/** The Brain prompt for a setup. It is only put in the composer: the operator sends it, and the Brain's own tools decide. */
export const askPrompt = (id: string) => `Explain setup ${id} and propose an order if it is eligible`;

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="space-y-1.5">
      <h3 className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">{title}</h3>
      {children}
    </section>
  );
}

export function SetupDrawer({ setup, onClose }: { setup: Setup; onClose: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const metrics = Object.entries(setup.brief.metrics);
  return (
    <aside
      role="dialog"
      aria-label="Setup details"
      data-testid="setup-drawer"
      className="absolute inset-y-0 right-0 z-20 flex w-full max-w-md flex-col gap-4 overflow-y-auto border-l border-border bg-background p-4 shadow-xl"
    >
      <header className="flex items-start justify-between gap-2">
        <div>
          <div className="text-base font-semibold">
            {symbolOf(setup.instrument_id)} <span className="text-sm font-normal text-muted-foreground">{setup.direction ?? "—"}</span>
          </div>
          <div className="font-mono text-[11px] text-muted-foreground">{setup.id}</div>
        </div>
        <button type="button" aria-label="Close setup details" onClick={onClose} className="rounded p-1 hover:bg-muted">
          <X className="size-4" aria-hidden />
        </button>
      </header>

      <button
        type="button"
        onClick={() => dispatchBrainAsk({ prompt: askPrompt(setup.id) })}
        className="h-8 rounded-lg bg-primary px-3 text-sm font-medium text-primary-foreground hover:bg-primary/80"
      >
        Ask the Brain
      </button>

      <Section title="Brief">
        <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-sm">
          <dt className="text-muted-foreground">Preset</dt>
          <dd>{setup.provenance.preset ?? "—"}</dd>
          <dt className="text-muted-foreground">Timeframe</dt>
          <dd>{setup.provenance.timeframe}</dd>
          <dt className="text-muted-foreground">Entry / stop / target</dt>
          <dd className="font-mono">
            {formatPrice(setup.entry)} / {formatPrice(setup.stop)} / {formatPrice(setup.target)}
          </dd>
          <dt className="text-muted-foreground">Age</dt>
          <dd>{formatAge(setup.age_seconds)}</dd>
          <dt className="text-muted-foreground">Cost</dt>
          <dd>
            {setup.cost.cost_r === null ? "—" : `${setup.cost.cost_r.toFixed(3)} R`}{" "}
            <span className="text-xs text-muted-foreground">({setup.cost.cost_model})</span>
          </dd>
          <dt className="text-muted-foreground">Scan run</dt>
          <dd className="truncate font-mono text-xs">{setup.provenance.scan_run_id}</dd>
        </dl>
        {setup.brief.matched_rules.length > 0 && (
          <p className="text-xs text-muted-foreground">Rules matched: {setup.brief.matched_rules.map(String).join(", ")}</p>
        )}
        {metrics.length > 0 && (
          <dl className="grid grid-cols-2 gap-x-3 gap-y-0.5 rounded border border-border p-2 text-xs">
            {metrics.map(([k, v]) => (
              <div key={k} className="contents">
                <dt className="truncate text-muted-foreground">{k}</dt>
                <dd className="truncate font-mono">{typeof v === "object" ? JSON.stringify(v) : String(v)}</dd>
              </div>
            ))}
          </dl>
        )}
      </Section>

      <Section title="Owner">
        {setup.profile ? (
          <p className="text-sm">
            {setup.profile.name} <span className="text-muted-foreground">({setup.profile.status.toLowerCase()})</span>
          </p>
        ) : null}
        <p className="text-sm" data-testid="setup-eligibility">
          {setup.eligibility.eligible ? "Eligible" : "Not eligible"}: {reasonText(setup.eligibility.reason)}
        </p>
      </Section>

      <Section title="Policy decisions">
        {setup.policies.length === 0 ? (
          <p className="text-sm text-muted-foreground">Not recorded yet</p>
        ) : (
          <ul className="space-y-1 text-sm">
            {setup.policies.map((p) => (
              <li key={p.policy}>
                <span className="font-medium">Policy {p.policy}</span>: {p.decision}
                {p.reason ? <span className="text-muted-foreground"> — {p.reason}</span> : null}
              </li>
            ))}
          </ul>
        )}
      </Section>

      <Section title="Funnel trail">
        {setup.funnel.trail.length === 0 ? (
          <p className="text-sm text-muted-foreground">No decisions recorded for this candidate</p>
        ) : (
          <ol className="space-y-1 text-sm" data-testid="funnel-trail">
            {setup.funnel.trail.map((d) => (
              <li key={d.id}>
                <span className="font-medium">{d.stage}</span> {d.outcome}
                {d.reason ? <span className="text-muted-foreground"> — {d.reason}</span> : null}
              </li>
            ))}
          </ol>
        )}
      </Section>
    </aside>
  );
}
