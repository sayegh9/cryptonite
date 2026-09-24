export const API_BASE = typeof window !== "undefined" ? "/api/v1" : (process.env.API_BASE || "http://localhost:8000/api/v1");

export async function fetchApi(endpoint: string, options: RequestInit = {}) {
  const url = endpoint.startsWith("/agent-api") ? endpoint : `${API_BASE}${endpoint}`;
  const res = await fetch(url, {
    ...options,
    credentials: "include", // Important for cookies!
    headers: {
      "Content-Type": "application/json",
      ...options.headers,
    },
  });
  
  let data;
  try {
    data = await res.json();
  } catch {
    data = {};
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
