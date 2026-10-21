"use client";

import { AlertTriangle, CalendarClock, OctagonAlert } from "lucide-react";
import type { AiStatus, Mandate, SessionVerdict } from "@/lib/mission/types";

/** ADR 0010: why a mandate takes no new entries today, and what happened to its positions */
const SESSION_BANNER: Record<Exclude<SessionVerdict, "ACTIVE">, { title: string; body: string; style: string }> = {
  LOSS_LATCHED: {
    title: "DAILY LOSS LIMIT",
    body: "daily loss limit reached: positions are being closed and new entries resume 00:00 UTC.",
    style: "border-red-700 bg-red-950/40 text-red-200",
  },
  LOCK_FIRED: {
    title: "PROFIT LOCK",
    body: "the day's P&L fell back to the profit lock after the target: positions are being closed and new entries resume 00:00 UTC.",
    style: "border-amber-700 bg-amber-950/30 text-amber-200",
  },
  TARGET_MET: {
    title: "DAILY TARGET MET",
    body: "daily profit target reached: positions are kept, new entries resume 00:00 UTC.",
    style: "border-cyan-700 bg-cyan-950/30 text-cyan-200",
  },
};

/** Degraded and halted states (§7.1): named causes, colour and text, announced to screen readers. */
export function MissionBanners({ status, mandates = [] }: { status: AiStatus | null; mandates?: Mandate[] }) {
  if (!status) return null;
  const sessions = mandates.filter((m) => m.status !== "ARCHIVED" && m.session && m.session.verdict !== "ACTIVE");
  return (
    <div className="space-y-2">
      {sessions.map((m) => {
        const b = SESSION_BANNER[m.session!.verdict as Exclude<SessionVerdict, "ACTIVE">];
        return (
          <div
            key={m.id}
            role="alert"
            data-testid="mission-session-banner"
            className={`flex items-start gap-2 rounded border px-3 py-2 text-2xs ${b.style}`}
          >
            <CalendarClock className="mt-0.5 h-3.5 w-3.5 flex-none" />
            <p>
              <span className="font-bold uppercase tracking-wide">{b.title}</span> · <span className="font-semibold">{m.name}</span>: {b.body}
            </p>
          </div>
        );
      })}
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
