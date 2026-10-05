import type { AiStatus, Mandate } from "./types";

/**
 * The truthful mode banner (G1, F-M12): what authority the AI holds right now, computed from
 * `/ai/mandates` and `/ai/status` instead of a hardcoded label.
 *
 * Venue: every AI command and approval is written with `environment = 'PAPER'` server-side
 * (`packages/execution/cryptonite_execution/approvals.py`, `services/debate_worker/mandate_routing.py`)
 * and `/ai/status` reads the PAPER kill switch. No endpoint reports the AI venue, so it is a
 * constant here; change it together with those writers.
 */
export const AI_VENUE = "PAPER" as const;

export type ModeBannerTone = "unknown" | "latched" | "idle" | "gated" | "acting";

export interface ModeBannerInput {
  mandates: Mandate[];
  status: AiStatus | null;
  /** Outcome of the latest fetch: true ok, false failed, null not fetched yet */
  mandatesOk: boolean | null;
  statusOk: boolean | null;
  /** The latest fetch was refused (401/403): not signed in as operator */
  denied: boolean;
}

export interface ModeBanner {
  tone: ModeBannerTone;
  /** Short label, e.g. "PAPER · 2 mandates at L1 · approvals required" */
  text: string;
  /** Longer explanation for the tooltip and screen readers */
  detail: string;
}

const AUTHORITY: Record<1 | 2 | 3, string> = {
  1: "approvals required",
  2: "acts after veto window",
  3: "acts without approval",
};

const AUTHORITY_DETAIL: Record<1 | 2 | 3, string> = {
  1: "Every risk-adding order waits for your approval; an unanswered approval means no.",
  2: "Risk-adding orders proceed when the veto window passes inside your active hours.",
  3: "The AI places orders inside its mandate without asking; you get a digest.",
};

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

function levelSummary(active: Mandate[]): string {
  const byLevel = new Map<number, number>();
  for (const m of active) byLevel.set(m.autonomy_level, (byLevel.get(m.autonomy_level) ?? 0) + 1);
  const levels = [...byLevel.keys()].sort((a, b) => a - b);
  if (levels.length === 1) return `${plural(active.length, "mandate")} at L${levels[0]}`;
  return levels.map((l, i) => `${i === 0 ? plural(byLevel.get(l)!, "mandate") : byLevel.get(l)} at L${l}`).join(", ");
}

export function computeModeBanner({ mandates, status, mandatesOk, statusOk, denied }: ModeBannerInput): ModeBanner {
  const ksKnown = statusOk === true && status != null;
  const mandatesKnown = mandatesOk === true;

  // A latched kill switch outranks everything we know about mandates
  if (ksKnown && status.kill_switch.latched) {
    const why = status.kill_switch.reason ? `: ${status.kill_switch.reason}` : "";
    return {
      tone: "latched",
      text: `${AI_VENUE} · kill switch latched`,
      detail: `The ${AI_VENUE} kill switch is latched${why}. No new orders are sent until an operator resets it.`,
    };
  }

  if (!ksKnown && !mandatesKnown) {
    const pending = mandatesOk === null && statusOk === null;
    return {
      tone: "unknown",
      text: `${AI_VENUE} · status unknown`,
      detail: pending
        ? "Loading mandates and kill-switch state."
        : denied
          ? "Sign in as operator to see mandates and the kill switch. Until then the AI's authority is unknown here."
          : "Mandates and kill-switch state could not be loaded. The AI's authority is unknown here, not zero.",
    };
  }

  const ksSuffix = ksKnown ? "" : " · kill switch unknown";
  const ksDetail = ksKnown ? " Kill switch armed." : " Kill-switch state could not be loaded.";

  if (!mandatesKnown) {
    return {
      tone: "unknown",
      text: `${AI_VENUE} · mandates unknown`,
      detail: `Mandates could not be loaded, so the AI's authority is unknown here.${ksDetail}`,
    };
  }

  const active = mandates.filter((m) => m.status === "ACTIVE");
  const halted = mandates.filter((m) => m.status === "HALTED").length;
  const paused = mandates.filter((m) => m.status === "PAUSED").length;
  const stopped = [halted && `${halted} halted`, paused && `${paused} paused`].filter(Boolean).join(", ");
  const stoppedSuffix = stopped ? ` · ${stopped}` : "";
  const stoppedDetail = stopped ? ` ${stopped[0].toUpperCase()}${stopped.slice(1)} (reduce-only).` : "";

  if (active.length === 0) {
    return {
      tone: "idle",
      text: `${AI_VENUE} · no active mandates${stoppedSuffix}${ksSuffix}`,
      detail: `The AI cannot open new positions: no mandate is active.${stoppedDetail}${ksDetail}`,
    };
  }

  const top = Math.max(...active.map((m) => m.autonomy_level)) as 1 | 2 | 3;
  return {
    tone: top === 1 ? "gated" : "acting",
    text: `${AI_VENUE} · ${levelSummary(active)} · ${AUTHORITY[top]}${stoppedSuffix}${ksSuffix}`,
    detail: `${plural(active.length, "active mandate")} on ${AI_VENUE}. ${AUTHORITY_DETAIL[top]}${stoppedDetail}${ksDetail}`,
  };
}
