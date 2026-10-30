"use client";

import { useRef, useState } from "react";
import { toast } from "sonner";
import { errorCopy } from "@/lib/autotrade/copy";
import type { ApiError, Profile, StopAllResult } from "@/lib/autotrade/types";
import { useAutoTradeStore } from "@/lib/stores/useAutoTradeStore";
import { btnSecondary, ConfirmDialog } from "./Dialogs";

const plural = (n: number) => `${n} profile${n === 1 ? "" : "s"}`;
const OUTCOME: Record<string, string> = {
  STOP_REQUESTED: "Stopped",
  SKIPPED_STATUS_CHANGED: "Already stopping",
};

/** Stop every running or paused profile (no TOTP: it only reduces risk). The master and kill switches are untouched. */
export function StopAllButton({ profiles }: { profiles: Profile[] }) {
  const stopAll = useAutoTradeStore((s) => s.stopAllProfiles);
  const eligible = profiles.filter((p) => p.status === "ACTIVE" || p.status === "PAUSED");
  const [open, setOpen] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<StopAllResult | null>(null);
  const [announce, setAnnounce] = useState("");
  const trigger = useRef<HTMLButtonElement>(null);
  const n = eligible.length;
  const nameOf = (id: string) => profiles.find((p) => p.id === id)?.name ?? id;

  return (
    <>
      <button
        ref={trigger}
        type="button"
        className={`${btnSecondary} border-red-800 text-red-200`}
        disabled={n === 0}
        aria-describedby={n === 0 ? "stop-all-help" : undefined}
        onClick={() => {
          setError(null);
          setResult(null);
          setOpen(true);
        }}
      >
        Stop all
      </button>
      {n === 0 && (
        <span id="stop-all-help" className="sr-only">
          No running or paused profiles to stop.
        </span>
      )}
      <span role="status" aria-live="polite" className="sr-only">
        {announce}
      </span>
      <ConfirmDialog
        open={open}
        title="Stop all profiles?"
        body={`This stops new entries on ${n} running or paused profile${n === 1 ? "" : "s"}. Open positions stay open and close on their own stops and targets, unless you choose to close them now. The master switch is unchanged. Halted profiles are already reduce-only and are not included.`}
        checkboxLabel="Also close open positions now"
        confirmLabel={`Stop ${n} profile${n === 1 ? "" : "s"}`}
        danger
        pending={pending}
        error={error}
        onClose={() => {
          setOpen(false);
          trigger.current?.focus();
        }}
        onConfirm={async (close) => {
          setPending(true);
          setError(null);
          setAnnounce("Stopping profiles…");
          try {
            const r = await stopAll(close);
            setResult(r);
            const tail = close ? "Closing open positions now." : "Open positions will close on their stops and targets.";
            const msg =
              r.refused_count > 0
                ? `${plural(r.requested_count)} stopped; ${r.refused_count} couldn't be stopped. Review the results and retry the remaining profiles.`
                : `Stop requested for ${plural(r.requested_count)}. ${tail}`;
            setAnnounce(msg);
            if (r.refused_count > 0) toast.warning(msg);
            else {
              toast.success(msg);
              setOpen(false);
              trigger.current?.focus();
            }
          } catch (e) {
            const msg = errorCopy(e as ApiError);
            setError(msg);
            setAnnounce(msg);
          } finally {
            setPending(false);
          }
        }}
      >
        {result && result.refused_count > 0 && (
          <ul className="space-y-1 text-xs" aria-label="Stop all results">
            {result.profiles.map((r) => (
              <li key={r.id} className="flex justify-between gap-2">
                <span className="text-zinc-200">{nameOf(r.id)}</span>
                <span className={r.outcome === "STOP_REQUESTED" ? "text-emerald-300" : "text-red-300"}>
                  {OUTCOME[r.outcome ?? ""] ?? `Couldn't stop: ${r.error ?? "unknown reason"}`}
                </span>
              </li>
            ))}
          </ul>
        )}
      </ConfirmDialog>
    </>
  );
}
