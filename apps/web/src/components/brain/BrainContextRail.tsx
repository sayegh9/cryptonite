"use client";

import { useCallback, useEffect, useState } from "react";
import { Coins, History, Landmark, RefreshCw, ShieldCheck } from "lucide-react";
import { fetchApi } from "@/lib/api";
import { fetchBrainSpend, listBrainSessions, loadBrainHistory } from "@/lib/brain/client";
import type { BrainSessionSummary } from "@/lib/brain/types";
import { useBrainStore } from "@/lib/stores/useBrainStore";
import { listMandates } from "@/lib/mission/client";
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
}

/** G4: closed rows and FLAT/zero-quantity rows (perp netting leaves "0E-18") are not positions */
const isHeld = (p: RailPosition, step?: string) =>
  p.status !== "CLOSED" && p.side !== "FLAT" && !isZeroQuantity(p.quantity ?? 0, step);

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

export function BrainContextRail() {
  const [positions, setPositions] = useState<RailPosition[] | null>(null);
  const [positionsError, setPositionsError] = useState<string | null>(null);
  const [spend, setSpend] = useState<{ spent: number; cap: number } | null>(null);
  const [sessions, setSessions] = useState<BrainSessionSummary[]>([]);
  const [mandates, setMandates] = useState<Mandate[] | null>(null);
  const [steps, setSteps] = useState<Record<string, string>>({});
  const sessionId = useBrainStore((s) => s.sessionId);
  const streaming = useBrainStore((s) => s.streaming);

  const refresh = useCallback(async () => {
    try {
      const [rows, sizeSteps] = await Promise.all([fetchApi("/paper/positions"), fetchSizeSteps()]);
      setSteps(sizeSteps);
      setPositions(Array.isArray(rows) ? rows : []);
      setPositionsError(null);
    } catch (e: any) {
      setPositions(null);
      setPositionsError(e?.status === 401 || e?.status === 403 ? "Sign in as operator to see positions." : "Positions unavailable.");
    }
    const s = await fetchBrainSpend().catch(() => null);
    setSpend(s ? { spent: Number(s.spent_usd), cap: Number(s.cap_usd) } : null);
    setSessions(await listBrainSessions().catch(() => []));
    setMandates(await listMandates().catch(() => null));
  }, []);

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

  const held = positions?.filter((p) => isHeld(p, steps[p.instrument_id])) ?? null;
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
                  {pnl !== null && <span className={pnl >= 0 ? "text-emerald-400" : "text-rose-400"}>{pnl.toFixed(2)}</span>}
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
        {sessions.length === 0 ? (
          <p className="text-zinc-500">No saved conversations.</p>
        ) : (
          <ul className="space-y-1">
            {sessions.slice(0, 10).map((s) => (
              <li key={s.id}>
                <button
                  type="button"
                  disabled={streaming}
                  onClick={() => {
                    useBrainStore.getState().setSessionId(s.id);
                    void loadBrainHistory(s.id);
                  }}
                  className={`w-full truncate text-left hover:text-cyan-300 ${s.id === sessionId ? "text-cyan-400" : "text-zinc-400"}`}
                  title={s.title ?? s.id}
                >
                  {s.title || "Untitled"}
                </button>
              </li>
            ))}
          </ul>
        )}
      </RailSection>
    </aside>
  );
}
