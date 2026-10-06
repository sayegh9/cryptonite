export const API_BASE = typeof window !== "undefined" ? "/api/v1" : (process.env.API_BASE || "http://localhost:8000/api/v1");

export interface FetchApiOptions extends RequestInit {
  /** Abort the request (headers and body) after this many ms; the thrown error has `timeout: true` */
  timeoutMs?: number;
}

export async function fetchApi(endpoint: string, { timeoutMs, ...options }: FetchApiOptions = {}) {
  const url = endpoint.startsWith("/agent-api") ? endpoint : `${API_BASE}${endpoint}`;

  // A hung backend must fail, not keep the caller waiting forever: abort on our timer or on the caller's signal
  let signal = options.signal ?? undefined;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let timedOut = false;
  if (timeoutMs) {
    const ctrl = new AbortController();
    const outer = options.signal;
    if (outer?.aborted) ctrl.abort(outer.reason);
    else outer?.addEventListener("abort", () => ctrl.abort(outer.reason), { once: true });
    timer = setTimeout(() => {
      timedOut = true;
      ctrl.abort();
    }, timeoutMs);
    signal = ctrl.signal;
  }

  let res: Response;
  let data;
  try {
    res = await fetch(url, {
      ...options,
      signal,
      credentials: "include", // Important for cookies!
      headers: {
        "Content-Type": "application/json",
        ...options.headers,
      },
    });
    try {
      data = await res.json();
    } catch (e) {
      if (signal?.aborted) throw e;
      data = {};
    }
  } catch (e) {
    if (!timedOut) throw e;
    const error: any = new Error(`Request timed out after ${timeoutMs} ms`);
    error.timeout = true;
    throw error;
  } finally {
    clearTimeout(timer);
  }

  if (!res.ok) {
    let errMsg = res.statusText;
    if (Array.isArray(data.detail)) {
      errMsg = data.detail.map((d: any) => d.msg).join(", ");
    } else if (data.detail) {
      errMsg = data.detail;
    } else if (data.message) {
      errMsg = data.message;
    }
    const error: any = new Error(errMsg);
    error.status = res.status;
    error.data = data;
    throw error;
  }
  
  return data;
}
