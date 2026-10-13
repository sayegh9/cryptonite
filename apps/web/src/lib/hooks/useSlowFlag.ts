import { useEffect, useState } from "react";

/** How long a loading placeholder stays silent before it says the wait is longer than usual */
export const SLOW_LOADING_MS = 8_000;

/**
 * True once `active` has stayed true for `afterMs`. Loading states use it to replace a silent skeleton
 * with "still loading" text, so a slow backend reads as slow rather than broken.
 */
export function useSlowFlag(active: boolean, afterMs = SLOW_LOADING_MS): boolean {
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    if (!active) {
      setSlow(false);
      return;
    }
    const t = setTimeout(() => setSlow(true), afterMs);
    return () => clearTimeout(t);
  }, [active, afterMs]);
  return slow;
}
