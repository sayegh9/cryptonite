/** /api/v1/funnel: where each scanner setup stopped, in plain language (H13c). */

export type FunnelStage =
  | "IN_PROGRESS" | "UNACCOUNTED" | "NOT_QUEUED" | "GATE_REFUSED" | "DEBATE_FAILED" | "VERDICT_NOT_ROUTED"
  | "PROPOSAL_SKIPPED" | "ROUTED" | "PROPOSED" | "APPROVED" | "REJECTED" | "EXPIRED";

export interface FunnelRow {
  candidate_id: string;
  time: string;
  instrument_id: string;
  coin: string;
  timeframe: string;
  preset: string | null;
  direction: "LONG" | "SHORT" | null;
  entry: number | null;
  stop: number | null;
  target: number | null;
  stage: FunnelStage;
  reason: string | null;
  verdict: string | null;
  policy_a: string | null;
  policy_b: string | null;
  policy_c: string | null;
  what_if_r: number | null;
  what_if_state: "FINAL" | "PROVISIONAL" | "PENDING";
  what_if_exit: string | null;
}

export interface FunnelCounts {
  evaluations: Record<string, number>;
  candidates: number;
  gate_passed: number;
  gate_refused: number;
  verdicts: Record<string, number>;
  proposals: Record<"PENDING" | "APPROVED" | "REJECTED" | "EXPIRED", number>;
  approved: number;
  unaccounted: number;
}

export interface Funnel {
  now: string;
  hours: number;
  counts: FunnelCounts;
  rows: FunnelRow[];
  rows_truncated: boolean;
  caveats: string[];
}

export const STAGE_TEXT: Record<FunnelStage, string> = {
  IN_PROGRESS: "Still moving through the pipeline",
  UNACCOUNTED: "Unaccounted: nothing records where it stopped",
  NOT_QUEUED: "Never reached a debate",
  GATE_REFUSED: "Stopped by the safety gate before a debate",
  DEBATE_FAILED: "The debate failed",
  VERDICT_NOT_ROUTED: "The debate decided, but no order was routed",
  PROPOSAL_SKIPPED: "The debate said HOLD and no card was made",
  ROUTED: "Sent to approval or execution",
  PROPOSED: "Card waiting for your decision",
  APPROVED: "You approved the card",
  REJECTED: "You rejected the card",
  EXPIRED: "The card expired unanswered",
};

const REASON_TEXT: Record<string, string> = {
  BAR_NOT_FRESH: "the bar had already closed too long ago to debate",
  BEFORE_TRACKING: "from before setup cards existed",
  DEBATE_STILL_RUNNING: "the debate started and has not finished",
  HOLD_WITHOUT_RECORD: "the debate said HOLD, with neither a card nor a recorded reason",
  QUEUED_NO_DEBATE: "queued for a debate that never started",
  NO_PROFILE: "no profile holds this coin",
  MIN_CONVICTION_UNMEASURED: "the profile sets a minimum conviction, which a HOLD cannot measure",
  NO_DIRECTION: "the setup has no direction",
  NO_STRUCTURE: "the setup has no usable stop and target",
  RR_BELOW_FLOOR: "reward to risk is below the floor",
  OPPOSITE_POSITION_OPEN: "a position on the other side is open",
  NO_PERP_MARK: "no perp mark price was available",
  NO_PRICE_TICK: "the instrument has no price increment",
  NO_PRICE: "no fresh price was available",
  NOT_A_HOLD: "the debate did not end as a plain HOLD",
  NOT_A_SCANNER_DEBATE: "not a scanner debate",
  POLICY_A_MISSING: "the rules policy has no decision on it",
  COMPLETED: "sent to execution",
  PENDING_APPROVAL: "waiting for approval",
};

/** A code from the pipeline in plain words. Text that is already a sentence, and any unknown code, is still shown. */
export function reasonText(reason: string | null): string | null {
  if (!reason) return null;
  const [code, ...rest] = reason.split(": ");
  const detail = rest.join(": ");
  if (code === "NON_ACTIONABLE") return `the debate's result could not be acted on (${reasonText(detail) ?? "no detail"})`;
  if (code === "MARKET_NOT_PROPOSABLE") return `a card cannot trade this market (${detail})`;
  if (code in REASON_TEXT) return detail ? `${REASON_TEXT[code]} (${detail})` : REASON_TEXT[code];
  if (code.startsWith("POLICY_A_")) return `the rules policy skipped it (${humanize(code.slice(9))})`;
  return /^[A-Z0-9_]+$/.test(reason) ? humanize(reason) : reason;
}

const humanize = (code: string) => code.replace(/_/g, " ").toLowerCase();

const EVAL_TEXT: Record<string, string> = {
  NO_TRIGGER: "no trigger",
  REJECTED_RR: "reward to risk too low",
  STALE: "stale",
  MATCHED_FRESH: "matched",
};

const n = (v: number) => v.toLocaleString("en-US");

export function countsLine(c: FunnelCounts, hours: number): string {
  const evals = Object.entries(c.evaluations).map(([k, v]) => `${EVAL_TEXT[k] ?? humanize(k)} ${n(v)}`).join(", ");
  const verdicts = Object.entries(c.verdicts).map(([k, v]) => `${k} ${n(v)}`).join(", ") || "none";
  const p = c.proposals;
  return `Last ${hours}h: evaluations (${evals}) · ${n(c.candidates)} setups · gate passed ${n(c.gate_passed)}, ` +
    `refused ${n(c.gate_refused)} · verdicts ${verdicts} · cards waiting ${n(p.PENDING)}, approved ${n(c.approved)}, ` +
    `rejected ${n(p.REJECTED)}, expired ${n(p.EXPIRED)} · unaccounted ${n(c.unaccounted)}`;
}

export function whatIfText(row: FunnelRow): string {
  if (row.what_if_r === null) return "not scored yet";
  const r = `${row.what_if_r >= 0 ? "+" : ""}${row.what_if_r.toFixed(2)}R`;
  if (row.what_if_state === "FINAL") return `${r} final`;
  return `${r} provisional${row.what_if_exit === "OPEN" ? ", still open" : ""}`;
}
