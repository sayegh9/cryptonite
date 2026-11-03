import React, { useEffect, useState } from 'react';
import { AlertTriangle, Database, ShieldCheck } from 'lucide-react';
import {
  fetchDebateEvidence,
  type DebateEvidence,
  type EvidenceItem,
  type RelatedPosition,
} from '@/lib/debateEvidence';

interface Props {
  runId: string;
  /** The run's status: the panel reads the evidence again when it changes (the verdict lands at the end). */
  status?: string;
}

const STATUS_CLASS: Record<string, string> = {
  OK: 'text-emerald-300 border-emerald-500/30 bg-emerald-500/10',
  STALE: 'text-amber-300 border-amber-500/30 bg-amber-500/10',
  MISSING: 'text-amber-300 border-amber-500/30 bg-amber-500/10',
  ERROR: 'text-rose-300 border-rose-500/30 bg-rose-500/10',
};

const ITEM_LABELS: [string, string][] = [
  ['quote', 'Quote'],
  ['mark', 'Perp mark'],
  ['bars', 'Bars'],
  ['funding', 'Funding'],
  ['open_interest', 'Open interest'],
  ['profile', 'Profile'],
  ['scanner', 'Scanner setups'],
  ['news', 'News'],
];

function num(v: unknown, digits = 8): string {
  return typeof v === 'number' && Number.isFinite(v) ? String(Number(v.toPrecision(digits))) : 'unknown';
}

function itemValue(key: string, it: EvidenceItem): string {
  const v = it.value as Record<string, unknown> | number | unknown[] | null;
  if (it.status !== 'OK' && it.status !== 'STALE') return it.reason || 'not usable';
  switch (key) {
    case 'quote':
    case 'mark':
      return `${num(v)} USD`;
    case 'funding': {
      const f = (v || {}) as Record<string, unknown>;
      return `${num(f.rate_pct, 6)}% per 8h (raw ${num(f.rate_raw, 6)})`;
    }
    case 'open_interest':
      return `${num(v)} ${it.unit ?? ''}`;
    case 'bars': {
      const b = (v || {}) as Record<string, unknown>;
      const s = (b.summary || null) as Record<string, unknown> | null;
      const head = `${String(b.timeframe)} ${String(b.returned)}/${String(b.expected)} closed`;
      return s ? `${head} · net ${num(s.net_change_pct, 4)}% · high ${num(s.window_high)} · low ${num(s.window_low)}` : head;
    }
    case 'profile': {
      const p = (v || null) as Record<string, unknown> | null;
      return p ? `${String(p.name)} · ${String(p.status)} · ${String(p.direction)}` : it.reason || 'none';
    }
    case 'scanner':
    case 'news':
      return `${Array.isArray(v) ? v.length : 0} ${key === 'news' ? 'headlines' : 'fresh'}`;
    default:
      return String(v);
  }
}

function positionLine(p: RelatedPosition): string {
  const market = p.market === 'PERP' ? 'perp' : p.market === 'SPOT_MARGIN' ? 'margin' : 'spot';
  const lev = p.leverage && p.leverage !== 1 ? ` ${p.leverage}x` : '';
  return `${p.instrument_id.split(/[-.]/)[0]} ${market} ${p.side}${lev} · ${num(p.quantity)} @ ${num(p.entry_price)} · stop ${num(p.stop)} · TP ${num(p.take_profit)}${p.mandate_name ? ` · ${p.mandate_name}` : ''}`;
}

/** H10: the facts the debate's roles were given, computed by code, shown apart from the agents' arguments. */
export const VerifiedFactsPanel: React.FC<Props> = ({ runId, status }) => {
  const [ev, setEv] = useState<DebateEvidence | null | undefined>(undefined);

  useEffect(() => {
    const ctrl = new AbortController();
    fetchDebateEvidence(runId, ctrl.signal)
      .then(setEv)
      .catch(() => setEv(null));
    return () => ctrl.abort();
  }, [runId, status]);

  if (ev === undefined) return null;
  if (ev === null) {
    return (
      <section data-testid="verified-facts" className="p-2 rounded border border-slate-700 text-2xs text-slate-400 font-mono">
        Verified facts could not be loaded.
      </section>
    );
  }
  if (ev.evidence_status === 'NO_SNAPSHOT' || !ev.snapshot) {
    return (
      <section data-testid="verified-facts" className="p-2 rounded border border-slate-700 text-2xs text-slate-400 font-mono">
        <span data-testid="no-snapshot">No snapshot</span>: this run predates evidence snapshots, so its facts were not recorded.
      </section>
    );
  }
  const snap = ev.snapshot;
  const positions = (snap.items.positions?.value as RelatedPosition[] | null) ?? null;
  const warn = ev.evidence_status !== 'OK';
  const blocking = ev.validation.filter((f) => f.severity === 'BLOCK');
  const warnings = ev.validation.filter((f) => f.severity === 'WARN');

  return (
    <section
      data-testid="verified-facts"
      aria-label="Verified facts"
      className="rounded border border-sky-500/40 bg-sky-950/30 p-3 text-2xs font-sans text-slate-200 space-y-2"
    >
      <header className="flex items-center justify-between gap-2">
        <span className="flex items-center gap-1.5 font-bold uppercase tracking-wide text-sky-300">
          <Database size={13} aria-hidden="true" /> Verified facts · snapshot v{snap.version}
        </span>
        <span className="text-slate-400 font-mono">from code, {snap.built_at}</span>
      </header>

      {ev.verdict.action_label && (
        <div
          data-testid="verdict-action"
          className={`flex items-center gap-1.5 rounded px-2 py-1 border font-semibold ${
            warn ? 'border-amber-500/40 bg-amber-500/10 text-amber-200' : 'border-slate-600 bg-slate-900 text-slate-100'
          }`}
        >
          <ShieldCheck size={13} aria-hidden="true" />
          {warn ? 'Non-actionable · ' : ''}
          {ev.verdict.action_label}
          {ev.approval ? ` · approval ${ev.approval.status}` : ''}
          {ev.routing && !ev.approval ? ` · ${ev.routing.outcome}${ev.routing.reason ? ` (${ev.routing.reason})` : ''}` : ''}
        </div>
      )}

      {warn && (
        <div data-testid="evidence-warning" role="alert" className="rounded border border-amber-500/40 bg-amber-500/10 p-2 text-amber-100 space-y-1">
          <div className="flex items-center gap-1.5 font-bold">
            <AlertTriangle size={13} aria-hidden="true" /> Evidence {ev.evidence_status === 'DEGRADED' ? 'degraded' : 'non-actionable'}
          </div>
          {ev.non_actionable_reason && <p className="break-words">{ev.non_actionable_reason}</p>}
          {[...blocking, ...warnings].map((f, i) => (
            <p key={i} className="break-words">
              {f.severity === 'BLOCK' ? 'Blocks' : 'Warning'}: {f.role} · {f.code} · {f.detail}
            </p>
          ))}
        </div>
      )}

      <div data-testid="related-positions">
        <div className="font-semibold text-slate-300">Related positions</div>
        {positions === null ? (
          <p className="text-rose-300">Unresolved: {snap.items.positions?.reason || 'position state could not be read'}</p>
        ) : positions.length === 0 ? (
          <p className="text-slate-400">None open on this coin.</p>
        ) : (
          <ul className="list-disc pl-4">
            {positions.map((p, i) => (
              <li key={i} className="font-mono break-words">{positionLine(p)}</li>
            ))}
          </ul>
        )}
      </div>

      {snap.purpose === 'ENTRY' && (
        <dl className="grid grid-cols-[auto_1fr_auto] gap-x-2 gap-y-0.5 items-baseline">
          {ITEM_LABELS.filter(([k]) => snap.items[k]).map(([key, label]) => {
            const it = snap.items[key];
            return (
              <React.Fragment key={key}>
                <dt className="text-slate-400">{label}</dt>
                <dd className="font-mono break-words min-w-0">{itemValue(key, it)}</dd>
                <dd>
                  <span data-testid={`evidence-status-${key}`} className={`px-1 rounded border font-mono ${STATUS_CLASS[it.status] ?? ''}`}>
                    {it.status}
                  </span>
                </dd>
              </React.Fragment>
            );
          })}
        </dl>
      )}
    </section>
  );
};
