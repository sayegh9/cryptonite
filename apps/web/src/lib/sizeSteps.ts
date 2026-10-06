import { fetchApi } from "@/lib/api";

/**
 * Instrument size steps (`size_increment` from GET /api/v1/instruments), keyed by instrument_id, for
 * formatting quantities to what the venue can actually trade (G4). Fetched once per page load; a
 * failure resolves to {} (callers fall back to significant digits) and is retried on the next call.
 */
let cached: Promise<Record<string, string>> | null = null;

export function fetchSizeSteps(): Promise<Record<string, string>> {
  if (!cached) {
    cached = fetchApi("/instruments")
      .then((rows: unknown) => {
        const out: Record<string, string> = {};
        if (Array.isArray(rows)) {
          for (const r of rows as { instrument_id?: string; size_increment?: string | null }[]) {
            if (r?.instrument_id && r.size_increment) out[r.instrument_id] = String(r.size_increment);
          }
        }
        return out;
      })
      .catch(() => {
        cached = null;
        return {};
      });
  }
  return cached;
}
