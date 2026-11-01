import { fetchApi, type FetchApiOptions } from "@/lib/api";
import type {
  ApiError,
  CreatedProfile,
  DecisionPage,
  Direction,
  ExecutionMode,
  Feasibility,
  PerformanceRange,
  Profile,
  ProfileEstimate,
  ProfilePerformance,
  PresetStats,
  ProfileSummary,
  StopAllResult,
  Style,
} from "./types";

/**
 * Auto-Trade API client. Writes go only through the existing /ai/mandates/* routes, plus the one P2 write
 * (/ai/profiles/stop-all). Every write carries the CSRF token; a TOTP code is a call argument, never stored.
 */

const csrf = () => (typeof window !== "undefined" ? localStorage.getItem("csrf_token") || "" : "");

function write<T>(path: string, body: unknown, method = "POST"): Promise<T> {
  return fetchApi(path, { method, headers: { "X-CSRF-Token": csrf() }, body: JSON.stringify(body ?? {}) });
}

type ReadOptions = Pick<FetchApiOptions, "signal" | "timeoutMs">;
const withTotp = (body: Record<string, unknown>, totp?: string) => (totp ? { ...body, totp_code: totp } : body);

export const MODE_LEVEL: Record<ExecutionMode, 1 | 2 | 3> = { ASK: 1, VETO: 2, AUTO: 3 };

export const listProfiles = async (init?: ReadOptions): Promise<Profile[]> => (await fetchApi("/ai/mandates", init)).mandates ?? [];
export const getProfile = (id: string, init?: ReadOptions): Promise<Profile> => fetchApi(`/ai/mandates/${id}`, init);
export const createProfile = (body: Record<string, unknown>, totp: string): Promise<CreatedProfile> =>
  write("/ai/mandates", withTotp(body, totp));
export const patchProfile = (id: string, body: Record<string, unknown>, totp?: string): Promise<Profile> =>
  write(`/ai/mandates/${id}`, withTotp(body, totp), "PATCH");
export const startProfile = (id: string, totp?: string): Promise<Profile> => write(`/ai/mandates/${id}/start`, withTotp({}, totp));
export const resumeProfile = (id: string, totp: string): Promise<Profile> => write(`/ai/mandates/${id}/resume`, { totp_code: totp });
export const pauseProfile = (id: string, reason?: string): Promise<Profile> =>
  write(`/ai/mandates/${id}/pause`, reason ? { reason } : {});
export const stopProfile = (id: string, close: boolean): Promise<Profile> => write(`/ai/mandates/${id}/stop`, { close });
export const archiveProfile = (id: string): Promise<Profile> => write(`/ai/mandates/${id}/archive`, {});
/** Sets the execution mode (ADR 0011 D2): raising needs TOTP, lowering is free. */
export const setProfileMode = (id: string, mode: ExecutionMode, totp?: string) =>
  write(`/ai/mandates/${id}/autonomy/confirm`, withTotp({ level: MODE_LEVEL[mode] }, totp));

export const stopAllProfiles = (close: boolean): Promise<StopAllResult> => write("/ai/profiles/stop-all", { close });
export const getSummary = (id: string, init?: ReadOptions): Promise<ProfileSummary> => fetchApi(`/ai/profiles/${id}/summary`, init);
/** Every non-archived profile's summary in one request (the list's poll). */
export const getSummaries = (init?: ReadOptions): Promise<{ as_of: string; summaries: ProfileSummary[] }> =>
  fetchApi("/ai/profiles/summaries", init);
export const getDecisions = (id: string, cursor?: string, init?: ReadOptions): Promise<DecisionPage> =>
  fetchApi(`/ai/profiles/${id}/decisions?limit=50${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`, init);
export const getPerformance = (id: string, range: PerformanceRange, init?: ReadOptions): Promise<ProfilePerformance> =>
  fetchApi(`/ai/profiles/${id}/performance?range=${range}`, init);
export const getPresetStats = (init?: ReadOptions): Promise<PresetStats> => fetchApi("/ai/profiles/presets/stats", init);
export const getEstimate = (coins: string[], style: Style, direction: Direction, init?: ReadOptions): Promise<ProfileEstimate> =>
  fetchApi(
    `/ai/profiles/estimate?coins=${encodeURIComponent(coins.join(","))}&style=${style}&direction=${direction}`,
    init,
  );

/** H3: can this budget place an order above the minimum? (the wizard asks before the profile exists) */
export const getFeasibility = async (
  q: { budget: string; maxPositionPct: string; maxRiskPct: string; maxLeverage: string; markets: string[]; timeframes: string[] },
  init?: ReadOptions,
): Promise<Feasibility> =>
  (
    await fetchApi(
      `/ai/profiles/feasibility?budget=${encodeURIComponent(q.budget)}&max_position_pct=${encodeURIComponent(q.maxPositionPct)}` +
        `&max_risk_pct=${encodeURIComponent(q.maxRiskPct)}&max_leverage=${encodeURIComponent(q.maxLeverage)}` +
        `&markets=${q.markets.join(",")}&timeframes=${q.timeframes.join(",")}`,
      init,
    )
  ).feasibility ?? {};

/** The API's "CODE: text" detail split into a code and the readable rest. */
export function toApiError(err: unknown): ApiError {
  const e = err as { message?: string; status?: number; data?: { detail?: unknown }; code?: string };
  if (e && typeof e === "object" && "code" in e && !("data" in e) && typeof e.message === "string") return e as ApiError;
  const message = String(e?.message || "Request failed");
  // FastAPI's validation errors name the field: keep them so the form can show each next to its field
  const detail = e?.data?.detail;
  const fields = Array.isArray(detail)
    ? Object.fromEntries(
        detail
          .filter((d): d is { loc: unknown[]; msg: string } => !!d && Array.isArray((d as { loc?: unknown }).loc))
          .map((d) => [String(d.loc[d.loc.length - 1]), d.msg]),
      )
    : undefined;
  const m = /^([A-Z][A-Z0-9_]{2,}):\s*(.*)$/s.exec(message);
  const base: ApiError = m ? { code: m[1], message: m[2] || m[1], status: e?.status } : { message, status: e?.status };
  return fields && Object.keys(fields).length ? { ...base, fields } : base;
}
