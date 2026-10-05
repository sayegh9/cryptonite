import { fetchApi } from "@/lib/api";
import type { AiStatus, Approval, AutonomyView, EvalRun, Mandate, OutcomesByRegime, Reliability } from "./types";

/** /api/v1/ai/* (the web reaches the backend only via /api/v1). Writes carry the CSRF token. */

const csrf = () => (typeof window !== "undefined" ? localStorage.getItem("csrf_token") || "" : "");

function write(path: string, body: unknown, method = "POST") {
  return fetchApi(path, { method, headers: { "X-CSRF-Token": csrf() }, body: JSON.stringify(body ?? {}) });
}

export const listMandates = async (): Promise<Mandate[]> => (await fetchApi("/ai/mandates")).mandates ?? [];
export const listApprovals = async (): Promise<{ now: string; approvals: Approval[] }> => fetchApi("/ai/approvals?status=pending");
export const fetchAiStatus = async (): Promise<AiStatus> => fetchApi("/ai/status");
export const fetchAutonomy = async (mandateId: string): Promise<AutonomyView> => fetchApi(`/ai/mandates/${mandateId}/autonomy`);
export const fetchEvals = async (): Promise<{
  runs: EvalRun[];
  calibration_note: string;
  calibration: Record<string, Reliability> | null;
}> => fetchApi("/ai/evals");
export const fetchOutcomesByRegime = async (mandateId: string | null, horizon = "24h"): Promise<OutcomesByRegime> =>
  fetchApi(`/ai/outcomes/by-regime?horizon=${encodeURIComponent(horizon)}${mandateId ? `&mandate_id=${encodeURIComponent(mandateId)}` : ""}`);

export const decideApproval = (id: string, decision: "APPROVE" | "VETO") => write(`/ai/approvals/${id}/decision`, { decision });
export const createMandate = (body: Record<string, unknown>): Promise<Mandate> => write("/ai/mandates", body);
export const editMandate = (id: string, body: Record<string, unknown>): Promise<Mandate> => write(`/ai/mandates/${id}`, body, "PATCH");
export const pauseMandate = (id: string): Promise<Mandate> => write(`/ai/mandates/${id}/pause`, {});
export const resumeMandate = (id: string, totp_code: string): Promise<Mandate> => write(`/ai/mandates/${id}/resume`, { totp_code });
export const demoteMandate = (id: string): Promise<Mandate> => write(`/ai/mandates/${id}/demote`, {});
export const evaluateAutonomy = (id: string): Promise<AutonomyView> => write(`/ai/mandates/${id}/autonomy/evaluate`, {});
export const confirmPromotion = (id: string, proposal_id: number, totp_code: string): Promise<AutonomyView> =>
  write(`/ai/mandates/${id}/autonomy/confirm`, { proposal_id, totp_code });
export const declinePromotion = (id: string, proposal_id: number): Promise<AutonomyView> =>
  write(`/ai/mandates/${id}/autonomy/decline`, { proposal_id });
