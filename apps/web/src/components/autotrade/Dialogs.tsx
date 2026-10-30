"use client";

import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";

/**
 * The two Auto-Trade modals. Both use the app dialog (focus trap, Escape closes, focus returns to the trigger).
 * Neither keeps a TOTP code after it closes: the code lives only in this component's state while it is open.
 */

export const btn = "rounded-md px-3 py-2 text-xs font-semibold disabled:cursor-not-allowed disabled:opacity-50";
export const btnPrimary = `${btn} bg-cyan-600 text-zinc-950 hover:bg-cyan-500`;
export const btnDanger = `${btn} bg-red-600 text-white hover:bg-red-500`;
export const btnSecondary = `${btn} border border-zinc-700 text-zinc-200 hover:bg-zinc-800`;

export function ConfirmDialog({
  open,
  title,
  body,
  confirmLabel,
  danger,
  pending,
  checkboxLabel,
  error,
  onConfirm,
  onClose,
  children,
}: {
  open: boolean;
  title: string;
  body: ReactNode;
  confirmLabel: string;
  danger?: boolean;
  pending?: boolean;
  checkboxLabel?: string;
  error?: string | null;
  onConfirm: (checked: boolean) => void;
  onClose: () => void;
  children?: ReactNode;
}) {
  const [checked, setChecked] = useState(false);
  const boxId = useId();
  useEffect(() => {
    if (open) setChecked(false);
  }, [open]);
  return (
    <Dialog open={open} onOpenChange={(o) => !o && !pending && onClose()}>
      <DialogContent showCloseButton={false} className="border border-zinc-800 bg-zinc-950 text-zinc-200 sm:max-w-md">
        <DialogTitle className="text-sm font-bold text-zinc-100">{title}</DialogTitle>
        <DialogDescription className="text-xs leading-relaxed text-zinc-300">{body}</DialogDescription>
        {children}
        {checkboxLabel && (
          <label htmlFor={boxId} className="flex items-center gap-2 text-xs text-zinc-200">
            <input id={boxId} type="checkbox" checked={checked} onChange={(e) => setChecked(e.target.checked)} disabled={pending} />
            {checkboxLabel}
          </label>
        )}
        {error && (
          <p role="alert" className="text-xs text-red-300">
            {error}
          </p>
        )}
        {pending && (
          <p role="status" className="text-xs text-zinc-400">
            Working…
          </p>
        )}
        <div className="flex justify-end gap-2">
          <button type="button" className={btnSecondary} onClick={onClose} disabled={pending}>
            Cancel
          </button>
          <button type="button" className={danger ? btnDanger : btnPrimary} onClick={() => onConfirm(checked)} disabled={pending}>
            {confirmLabel}
          </button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

export function TotpDialog({
  open,
  change,
  confirmLabel,
  pending,
  error,
  onSubmit,
  onClose,
}: {
  open: boolean;
  /** What adds risk, in plain words ("Creating this profile") */
  change: string;
  confirmLabel: string;
  pending?: boolean;
  error?: string | null;
  onSubmit: (code: string) => void;
  onClose: () => void;
}) {
  const [code, setCode] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const inputId = useId();
  const errId = useId();
  useEffect(() => {
    if (!open) setCode("");
  }, [open]);
  useEffect(() => {
    if (error) {
      setCode("");
      inputRef.current?.focus();
    }
  }, [error]);
  return (
    <Dialog open={open} onOpenChange={(o) => !o && !pending && onClose()}>
      <DialogContent showCloseButton={false} initialFocus={inputRef} className="border border-zinc-800 bg-zinc-950 text-zinc-200 sm:max-w-sm">
        <DialogTitle className="text-sm font-bold text-zinc-100">Confirm this risk change</DialogTitle>
        <DialogDescription className="text-xs text-zinc-300">{change} increases risk, so it needs your current TOTP code.</DialogDescription>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (code.length === 6 && !pending) onSubmit(code);
          }}
          className="space-y-3"
        >
          <label htmlFor={inputId} className="block text-xs text-zinc-300">
            TOTP code
          </label>
          <input
            ref={inputRef}
            id={inputId}
            inputMode="numeric"
            autoComplete="one-time-code"
            pattern="\d{6}"
            maxLength={6}
            value={code}
            aria-invalid={!!error}
            aria-describedby={error ? errId : undefined}
            onChange={(e) => setCode(e.target.value.replace(/\D/g, "").slice(0, 6))}
            className="w-32 rounded border border-zinc-700 bg-zinc-900 px-2 py-1.5 font-mono text-base tracking-widest text-zinc-100"
          />
          {error && (
            <p id={errId} role="alert" className="text-xs text-red-300">
              {error}
            </p>
          )}
          <div className="flex justify-end gap-2">
            <button type="button" className={btnSecondary} onClick={onClose} disabled={pending}>
              Cancel
            </button>
            <button type="submit" className={btnPrimary} disabled={pending || code.length !== 6}>
              {pending ? "Confirming…" : confirmLabel}
            </button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
