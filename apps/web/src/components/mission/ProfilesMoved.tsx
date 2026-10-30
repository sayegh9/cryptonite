"use client";

import { dispatchSwitchWorkspace } from "@/lib/navigation";

/** Profiles are created and edited in Auto-Trade only (its profile detail is the single editor). */
export function ProfilesMoved() {
  return (
    <section aria-label="Profiles" className="rounded-xl border border-zinc-800 bg-zinc-950 p-4 text-sm text-zinc-300">
      <p>Profiles are created and edited in Auto-Trade.</p>
      <button
        type="button"
        onClick={() => dispatchSwitchWorkspace({ mode: "autotrade", view: "list" })}
        className="mt-3 rounded-md bg-cyan-600 px-3 py-2 text-xs font-semibold text-zinc-950 hover:bg-cyan-500"
      >
        Open Auto-Trade
      </button>
    </section>
  );
}
