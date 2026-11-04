"use client";

import { useCallback, useEffect, useState } from "react";
import { FunnelView } from "@/components/proposals/FunnelView";
import { useVisiblePoll } from "@/components/autotrade/AutoTradeWorkspace";
import { btnPrimary, btnSecondary } from "@/components/autotrade/Dialogs";
import { HIGH_LEVERAGE_CONFIRM } from "@/lib/leverage";
import {
  approveProposal,
  preflightProposal,
  rejectProposal,
  type Proposal,
  type ProposalApproval,
  type ProposalPreflight,
} from "@/lib/proposals/client";
import { useProposalStore } from "@/lib/stores/useProposalStore";

const LIST_POLL_MS = 15_000;
const PREFLIGHT_POLL_MS = 30_000;
const LEVERAGE_CHOICES = [1, 2, 3, 5, 8, 10];

const coin = (instrument: string) => instrument.split(/USDT|USDC|-|\./)[0];
const market = (instrument: string) => (instrument.includes("-PERP") ? "Perp" : "Spot");
const isPerp = (instrument: string) => instrument.includes("-PERP");
const price = (v: number | string | null | undefined) =>
  v === null || v === undefined || v === "" ? "—" : Number(v).toLocaleString("en-US", { maximumFractionDigits: 8 });

function countdown(ms: number): string {
  if (ms <= 0) return "expired";
  const s = Math.ceil(ms / 1000);
  return `expires in ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-2xs text-zinc-400">{label}</dt>
      <dd className="text-xs text-zinc-100">{children}</dd>
    </div>
  );
}

function PendingRow({ p, onDecided }: { p: Proposal; onDecided: () => void }) {
  const offset = useProposalStore((s) => s.clockOffsetMs);
  const [left, setLeft] = useState(() => new Date(p.expires_at).getTime() - Date.now() - offset);
  const [flight, setFlight] = useState<ProposalPreflight | null>(null);
  const [ack, setAck] = useState(false);
  const [leverage, setLeverage] = useState<number | null>(null);
  const [totp, setTotp] = useState("");
  const [confirmHigh, setConfirmHigh] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const perp = isPerp(p.instrument_id);

  useEffect(() => {
    const tick = () => setLeft(new Date(p.expires_at).getTime() - Date.now() - offset);
    tick();
    const id = window.setInterval(tick, 1000);
    return () => window.clearInterval(id);
  }, [p.expires_at, offset]);

  const check = useCallback(() => {
    preflightProposal(p.id, leverage).then(setFlight, () => setFlight(null));
  }, [p.id, leverage]);
  useEffect(check, [check]);
  useVisiblePoll(check, PREFLIGHT_POLL_MS);

  const plan = flight?.leverage_plan ?? null;
  const highLeverage = (plan?.leverage ?? 0) >= HIGH_LEVERAGE_CONFIRM;
  const belowFloor = flight?.meets_rr_floor === false;
  const expired = left <= 0;
  const ready = ack && !belowFloor && !expired && (!perp || totp.length === 6) && (!highLeverage || confirmHigh);

  const decide = async (act: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await act();
      onDecided();
    } catch (e) {
      setError(e instanceof Error ? e.message : "The server refused it");
      check();
    } finally {
      setBusy(false);
      setTotp(""); // a used code is never re-sent
    }
  };
  const approve = () => {
    const body: ProposalApproval = { acknowledge_against_debate: true };
    if (perp) body.totp_code = totp;
    if (perp && leverage != null) body.leverage = leverage;
    if (perp && highLeverage) body.confirm_high_leverage = confirmHigh;
    void decide(() => approveProposal(p.id, body));
  };

  const card = p.card;
  return (
    <li className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-3" data-testid="proposal-row">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <strong className="text-sm text-zinc-100">{coin(p.instrument_id)}</strong>
        <span className={p.side === "BUY" ? "text-xs font-semibold text-emerald-300" : "text-xs font-semibold text-red-300"}>
          {p.side === "BUY" ? "Long" : "Short"}
        </span>
        <span className="text-xs text-zinc-300">{market(p.instrument_id)} · {p.instrument_id}</span>
        <span className="text-xs text-zinc-300">Grade {card.setup_grade}</span>
        <span className="text-xs text-zinc-300">{[card.preset_slug, card.timeframe].filter(Boolean).join(" · ")}</span>
        <span className="ml-auto text-xs tabular-nums text-amber-300" data-testid="proposal-expiry">{countdown(left)}</span>
      </div>
      <dl className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-5">
        <Fact label="Entry">{price(card.grade_inputs.entry)}</Fact>
        <Fact label="Stop">{price(card.stop_loss)}</Fact>
        <Fact label="TP1">{price(flight?.ok ? flight.tp1?.price ?? card.take_profit : card.take_profit)}</Fact>
        <Fact label="Target">{price(card.take_profit)}</Fact>
        <Fact label="Live R:R">
          <span data-testid="proposal-rr">{flight?.live_rr != null ? `${flight.live_rr.toFixed(2)}x` : "unknown"}</span>
        </Fact>
      </dl>
      {card.spot_levels && card.perp_shift != null && (
        <p className="mt-2 text-xs text-zinc-300" data-testid="proposal-perp-shift">
          Levels moved to the perp price by {card.perp_shift > 0 ? "+" : ""}{price(card.perp_shift)}
        </p>
      )}
      {belowFloor && (
        <p className="mt-2 text-xs font-semibold text-red-300" data-testid="proposal-floor">
          Below the {flight?.rr_floor ?? 1.5} floor, cannot be approved.
        </p>
      )}
      <p className="mt-2 text-xs text-zinc-300">
        <span className="font-semibold text-amber-300">The debate said {card.against_debate_verdict}.</span>{" "}
        {card.debate_reasoning}
      </p>
      {flight?.ok === false && !belowFloor && (
        <p className="mt-2 text-xs text-amber-300" data-testid="proposal-refusal">Approve would be refused: {flight.detail}</p>
      )}
      {flight?.ok && (
        <p className="mt-2 text-xs text-zinc-300">Approving queues: {flight.summary}</p>
      )}

      <div className="mt-3 flex flex-wrap items-end gap-3">
        <label className="flex items-center gap-2 text-xs text-zinc-200">
          <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
          The debate said HOLD
        </label>
        {perp && (
          <>
            <label className="flex flex-col gap-1 text-2xs text-zinc-300">
              Leverage (at most)
              <select
                className="rounded border border-zinc-700 bg-zinc-950 px-2 py-1 text-xs text-zinc-100"
                value={leverage ?? ""}
                onChange={(e) => setLeverage(e.target.value ? Number(e.target.value) : null)}
              >
                <option value="">Highest allowed{plan?.ok ? ` (${plan.leverage}x)` : ""}</option>
                {LEVERAGE_CHOICES.map((l) => <option key={l} value={l}>{l}x</option>)}
              </select>
            </label>
            <label className="flex flex-col gap-1 text-2xs text-zinc-300">
              TOTP code
              <input
                className="w-24 rounded border border-zinc-700 bg-zinc-950 px-2 py-1 text-xs text-zinc-100"
                inputMode="numeric"
                autoComplete="one-time-code"
                maxLength={6}
                value={totp}
                onChange={(e) => setTotp(e.target.value.replace(/\D/g, ""))}
              />
            </label>
            {highLeverage && (
              <label className="flex items-center gap-2 text-xs text-zinc-200">
                <input type="checkbox" checked={confirmHigh} onChange={(e) => setConfirmHigh(e.target.checked)} />
                Confirm {plan?.leverage}x
              </label>
            )}
          </>
        )}
        <div className="ml-auto flex gap-2">
          <button type="button" className={btnPrimary} disabled={busy || !ready} onClick={approve}>
            Approve {coin(p.instrument_id)}
          </button>
          <button type="button" className={btnSecondary} disabled={busy} onClick={() => void decide(() => rejectProposal(p.id))}>
            Reject {coin(p.instrument_id)}
          </button>
        </div>
      </div>
      {error && <p role="alert" className="mt-2 text-xs text-red-300">{error}</p>}
    </li>
  );
}

export function SetupProposals() {
  const proposals = useProposalStore((s) => s.proposals);
  const pending = useProposalStore((s) => s.pending);
  const loaded = useProposalStore((s) => s.loaded);
  const error = useProposalStore((s) => s.error);
  const showRecent = useProposalStore((s) => s.showRecent);
  const setShowRecent = useProposalStore((s) => s.setShowRecent);
  const load = useProposalStore((s) => s.load);
  const [view, setView] = useState<"review" | "funnel">("review");

  useEffect(() => {
    void useProposalStore.persist.rehydrate();
  }, []);
  useVisiblePoll(() => void load(), LIST_POLL_MS);
  useEffect(() => {
    void load();
  }, [load]);

  const open = proposals.filter((p) => p.status === "PENDING");
  const decided = proposals.filter((p) => p.status !== "PENDING");

  return (
    <section aria-labelledby="proposals-heading" className="mb-6 space-y-2" data-testid="setup-proposals">
      <header className="flex flex-wrap items-center gap-2">
        <h2 id="proposals-heading" className="text-sm font-bold text-zinc-100">Setups to review</h2>
        <span className="rounded-full bg-zinc-800 px-2 py-0.5 text-xs font-semibold text-zinc-100" data-testid="proposal-count">
          {loaded ? pending : "?"}
        </span>
        {view === "review" && decided.length > 0 && (
          <button type="button" className="ml-auto text-xs text-cyan-300 underline" onClick={() => setShowRecent(!showRecent)}>
            {showRecent ? "Hide" : "Show"} decided ({decided.length})
          </button>
        )}
      </header>
      <div className="flex flex-wrap gap-2" role="group" aria-label="Setups view">
        {([["review", "To review"], ["funnel", "What happened to setups"]] as const).map(([id, label]) => (
          <button
            key={id}
            type="button"
            aria-pressed={view === id}
            className={`rounded border px-2 py-1 text-xs ${view === id ? "border-cyan-400 text-cyan-200" : "border-zinc-700 text-zinc-300"}`}
            onClick={() => setView(id)}
          >
            {label}
          </button>
        ))}
      </div>
      {view === "funnel" ? <FunnelView /> : (
        <>
          <p className="text-xs text-zinc-300">
            Setups the scanner found and the debate said HOLD on. Nothing trades unless you approve; unanswered setups expire.
          </p>
          {error && !loaded && <p className="text-xs text-amber-300" data-testid="proposal-error">Could not load the setups: {error}</p>}
          {loaded && open.length === 0 && <p className="text-xs text-zinc-400">No setups are waiting.</p>}
          <ul className="space-y-2">
            {open.map((p) => <PendingRow key={p.id} p={p} onDecided={() => void load()} />)}
          </ul>
          {showRecent && decided.length > 0 && (
            <ul className="space-y-1" data-testid="proposal-decided">
              {decided.map((p) => (
                <li key={p.id} className="flex flex-wrap gap-x-3 text-xs text-zinc-300">
                  <span className="font-semibold text-zinc-100">{coin(p.instrument_id)} {p.side === "BUY" ? "Long" : "Short"}</span>
                  <span>{p.status}</span>
                  {p.command_id && <span>order {p.command_id.slice(0, 8)}</span>}
                  {p.refusal && <span>last refusal: {p.refusal}</span>}
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  );
}
