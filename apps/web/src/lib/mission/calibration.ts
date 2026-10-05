import type { IsotonicFit } from "./types";

/**
 * Calibrated conviction (F5, informational only): the hit rate the fitted isotonic step function gives
 * for a stated conviction (0-1). Below the first step the first level applies, above the last the last.
 * null when the map is not fitted or the conviction is unknown. Never used for sizing or approval.
 */
export function calibratedConviction(fit: IsotonicFit | null | undefined, conviction: unknown): number | null {
  if (!fit?.fitted || !fit.points?.length) return null;
  if (conviction === null || conviction === undefined || conviction === "") return null;
  const x = Number(conviction);
  if (!Number.isFinite(x)) return null;
  let y = fit.points[0][1];
  for (const [px, py] of fit.points) {
    if (px <= x) y = py;
    else break;
  }
  return y;
}
