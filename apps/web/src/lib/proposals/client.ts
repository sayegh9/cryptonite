import { fetchApi, type FetchApiOptions } from "@/lib/api";
import type { CardPreflight } from "@/lib/brain/types";

/** /api/v1/proposals: scanner setups the debate said HOLD on (H13b). Writes carry the CSRF token. */

export type ProposalStatus = "PENDING" | "APPROVED" | "REJECTED" | "EXPIRED";

export interface ProposalCard {
  allocation_pct: number;
  stop_loss: number;
  take_profit: number;
  setup_grade: "A" | "B" | "C";
  grade_reasons?: string[];
  grade_inputs: { entry: number };
  against_debate_verdict: string;
  debate_reasoning?: string;
  preset_slug?: string | null;
  timeframe?: string | null;
}

export interface Proposal {
  id: string;
  instrument_id: string;
  side: "BUY" | "SELL";
  status: ProposalStatus;
  refusal: string | null;
  command_id: string | null;
  expires_at: string;
  created_at: string;
  decided_at: string | null;
  mandate_name: string | null;
  card: ProposalCard;
}

export interface ProposalList {
  now: string;
  pending: number;
  proposals: Proposal[];
}

/** The Brain card's dry run, with the reward-to-risk at the live price against the floor approve enforces */
export type ProposalPreflight = CardPreflight & {
  live_entry?: number | null;
  live_rr?: number | null;
  rr_floor?: number;
  meets_rr_floor?: boolean | null;
};

export interface ProposalApproval {
  acknowledge_against_debate: true;
  totp_code?: string;
  leverage?: number;
  confirm_high_leverage?: boolean;
}

const csrf = () => (typeof window !== "undefined" ? localStorage.getItem("csrf_token") || "" : "");

function write(path: string, body?: unknown) {
  return fetchApi(path, { method: "POST", headers: { "X-CSRF-Token": csrf() }, body: JSON.stringify(body ?? {}) });
}

export const listProposals = (init?: Pick<FetchApiOptions, "signal" | "timeoutMs">): Promise<ProposalList> =>
  fetchApi("/proposals", init);
export const preflightProposal = (id: string, leverage?: number | null): Promise<ProposalPreflight> =>
  write(`/proposals/${id}/preflight${leverage != null ? `?leverage=${encodeURIComponent(String(leverage))}` : ""}`);
export const approveProposal = (id: string, body: ProposalApproval): Promise<{ command_id: string; status: string }> =>
  write(`/proposals/${id}/approve`, body);
export const rejectProposal = (id: string): Promise<{ status: string }> => write(`/proposals/${id}/reject`);
