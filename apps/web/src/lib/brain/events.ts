/**
 * Typed cross-workspace events for the Brain (blueprint §7.1).
 *
 *   cryptonite:brain-ask         send context from any workspace into the Brain ("ask about this chart")
 *   cryptonite:brain-open-trace  open the Decision Trace drawer for a trace id
 */

export interface BrainAskDetail {
  prompt: string;
  /** true: send immediately; false/undefined: put it in the composer for the operator to edit */
  send?: boolean;
}

export interface BrainOpenTraceDetail {
  traceId: string;
}

export const BRAIN_ASK_EVENT = "cryptonite:brain-ask" as const;
export const BRAIN_OPEN_TRACE_EVENT = "cryptonite:brain-open-trace" as const;

declare global {
  interface WindowEventMap {
    "cryptonite:brain-ask": CustomEvent<BrainAskDetail>;
    "cryptonite:brain-open-trace": CustomEvent<BrainOpenTraceDetail>;
  }
}

export function dispatchBrainAsk(detail: BrainAskDetail): void {
  window.dispatchEvent(new CustomEvent<BrainAskDetail>(BRAIN_ASK_EVENT, { detail }));
}

export function dispatchBrainOpenTrace(traceId: string): void {
  window.dispatchEvent(new CustomEvent<BrainOpenTraceDetail>(BRAIN_OPEN_TRACE_EVENT, { detail: { traceId } }));
}
