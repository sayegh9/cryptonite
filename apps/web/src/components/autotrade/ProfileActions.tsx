"use client";

import { useState } from "react";
import { toast } from "sonner";
import { canArchive, canPause, canResume, canStart, canStop, errorCopy, isTotpError, MODE_LABEL } from "@/lib/autotrade/copy";
import type { ApiError, Profile } from "@/lib/autotrade/types";
import { useAutoTradeStore } from "@/lib/stores/useAutoTradeStore";
import { isStaleBuild, STALE_BUILD_TITLE, useStaleBuild } from "@/lib/stores/useBuildStore";
import { btnPrimary, btnSecondary, ConfirmDialog, TotpDialog } from "./Dialogs";

type Pending = null | "start" | "resume" | "stop" | "archive";

/**
 * Start / Resume / Pause / Stop / Archive for one profile, each offered only in a status the API accepts. Start of an
 * Ask me profile needs no code; Start above Ask and every Resume need TOTP (profile_lifecycle). Stop and Archive only
 * reduce risk. The card keeps its state until the API confirms.
 */
export function ProfileActions({ profile: p, compact = false }: { profile: Profile; compact?: boolean }) {
  const store = useAutoTradeStore();
  const pendingWrite = useAutoTradeStore((s) => s.mutationById[p.id] === "pending");
  const staleBuild = useStaleBuild();
  const [dialog, setDialog] = useState<Pending>(null);
  const [totpFor, setTotpFor] = useState<null | "start" | "resume">(null);
  const [error, setError] = useState<string | null>(null);
  const busy = pendingWrite;
  const open = p.positions?.length ?? 0;

  const fail = (action: string) => (err: ApiError) => {
    const msg = errorCopy(err, p.execution_mode);
    setError(err.message ? msg : `Couldn't ${action} ${p.name}. Try again.`);
    toast.error(err.message ? msg : `Couldn't ${action} ${p.name}. Try again.`);
  };

  const doStart = async (code?: string) => {
    if (isStaleBuild()) return;
    setError(null);
    try {
      if (dialog === "resume" || totpFor === "resume") await store.resumeProfile(p.id, code ?? "");
      else await store.startProfile(p.id, code);
      toast.success(p.dry_run ? `${p.name} started in dry run.` : `${p.name} started.`);
      setDialog(null);
      setTotpFor(null);
    } catch (e) {
      const err = e as ApiError;
      if (isTotpError(err) && !code) {
        // The lifecycle asks for a code (a mode above Ask): ask for it, keep the dialog's choice
        setTotpFor(dialog === "resume" ? "resume" : "start");
        setDialog(null);
        return;
      }
      if (code && isTotpError(err)) return setError(errorCopy(err));
      fail(dialog === "resume" || totpFor === "resume" ? "resume" : "start")(err);
      setDialog(null);
      setTotpFor(null);
    }
  };

  const size = compact ? "px-2.5 py-1.5" : "";
  return (
    <div className="flex flex-wrap gap-2">
      {canStart(p.status) && (
        <button type="button" className={`${btnPrimary} ${size}`} disabled={busy || staleBuild} title={staleBuild ? STALE_BUILD_TITLE : undefined}
          aria-label={`Start ${p.name}`} onClick={() => { setError(null); setDialog("start"); }}>
          Start
        </button>
      )}
      {canResume(p.status) && (
        <button type="button" className={`${btnPrimary} ${size}`} disabled={busy || staleBuild} title={staleBuild ? STALE_BUILD_TITLE : undefined}
          aria-label={`Resume ${p.name}`} onClick={() => { setError(null); setDialog("resume"); }}>
          Resume
        </button>
      )}
      {canPause(p.status) && (
        <button
          type="button"
          className={`${btnSecondary} ${size}`}
          disabled={busy}
          aria-label={`Pause ${p.name}`}
          onClick={async () => {
            try {
              await store.pauseProfile(p.id);
              toast.success(`${p.name} paused. Existing positions remain open.`);
            } catch (e) {
              fail("pause")(e as ApiError);
            }
          }}
        >
          Pause
        </button>
      )}
      {canStop(p.status) && (
        <button type="button" className={`${btnSecondary} ${size}`} disabled={busy} aria-label={`Stop ${p.name}`} onClick={() => { setError(null); setDialog("stop"); }}>
          Stop
        </button>
      )}
      {canArchive(p.status, open) && (
        <button type="button" className={`${btnSecondary} ${size}`} disabled={busy} aria-label={`Archive ${p.name}`} onClick={() => { setError(null); setDialog("archive"); }}>
          Archive
        </button>
      )}

      <ConfirmDialog
        open={dialog === "start" || dialog === "resume"}
        title={`${dialog === "resume" ? "Resume" : "Start"} ${p.name}?`}
        body={
          <>
            Mode: {MODE_LABEL[p.execution_mode]} · {p.environment} · {p.dry_run ? "Dry run: orders are simulated." : "Orders are placed on the paper account."}
            {dialog === "resume" && <> Open positions continue under this profile. Resuming needs your TOTP code.</>}
          </>
        }
        confirmLabel={dialog === "resume" ? "Resume profile" : "Start profile"}
        pending={busy}
        error={error}
        onClose={() => setDialog(null)}
        onConfirm={() => {
          if (dialog === "resume") {
            setTotpFor("resume");
            setDialog(null);
          } else if (p.execution_mode !== "ASK") {
            setTotpFor("start");
            setDialog(null);
          } else void doStart();
        }}
      />
      <TotpDialog
        open={totpFor !== null}
        change={totpFor === "resume" ? `Resuming ${p.name}` : `Starting ${p.name} in ${MODE_LABEL[p.execution_mode]}`}
        confirmLabel={totpFor === "resume" ? "Confirm and resume" : "Confirm and start"}
        pending={busy}
        error={totpFor ? error : null}
        onClose={() => {
          setTotpFor(null);
          setError(null);
        }}
        onSubmit={(code) => void doStart(code)}
      />
      <ConfirmDialog
        open={dialog === "stop"}
        title={`Stop ${p.name}?`}
        body="No new entries. Open positions stay open and close on their own stops and targets."
        checkboxLabel="Also close open positions now"
        confirmLabel="Stop profile"
        danger
        pending={busy}
        error={error}
        onClose={() => setDialog(null)}
        onConfirm={async (close) => {
          try {
            await store.stopProfile(p.id, close);
            toast.success(close ? "Stop requested. Closing open positions now." : "Stop requested. Open positions will close on their stops and targets.");
            setDialog(null);
          } catch (e) {
            setError(errorCopy(e as ApiError));
          }
        }}
      />
      <ConfirmDialog
        open={dialog === "archive"}
        title={`Archive ${p.name}?`}
        body="It disappears from the list. Its history is kept."
        confirmLabel="Archive profile"
        danger
        pending={busy}
        error={error}
        onClose={() => setDialog(null)}
        onConfirm={async () => {
          try {
            await store.archiveProfile(p.id);
            toast.success(`${p.name} archived.`);
            setDialog(null);
          } catch (e) {
            setError(errorCopy(e as ApiError));
          }
        }}
      />
    </div>
  );
}
