export const API_BASE = typeof window !== "undefined" ? "/api/v1" : (process.env.API_BASE || "http://localhost:8000/api/v1");

export async function fetchApi(endpoint: string, options: RequestInit = {}) {
  const res = await fetch(`${API_BASE}${endpoint}`, {
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
  } catch (err) {
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
    throw new Error(errMsg);
  }
  
  return data;
}
