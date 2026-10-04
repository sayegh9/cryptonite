"use client";

import { AlertTriangle, OctagonAlert } from "lucide-react";
import type { AiStatus } from "@/lib/mission/types";

/** Degraded and halted states (§7.1): named causes, colour and text, announced to screen readers. */
export function MissionBanners({ status }: { status: AiStatus | null }) {
  if (!status) return null;
  return (
    <div className="space-y-2">
      {status.halted_mandates.length > 0 && (
        <div
          role="alert"
          data-testid="mission-halted-banner"
          className="flex items-start gap-2 rounded border border-red-700 bg-red-950/40 px-3 py-2 text-2xs text-red-200"
        >
          <OctagonAlert className="mt-0.5 h-3.5 w-3.5 flex-none text-red-400" />
          <div>
            <p className="font-bold uppercase tracking-wide">
              HALTED · {status.halted_mandates.length} mandate{status.halted_mandates.length > 1 ? "s" : ""} take no new risk
            </p>
            <ul className="mt-1 space-y-0.5">
              {status.halted_mandates.map((h) => (
                <li key={h.id}>
                  <span className="font-semibold">{h.name}</span>: {h.reason || "halted"}. Positions are being closed; resume needs your TOTP.
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}
      {status.degraded.length > 0 && (
        <div
          role="alert"
          data-testid="mission-degraded-banner"
          className="flex items-start gap-2 rounded border border-amber-700 bg-amber-950/30 px-3 py-2 text-2xs text-amber-200"
        >
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 flex-none text-amber-400" />
          <div>
            <p className="font-bold uppercase tracking-wide">DEGRADED</p>
            <ul className="mt-1 list-disc space-y-0.5 pl-4">
              {status.degraded.map((d) => (
                <li key={d}>{d}</li>
              ))}
            </ul>
          </div>
        </div>
      )}
    </div>
  );
}
