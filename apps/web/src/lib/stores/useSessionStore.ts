import { create } from "zustand";
import { fetchApi } from "@/lib/api";

/**
 * The operator session, the one answer every component reads (instead of each panel guessing from its own
 * failed request). Not persisted: the session cookie is the truth, so a reload checks it again rather than
 * trusting a stored flag.
 *
 * - checking: the first check (or a retry) has not answered yet. Show "verifying", never the sign-in form.
 * - authenticated: the server confirmed the operator session.
 * - anonymous: no operator session (the server said so, or an operator-only call was refused).
 * - unreachable: the check failed or timed out. Neither signed in nor signed out is known.
 */
export type SessionStatus = "checking" | "authenticated" | "anonymous" | "unreachable";

/** A hung backend becomes "unreachable" after this long, never an endless "checking" */
export const SESSION_CHECK_TIMEOUT_MS = 10_000;

interface SessionState {
  status: SessionStatus;
  username: string | null;
  checkedAt: number | null;
  /** Verify the cookie session with the server. Concurrent callers share one request. */
  check: () => Promise<void>;
  /** A sign-in succeeded, or an operator-only call answered OK */
  markAuthenticated: (username?: string | null) => void;
  /** An operator-only call was refused (401/403): the session ended or never existed */
  markAnonymous: () => void;
}

let inflight: Promise<void> | null = null;

/** The trip/reset actions send the CSRF token from localStorage; keep it in step with the session */
export function storeCsrfToken(token: string | null | undefined) {
  if (!token) return;
  try {
    localStorage.setItem("csrf_token", token);
  } catch {
    // Storage can be blocked (private windows); the session itself still works from the cookie
  }
}

export const useSessionStore = create<SessionState>()((set) => ({
  status: "checking",
  username: null,
  checkedAt: null,
  check: () => {
    if (inflight) return inflight;
    inflight = fetchApi("/auth/me", { timeoutMs: SESSION_CHECK_TIMEOUT_MS })
      .then((me) => {
        if (me?.authenticated) {
          storeCsrfToken(me.csrf_token);
          set({ status: "authenticated", username: me.username ?? null, checkedAt: Date.now() });
        } else {
          set({ status: "anonymous", username: null, checkedAt: Date.now() });
        }
      })
      .catch((err: any) => {
        if (err?.status === 401 || err?.status === 403) {
          set({ status: "anonymous", username: null, checkedAt: Date.now() });
        } else {
          set({ status: "unreachable", checkedAt: Date.now() });
        }
      })
      .finally(() => {
        inflight = null;
      });
    return inflight;
  },
  markAuthenticated: (username = null) =>
    set((s) => ({ status: "authenticated", username: username ?? s.username, checkedAt: Date.now() })),
  markAnonymous: () => set({ status: "anonymous", username: null, checkedAt: Date.now() }),
}));
