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

/** Data older than this (two header polls) is shown as unknown, never as current */
export const STALE_MS = 30_000;

export type ModeBannerTone = "pending" | "unknown" | "latched" | "idle" | "gated" | "acting";

export interface ModeBannerInput {
  mandates: Mandate[];
  status: AiStatus | null;
  /** Outcome of the latest fetch: true ok, false failed, null not fetched yet */
  mandatesOk: boolean | null;
  statusOk: boolean | null;
  /** Date.now() of the last successful fetch of each source */
  mandatesAt: number | null;
  statusAt: number | null;
  /** The latest fetch was refused (401/403): not signed in as operator */
  denied: boolean;
  now: number;
}

/**
 * A qualifier on the claim. Flags are rendered before the summary and never truncated: `warn` flags
 * weaken it (unknown, latched), `info` flags count mandates that stopped (halted, paused).
 */
export interface ModeBannerFlag {
  label: string;
  kind: "warn" | "info";
}

export interface ModeBanner {
  tone: ModeBannerTone;
  venue: typeof AI_VENUE;
  flags: ModeBannerFlag[];
  /** The authority claim and mandate counts, e.g. "approvals required · 2 mandates at L1"; may truncate */
  summary: string;
  /** Venue, flags and summary joined, e.g. "PAPER · 1 halted · approvals required · 2 mandates at L1" */
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
const warn = (label: string): ModeBannerFlag => ({ label, kind: "warn" });
const info = (label: string): ModeBannerFlag => ({ label, kind: "info" });

type Freshness = "ok" | "failed" | "stale" | "pending";

function freshness(ok: boolean | null, at: number | null, now: number): Freshness {
  if (ok === null) return "pending";
  if (!ok) return "failed";
  return at != null && now - at <= STALE_MS ? "ok" : "stale";
}

/** Fixed wording (no running age), so the role="status" region is not re-announced on every tick */
function why(f: Freshness, many = false): string {
  if (f === "stale") return `${many ? "have" : "has"} not updated in over ${STALE_MS / 1000} s`;
  if (f === "pending") return many ? "are loading" : "is loading";
  return "could not be loaded";
}

function levelSummary(active: Mandate[]): string {
  const byLevel = new Map<number, number>();
  for (const m of active) byLevel.set(m.autonomy_level, (byLevel.get(m.autonomy_level) ?? 0) + 1);
  const levels = [...byLevel.keys()].sort((a, b) => a - b);
  if (levels.length === 1) return `${plural(active.length, "mandate")} at L${levels[0]}`;
  return levels.map((l, i) => `${i === 0 ? plural(byLevel.get(l)!, "mandate") : byLevel.get(l)} at L${l}`).join(", ");
}

function banner(tone: ModeBannerTone, flags: ModeBannerFlag[], summary: string, detail: string): ModeBanner {
  const text = [AI_VENUE, ...flags.map((f) => f.label), summary].filter(Boolean).join(" · ");
  return { tone, venue: AI_VENUE, flags, summary, text, detail };
}

export function computeModeBanner(input: ModeBannerInput): ModeBanner {
  const { mandates, status, denied, now } = input;
  const ks = freshness(input.statusOk, input.statusAt, now);
  const md = freshness(input.mandatesOk, input.mandatesAt, now);
  const ksKnown = ks === "ok" && status != null;
  const mandatesKnown = md === "ok";

  // A latched kill switch outranks everything we know about mandates
  if (ksKnown && status.kill_switch.latched) {
    const reason = status.kill_switch.reason ? `: ${status.kill_switch.reason}` : "";
    return banner(
      "latched",
      [warn("kill switch latched")],
      "",
      `The ${AI_VENUE} kill switch is latched${reason}. No new orders are sent until an operator resets it.`,
    );
  }

  if (!ksKnown && !mandatesKnown) {
    if (ks === "pending" && md === "pending") {
      return banner("pending", [warn("status unknown")], "", "Loading mandates and kill-switch state.");
    }
    const detail = denied
      ? "Sign in as operator to see mandates and the kill switch. Until then the AI's authority is unknown here."
      : ks === md
        ? `Mandates and kill-switch state ${why(ks, true)}. The AI's authority is unknown here, not zero.`
        : `Mandates ${why(md, true)} and kill-switch state ${why(ks)}. The AI's authority is unknown here, not zero.`;
    return banner("unknown", [warn("status unknown")], "", detail);
  }

  // Unknowns lead, so a narrow header can never cut the qualifier that weakens the claim
  const flags: ModeBannerFlag[] = [];
  let unknownDetail = "";
  if (!ksKnown) {
    flags.push(warn("kill switch unknown"));
    unknownDetail = `Kill-switch state ${why(ks)}. `;
  }
  const ksDetail = ksKnown ? " Kill switch armed." : "";

  if (!mandatesKnown) {
    return banner(
      "unknown",
      [warn("mandates unknown")],
      "",
      `Mandates ${why(md, true)}, so the AI's authority is unknown here.${ksDetail}`,
    );
  }

  const active = mandates.filter((m) => m.status === "ACTIVE");
  const halted = mandates.filter((m) => m.status === "HALTED").length;
  const paused = mandates.filter((m) => m.status === "PAUSED").length;
  if (halted) flags.push(info(`${halted} halted`));
  if (paused) flags.push(info(`${paused} paused`));
  const stopped = [halted && `${halted} halted`, paused && `${paused} paused`].filter(Boolean).join(", ");
  const stoppedDetail = stopped ? ` ${stopped[0].toUpperCase()}${stopped.slice(1)} (reduce-only).` : "";

  if (active.length === 0) {
    return banner(
      ksKnown ? "idle" : "unknown",
      flags,
      "no active mandates",
      `${unknownDetail}The AI cannot open new positions: no mandate is active.${stoppedDetail}${ksDetail}`,
    );
  }

  const top = Math.max(...active.map((m) => m.autonomy_level)) as 1 | 2 | 3;
  return banner(
    !ksKnown ? "unknown" : top === 1 ? "gated" : "acting",
    flags,
    `${AUTHORITY[top]} · ${levelSummary(active)}`,
    `${unknownDetail}${plural(active.length, "active mandate")} on ${AI_VENUE}. ${AUTHORITY_DETAIL[top]}${stoppedDetail}${ksDetail}`,
  );
}
