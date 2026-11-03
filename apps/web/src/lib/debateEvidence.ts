/**
 * H10: a debate run's evidence snapshot (GET /api/v1/agents/debates/{id}/evidence): the verified facts every role
 * saw, computed by code, the post-check's flags, the verdict's action and what routing and approval did with it.
 * A run from before the snapshot existed answers `evidence_status: "NO_SNAPSHOT"` with `snapshot: null`.
 */

export type EvidenceItemStatus = "OK" | "STALE" | "MISSING" | "ERROR";
export type EvidenceStatus = "OK" | "DEGRADED" | "NON_ACTIONABLE" | "NO_SNAPSHOT";

export interface EvidenceItem {
  status: EvidenceItemStatus;
  value: unknown;
  source: string;
  as_of: string | null;
  unit: string | null;
  reason?: string;
  age_s?: number | null;
}

export interface RelatedPosition {
  instrument_id: string;
  market: string;
  side: string;
  quantity: number | null;
  entry_price: number | null;
  leverage: number;
  stop: number | null;
  take_profit: number | null;
  mandate_name: string | null;
  opened_at: string | null;
}

export interface EvidenceSnapshot {
  schema_version: number;
  version: number;
  purpose: "ENTRY" | "EXIT";
  instrument_id: string;
  market: string;
  built_at: string;
  items: Record<string, EvidenceItem>;
  essential: string[];
  optional: string[];
}

export interface ValidationFlag {
  snapshot_version: number;
  role: string;
  code: string;
  fact: string;
  severity: "BLOCK" | "WARN";
  detail: string;
}

export interface DebateEvidence {
  run_id: string;
  instrument_id: string;
  status: string;
  trigger_source: string | null;
  evidence_status: EvidenceStatus;
  failure_reason: string | null;
  non_actionable_reason: string | null;
  verdict: {
    recommendation: string | null;
    conviction_level: string | null;
    conviction_score: number | null;
    allocation_pct: number | null;
    decision_price: number | null;
    action_label: string | null;
  };
  snapshot_version: number | null;
  snapshot_versions: { version: number; created_at: string; essential_ok: boolean }[];
  snapshot: EvidenceSnapshot | null;
  validation: ValidationFlag[];
  routing: { outcome: string; reason: string | null } | null;
  approval: { id: string; status: string; action_class: string } | null;
}

export const HOLD_ENTRY_LABEL = "Add no new position";

export async function fetchDebateEvidence(runId: string, signal?: AbortSignal): Promise<DebateEvidence | null> {
  const res = await fetch(`/api/v1/agents/debates/${encodeURIComponent(runId)}/evidence`, {
    credentials: "include",
    cache: "no-store",
    signal,
  });
  if (!res.ok) return null;
  const body = await res.json().catch(() => null);
  if (!body || typeof body !== "object" || typeof body.evidence_status !== "string") return null;
  return body as DebateEvidence;
}
