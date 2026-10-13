"use client";

import { useCallback, useEffect, useState } from "react";
import { Coins, History, Landmark, RefreshCw, ShieldCheck } from "lucide-react";
import { fetchApi } from "@/lib/api";
import { useSessionStore } from "@/lib/stores/useSessionStore";
import { fetchBrainSpend, listBrainSessions, openBrainSession } from "@/lib/brain/client";
import type { BrainSessionSummary } from "@/lib/brain/types";
import { useBrainStore } from "@/lib/stores/useBrainStore";
import { listMandates } from "@/lib/mission/client";
import { staleMarkText } from "@/lib/mission/marks";
import { MANDATE_UPDATED_EVENT } from "@/lib/mission/events";
import type { Mandate } from "@/lib/mission/types";
import { fmtQuantity, isZeroQuantity } from "@/lib/format";
import { fetchSizeSteps } from "@/lib/sizeSteps";

interface RailPosition {
  instrument_id: string;
  side?: string;
  quantity?: string | number;
  unrealized_pnl?: string | number;
  status?: string;
  /** B3: the API flags a position valued at a stored mark instead of a live quote. */
  mark_stale?: boolean;
  mark_age_s?: number | null;
}

/**
 * G4: closed rows and zero-quantity rows (perp netting leaves "0E-18") are not positions. Same
 * step-independent rule as Portfolio's isHeldPosition; the step size is only used for display, so dust
 * shows as "<0.00001" instead of vanishing. A row without a quantity is shown, not hidden.
 */
const isHeld = (p: RailPosition) => p.status !== "CLOSED" && !isZeroQuantity(p.quantity);

const num = (v: unknown) => (v === null || v === undefined || v === "" ? null : Number(v));

function RailSection({ icon, title, children }: { icon: React.ReactNode; title: string; children: React.ReactNode }) {
  return (
    <section className="border-b border-zinc-800 p-3">
      <h3 className="mb-2 flex items-center gap-1.5 text-2xs font-bold uppercase tracking-wider text-zinc-400">
        {icon}
        {title}
      </h3>
      {children}
    </section>
  );
}

/** A rail request that has not answered by then is reported, not waited on */
const RAIL_TIMEOUT_MS = 10_000;

export function BrainContextRail() {
  // Re-runs the refresh when the operator session is confirmed, so a rail opened before sign-in fills in
  const session = useSessionStore((s) => s.status);
  const [positions, setPositions] = useState<RailPosition[] | null>(null);
  const [positionsError, setPositionsError] = useState<string | null>(null);
  const [spend, setSpend] = useState<{ spent: number; cap: number } | null>(null);
  const [sessions, setSessions] = useState<BrainSessionSummary[] | null>([]);
  const [mandates, setMandates] = useState<Mandate[] | null>(null);
  const [steps, setSteps] = useState<Record<string, string>>({});
  const sessionId = useBrainStore((s) => s.sessionId);
  const streaming = useBrainStore((s) => s.streaming);

  const refresh = useCallback(async () => {
    // Step sizes only refine the display: never block positions (or spend) on /instruments
    void fetchSizeSteps().then(setSteps);
    if (session === "anonymous") {
      setPositions(null);
      setPositionsError("Sign in as operator to see positions.");
    } else {
      try {
        const rows = await fetchApi("/paper/positions", { timeoutMs: RAIL_TIMEOUT_MS });
        setPositions(Array.isArray(rows) ? rows : []);
        setPositionsError(null);
      } catch (e: any) {
        setPositions(null);
        if (e?.status === 401 || e?.status === 403) {
          void useSessionStore.getState().check();
          setPositionsError("Positions were refused. Your operator session may have ended.");
        } else {
          setPositionsError(e?.timeout ? "Positions did not load in time." : "Positions unavailable.");
        }
      }
    }
    const s = await fetchBrainSpend().catch(() => null);
    setSpend(s ? { spent: Number(s.spent_usd), cap: Number(s.cap_usd) } : null);
    setSessions(await listBrainSessions({ limit: 5 }).then((p) => p.sessions).catch(() => null));
    setMandates(await listMandates().catch(() => null));
  }, [session]);

  // Mission Control edits (cryptonite:mandate-updated) show up here without a reload
  useEffect(() => {
    const onUpdated = () => void listMandates().then(setMandates).catch(() => undefined);
    window.addEventListener(MANDATE_UPDATED_EVENT, onUpdated);
    return () => window.removeEventListener(MANDATE_UPDATED_EVENT, onUpdated);
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Spend and positions change after a turn: refresh when the Brain finishes answering
  useEffect(() => {
    if (!streaming) void refresh();
  }, [streaming, refresh]);

  const held = positions?.filter(isHeld) ?? null;
  const used = spend && spend.cap > 0 ? Math.min(100, (spend.spent / spend.cap) * 100) : null;

  return (
    <aside aria-label="Brain context" className="flex h-full min-h-0 flex-col overflow-y-auto bg-zinc-950 font-mono text-2xs text-zinc-300">
      <div className="flex items-center justify-between border-b border-zinc-800 px-3 py-2">
        <span className="text-2xs font-bold uppercase tracking-wider text-zinc-500">Context</span>
        <button type="button" onClick={() => void refresh()} className="text-zinc-500 hover:text-zinc-200" aria-label="Refresh context">
          <RefreshCw className="h-3 w-3" />
        </button>
      </div>

      <RailSection icon={<Landmark className="h-3 w-3 text-cyan-400" />} title="Paper positions">
        {positionsError && <p className="text-zinc-500">{positionsError}</p>}
        {held && held.length === 0 && <p className="text-zinc-500">Flat. No open positions.</p>}
        {held && held.length > 0 && (
          <ul className="space-y-1" data-testid="brain-rail-positions">
            {held.slice(0, 8).map((p, i) => {
              const pnl = num(p.unrealized_pnl);
              return (
                <li key={`${p.instrument_id}-${i}`} className="flex justify-between gap-2">
                  <span className="truncate">
                    <span className={p.side === "SHORT" || p.side === "SELL" ? "text-rose-400" : "text-emerald-400"}>{p.side ?? ""}</span>{" "}
                    <span data-testid="brain-rail-qty">{fmtQuantity(p.quantity, steps[p.instrument_id])}</span> {p.instrument_id}
                  </span>
                  {pnl !== null && (
                    <span
                      className={p.mark_stale ? "text-zinc-500" : pnl >= 0 ? "text-emerald-400" : "text-rose-400"}
                      title={p.mark_stale ? staleMarkText(p.mark_age_s) : undefined}
                      data-testid={p.mark_stale ? "brain-rail-pnl-stale" : "brain-rail-pnl"}
                    >
                      {pnl.toFixed(2)}
                      {p.mark_stale ? " (stale)" : ""}
                    </span>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </RailSection>

      <RailSection icon={<Coins className="h-3 w-3 text-amber-400" />} title="AI spend today">
        {spend && used !== null ? (
          <div>
            <div
              role="meter"
              aria-label="AI spend today"
              aria-valuemin={0}
              aria-valuemax={spend.cap}
              aria-valuenow={spend.spent}
              aria-valuetext={`$${spend.spent.toFixed(2)} of $${spend.cap.toFixed(2)} daily cap`}
              className="h-1.5 w-full overflow-hidden rounded bg-zinc-800"
            >
              <div
                className={`h-full ${used >= 90 ? "bg-rose-500" : used >= 70 ? "bg-amber-500" : "bg-cyan-500"}`}
                style={{ width: `${used}%` }}
              />
            </div>
            <p className="mt-1 text-zinc-400" data-testid="brain-rail-spend">
              ${spend.spent.toFixed(2)} of ${spend.cap.toFixed(2)} cap
              {used >= 90 ? " · chat paused near the cap" : used >= 70 ? " · above 70%" : ""}
            </p>
          </div>
        ) : (
          <p className="text-zinc-500">Spend unavailable.</p>
        )}
      </RailSection>

      <RailSection icon={<ShieldCheck className="h-3 w-3 text-emerald-400" />} title="Mandates">
        <div className="text-zinc-400" data-testid="brain-rail-mandates">
          {mandates && mandates.length > 0 ? (
            <ul className="space-y-1">
              {mandates.map((m) => (
                <li key={m.id} className="flex justify-between gap-2">
                  <span className="truncate text-zinc-200">{m.name}</span>
                  <span className={m.status === "HALTED" ? "text-rose-400" : "text-zinc-400"}>
                    {m.level_name}
                    {m.status !== "ACTIVE" ? ` · ${m.status}` : ""}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p>
              No active mandate. Autonomy: <span className="text-zinc-100">L1 Copilot</span>. Every order the Brain proposes waits for your
              approval, inside a mandate you set in Mission Control.
            </p>
          )}
        </div>
      </RailSection>

      <RailSection icon={<History className="h-3 w-3 text-zinc-400" />} title="Conversations">
        {sessions === null ? (
          <p className="text-amber-200">Conversations could not be loaded.</p>
        ) : sessions.length === 0 ? (
          <p className="text-zinc-500">No saved conversations.</p>
        ) : (
          <ul className="space-y-1">
            {sessions.slice(0, 5).map((s) => (
              <li key={s.id}>
                <button
                  type="button"
                  disabled={streaming}
                  onClick={() => void openBrainSession(s.id)}
                  className={`w-full truncate text-left hover:text-cyan-300 ${s.id === sessionId ? "text-cyan-400" : "text-zinc-400"}`}
                  title={s.title ?? s.id}
                >
                  {s.title || "Untitled"}
                </button>
              </li>
            ))}
          </ul>
        )}
        <button
          type="button"
          onClick={() => useBrainStore.getState().setHistoryOpen(true)}
          className="mt-2 text-2xs text-cyan-400 hover:text-cyan-300"
        >
          All conversations
        </button>
      </RailSection>
    </aside>
  );
}
