"use client";

import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { fetchApi } from "@/lib/api";
import { useSessionStore } from "@/lib/stores/useSessionStore";

/** Open perps are read again this often while the operator is signed in */
const POLL_MS = 10_000;

interface PositionRow {
  instrument_id?: string;
  status?: string;
  quantity?: string | number;
}

const isOpenPerp = (r: PositionRow) =>
  typeof r.instrument_id === "string" &&
  r.instrument_id.split(".")[0].toUpperCase().endsWith("-PERP") &&
  (r.status ?? "OPEN") === "OPEN" &&
  Number(r.quantity) > 0;

/**
 * "Flatten perps" in the header (leverage plan 4.7), shown while any perp position is open. It closes every perp
 * with a reduce-only market close and leaves spot alone; it needs the operator's TOTP code. The API sends one
 * CLOSE_POSITION per perp to the instance that holds it.
 */
export function FlattenPerpsButton() {
  const session = useSessionStore((s) => s.status);
  const [perps, setPerps] = useState<string[]>([]);
  const [open, setOpen] = useState(false);
  const [totp, setTotp] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (session !== "authenticated") {
      setPerps([]);
      return;
    }
    let cancelled = false;
    const load = () =>
      fetchApi("/paper/positions")
        .then((rows: unknown) => {
          if (cancelled || !Array.isArray(rows)) return;
          const held = Array.from(new Set((rows as PositionRow[]).filter(isOpenPerp).map((r) => r.instrument_id as string))).sort();
          setPerps(held);
        })
        .catch(() => {
          // Unknown: keep what was last seen (a flatten is still refused server-side without a perp to close)
        });
    load();
    const id = setInterval(load, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [session]);

  useEffect(() => {
    if (open) input.current?.focus();
  }, [open]);

  if (perps.length === 0) return null;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const res = await fetchApi("/paper/perps/flatten", {
        method: "POST",
        headers: { "X-CSRF-Token": localStorage.getItem("csrf_token") || "" },
        body: JSON.stringify({ totp_code: totp }),
      });
      toast.success(`Flatten perps: ${res?.closed ?? 0} close order(s) queued`);
      setOpen(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Flatten failed");
    } finally {
      // A used code is never sent again
      setTotp("");
      setBusy(false);
    }
  };

  return (
    <div className="relative ml-2 flex-none">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        title={`Close ${perps.join(", ")} (spot is left alone)`}
        className="rounded border border-leverage-high/70 bg-leverage-high/10 px-2 py-1 font-mono text-2xs font-bold text-leverage-high hover:bg-leverage-high/20"
      >
        Flatten perps ({perps.length})
      </button>
      {open && (
        <form
          onSubmit={submit}
          aria-label="Flatten perps"
          className="absolute right-0 top-full z-30 mt-1 w-72 rounded border border-zinc-700 bg-zinc-950 p-3 font-mono text-2xs text-zinc-300 shadow-lg"
        >
          <p className="mb-2">
            Closes {perps.map((p) => p.split(".")[0]).join(", ")} with reduce-only market orders at the live price. Spot
            holdings are not touched.
          </p>
          <label className="mb-2 flex items-center gap-2">
            TOTP code
            <input
              ref={input}
              aria-label="TOTP code"
              inputMode="numeric"
              autoComplete="one-time-code"
              maxLength={6}
              value={totp}
              onChange={(e) => setTotp(e.target.value.replace(/\D/g, ""))}
              className="w-24 rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-zinc-100"
            />
          </label>
          {error && (
            <p role="alert" className="mb-2 text-rose-300">
              {error}
            </p>
          )}
          <div className="flex gap-2">
            <button
              type="submit"
              disabled={busy || totp.length !== 6}
              className="flex-1 rounded bg-rose-600 px-2 py-1 font-bold text-white disabled:opacity-50"
            >
              {busy ? "Sending…" : `Flatten ${perps.length} perp${perps.length === 1 ? "" : "s"}`}
            </button>
            <button type="button" onClick={() => setOpen(false)} className="rounded border border-zinc-700 px-2 py-1 text-zinc-300">
              Cancel
            </button>
          </div>
        </form>
      )}
    </div>
  );
}
