"use client";

import { ago, coinOf, MARKET_LABEL, MODE_LABEL, signedUsd, STATUS_LABEL } from "@/lib/autotrade/copy";
import type { Profile, ProfileStatus, ProfileSummary } from "@/lib/autotrade/types";
import { ProfileActions } from "./ProfileActions";

const STATUS_TONE: Record<ProfileStatus, string> = {
  ACTIVE: "border-emerald-700 bg-emerald-950/40 text-emerald-200",
  PAUSED: "border-amber-700 bg-amber-950/40 text-amber-200",
  HALTED: "border-red-700 bg-red-950/40 text-red-200",
  STOPPING: "border-amber-700 bg-amber-950/40 text-amber-200",
  STOPPED: "border-zinc-700 bg-zinc-900 text-zinc-300",
  ARCHIVED: "border-zinc-800 bg-zinc-900 text-zinc-500",
};

export function StatusPill({ status }: { status: ProfileStatus }) {
  return (
    <span className={`rounded-full border px-2.5 py-0.5 text-xs font-bold ${STATUS_TONE[status]}`} data-testid="profile-status">
      {STATUS_LABEL[status]}
    </span>
  );
}

/** One line on what the profile does with new entries right now (mode, master switch, dry run, lifecycle). */
export function modeLine(p: Profile, summary: ProfileSummary | null): string {
  if (p.status === "PAUSED") return p.paused_reason ? `Paused: ${p.paused_reason}` : "Paused";
  if (p.status === "HALTED") return `Halted: ${p.halted_reason ?? "reason unavailable"}. Review positions before resuming.`;
  if (p.status === "STOPPING") {
    const n = p.positions?.length ?? 0;
    return `Stopping: no new entries. ${n} open position${n === 1 ? "" : "s"} run${n === 1 ? "s" : ""} on their exits.`;
  }
  if (summary?.master_switch === "OFF" && p.execution_mode !== "ASK")
    return `Auto-trade switch off. New entries require approval. (Mode: ${MODE_LABEL[p.execution_mode]})`;
  return `Mode: ${MODE_LABEL[p.execution_mode]}`;
}

export function HealthBlock({ summary }: { summary: ProfileSummary | null }) {
  if (!summary) return <p className="text-xs text-zinc-500">Health: Unavailable</p>;
  const h = summary.health_score;
  if (h.score == null) return <p className="text-xs text-zinc-400" data-testid="profile-health">Health: Not running</p>;
  const tone = h.score >= 80 ? "text-emerald-300" : h.score >= 50 ? "text-amber-300" : "text-red-300";
  const label = h.score >= 80 ? "good" : h.score >= 50 ? "degraded" : "poor";
  const c = h.components;
  return (
    <div className="text-xs" data-testid="profile-health">
      <p>
        <span className="text-zinc-400">Health </span>
        <span className={`font-bold ${tone}`}>
          {h.score}/100 ({label})
        </span>
      </p>
      <p className="text-zinc-400">
        Market data {c.freshness.points}/40 · Recent failures {c.recent_failures.points}/30 · Breaker {c.breaker.points}/30
        {c.breaker.reason ? ` (${c.breaker.reason})` : ""}
      </p>
      <p className="text-zinc-500">Last decision {ago(summary.last_decision_at)}</p>
    </div>
  );
}

/**
 * Today's P&L on a scale from the daily loss limit (left) through zero to the target (right), from the API's values.
 * Not drawn when the loss limit is unset or unavailable.
 */
export function PnlScale({ summary, name }: { summary: ProfileSummary | null; name: string }) {
  if (!summary) return <p className="text-xs text-zinc-500">Today&apos;s P&amp;L: Unavailable</p>;
  const t = summary.today;
  const pnl = Number(t.pnl);
  const loss = t.daily_loss_limit_usd != null ? Number(t.daily_loss_limit_usd) : null;
  const target = t.daily_profit_target_usd != null ? Number(t.daily_profit_target_usd) : null;
  const text = `Today ${signedUsd(t.pnl)} (realized ${signedUsd(t.realized_pnl)}, unrealized ${signedUsd(t.unrealized_pnl)}) USD, UTC day`;
  return (
    <div className="text-xs">
      <p className="text-zinc-200">{text}</p>
      {loss != null && loss > 0 && (
        <>
          {(() => {
            // Placement only: the limit, target and P&L are the API's numbers
            const right = Math.max(target ?? 0, 0);
            const span = loss + right;
            const zeroAt = (loss / span) * 100;
            const at = ((Math.min(Math.max(pnl, -loss), right) + loss) / span) * 100;
            return (
              <div
                role="meter"
                aria-label={`Today's P&L against the daily loss limit — ${name}`}
                aria-valuemin={-loss}
                aria-valuemax={target ?? 0}
                aria-valuenow={pnl}
                aria-valuetext={`${signedUsd(pnl)} of a ${signedUsd(-loss)} daily loss limit${target != null ? ` and a ${signedUsd(target)} target` : ""}`}
                className="relative mt-1 h-2 w-full rounded bg-zinc-800"
              >
                <div className="absolute inset-y-0 w-px bg-zinc-400" style={{ left: `${zeroAt}%` }} aria-hidden="true" />
                <div className={`absolute top-1/2 h-3 w-3 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-zinc-950 ${pnl < 0 ? "bg-red-400" : "bg-emerald-400"}`} style={{ left: `${at}%` }} aria-hidden="true" />
              </div>
            );
          })()}
          <div className="mt-0.5 flex justify-between text-2xs text-zinc-500">
            <span>Loss limit {signedUsd(-loss)}</span>
            <span>{target != null ? `Target ${signedUsd(target)}` : "No target"}</span>
          </div>
          {pnl <= -loss && <p className="font-bold text-red-300">Daily loss limit reached · {summary.status}</p>}
        </>
      )}
    </div>
  );
}

export function lastActionText(summary: ProfileSummary | null): string {
  const d = summary?.last_decision;
  if (!d) return "No decisions yet.";
  const what = `${d.stage.toLowerCase()} ${d.outcome.toLowerCase().replaceAll("_", " ")}`;
  return `Last action: ${what}${d.instrument_id ? ` on ${coinOf(d.instrument_id)}` : ""} · ${ago(d.ts)}`;
}

export function ProfileCard({ profile: p, summary, onOpen }: { profile: Profile; summary: ProfileSummary | null; onOpen: () => void }) {
  const coins = Array.from(new Set(p.instruments.map(coinOf)));
  return (
    <article aria-label={`Profile ${p.name}`} className="flex flex-col gap-3 rounded-xl border border-zinc-800 bg-zinc-950 p-4" data-testid="profile-card">
      <header className="flex flex-wrap items-center gap-2">
        <h3 className="min-w-0 flex-1 truncate text-sm font-bold text-zinc-100">{p.name}</h3>
        <StatusPill status={p.status} />
        <span className={`rounded border px-1.5 py-0.5 text-2xs font-bold ${p.environment === "LIVE" ? "border-red-700 text-red-300" : "border-zinc-700 text-zinc-300"}`}>
          {p.environment}
        </span>
      </header>
      <p className="text-xs text-zinc-300" data-testid="profile-mode-line">{modeLine(p, summary)}</p>
      {p.dry_run && <p className="text-xs text-cyan-300">Dry run: orders are simulated.</p>}
      {p.environment === "LIVE" && <p className="text-xs text-red-300">Live trading is not available yet.</p>}
      <PnlScale summary={summary} name={p.name} />
      <p className="text-xs text-zinc-400">{lastActionText(summary)}</p>
      <HealthBlock summary={summary} />
      <p className="text-xs text-zinc-400">
        {p.markets.map((m) => MARKET_LABEL[m]).join(" + ")} · {coins.length} coin{coins.length === 1 ? "" : "s"} ({coins.join(", ")}) ·{" "}
        {p.style === "SWING" ? "Swing" : "Intraday"} · {p.direction === "BOTH" ? "Long and short" : p.direction === "LONG" ? "Long" : "Short"} · up to {p.max_leverage}×
      </p>
      <div className="mt-auto flex flex-wrap items-center gap-2">
        <button type="button" onClick={onOpen} className="rounded-md bg-zinc-100 px-3 py-1.5 text-xs font-semibold text-zinc-950 hover:bg-white" aria-label={`Open profile ${p.name}`}>
          Open profile
        </button>
        <ProfileActions profile={p} compact />
      </div>
    </article>
  );
}
