/**
 * Typed cross-workspace event for mandates (blueprint §7.1):
 *
 *   cryptonite:mandate-updated   a mandate was created, edited, paused, resumed, promoted or demoted
 */

export interface MandateUpdatedDetail {
  mandateId: string;
  change: "created" | "edited" | "paused" | "resumed" | "demoted" | "promoted" | "decided";
}

export const MANDATE_UPDATED_EVENT = "cryptonite:mandate-updated" as const;

declare global {
  interface WindowEventMap {
    "cryptonite:mandate-updated": CustomEvent<MandateUpdatedDetail>;
  }
}

export function dispatchMandateUpdated(detail: MandateUpdatedDetail): void {
  window.dispatchEvent(new CustomEvent<MandateUpdatedDetail>(MANDATE_UPDATED_EVENT, { detail }));
}
