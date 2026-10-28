"use client";

import { Fragment, useEffect, useState } from "react";
import { Shield, ShieldAlert, ShieldCheck, ShieldMinus, ShieldQuestion } from "lucide-react";
import { MANDATE_UPDATED_EVENT } from "@/lib/mission/events";
import { computeModeBanner, type ModeBannerTone } from "@/lib/mission/modeBanner";
import { useMandateStore } from "@/lib/stores/useMandateStore";

const POLL_MS = 15_000;
/** Re-evaluates staleness between store updates */
const TICK_MS = 5_000;

// `unknown` is a warning (yellow, dashed): never the cyan ShieldCheck of a fully known L1 state
const TONE: Record<ModeBannerTone, { cls: string; Icon: typeof Shield }> = {
  pending: { cls: "bg-zinc-900 border-zinc-700 text-zinc-400", Icon: ShieldQuestion },
  unknown: { cls: "bg-yellow-950/40 border-dashed border-yellow-500/70 text-yellow-200", Icon: ShieldQuestion },
  latched: { cls: "bg-red-950/50 border-red-800 text-red-300", Icon: ShieldAlert },
  idle: { cls: "bg-zinc-900 border-zinc-700 text-zinc-300", Icon: ShieldMinus },
  gated: { cls: "bg-cyan-950/40 border-cyan-900/60 text-cyan-300", Icon: ShieldCheck },
  acting: { cls: "bg-amber-950/40 border-amber-800/60 text-amber-300", Icon: Shield },
};

const Sep = () => (
  <span aria-hidden="true" className="flex-none whitespace-pre opacity-60">
    {" · "}
  </span>
);

/**
 * Truthful mode banner (G1): venue, mandates by autonomy level and the kill switch, read from the
 * mandate store. Exactly one mounted instance should `poll` (the app header); others only read.
 * Flags (unknown, latched, halted, paused) never truncate; only the summary does (`summaryClassName`
 * caps it), and the full text is always in the tooltip. `wrapBelowMd`: on phones (no tooltip) the
 * summary wraps onto its own line instead of truncating.
 */
export function ModeBanner({
  poll = false,
  compact = false,
  wrapBelowMd = false,
  className = "",
  summaryClassName = "",
}: {
  poll?: boolean;
  compact?: boolean;
  wrapBelowMd?: boolean;
  className?: string;
  summaryClassName?: string;
}) {
  const mandates = useMandateStore((s) => s.mandates);
  const status = useMandateStore((s) => s.status);
  const mandatesOk = useMandateStore((s) => s.mandatesOk);
  const statusOk = useMandateStore((s) => s.statusOk);
  const mandatesAt = useMandateStore((s) => s.mandatesAt);
  const statusAt = useMandateStore((s) => s.statusAt);
  const denied = useMandateStore((s) => s.denied);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), TICK_MS);
    return () => clearInterval(t);
  }, []);

  useEffect(() => {
    if (!poll) return;
    const refresh = () => void useMandateStore.getState().refresh();
    // A mandate change must not be answered by a refresh that started before it
    const onUpdated = (e: Event) => void useMandateStore.getState().refresh({ after: e.timeStamp });
    const onVisible = () => document.visibilityState === "visible" && refresh();
    refresh();
    const t = setInterval(refresh, POLL_MS);
    window.addEventListener(MANDATE_UPDATED_EVENT, onUpdated);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(t);
      window.removeEventListener(MANDATE_UPDATED_EVENT, onUpdated);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [poll]);

  // A store update re-renders before the next tick; never judge fresh data against an older clock
  const clock = Math.max(now, mandatesAt ?? 0, statusAt ?? 0);
  const banner = computeModeBanner({ mandates, status, mandatesOk, statusOk, mandatesAt, statusAt, denied, now: clock });
  const { cls, Icon } = TONE[banner.tone];

  return (
    <div
      role="status"
      data-testid="mode-banner"
      data-tone={banner.tone}
      title={denied ? "Click to sign in as Operator\n" + `${banner.text}\n${banner.detail}` : `${banner.text}\n${banner.detail}`}
      onClick={
        denied
          ? () => window.dispatchEvent(new CustomEvent("cryptonite:open-login"))
          : undefined
      }
      className={`flex min-w-0 items-center gap-1.5 rounded border px-2 font-mono text-2xs ${compact ? "py-0.5" : "py-1"} ${cls} ${denied ? "cursor-pointer hover:brightness-110 transition-all" : ""} ${className}`}
    >
      <Icon className="h-3 w-3 flex-none" aria-hidden="true" />
      <span
        data-testid="mode-banner-text"
        className={`flex min-w-0 items-center font-bold tracking-wide ${wrapBelowMd ? "flex-wrap gap-y-0.5 md:flex-nowrap" : ""}`}
      >
        <span className="flex-none">{banner.venue}</span>
        {banner.flags.map((f) => (
          <Fragment key={f.label}>
            <Sep />
            <span
              data-testid="mode-banner-flag"
              data-kind={f.kind}
              className={`flex-none whitespace-nowrap rounded-sm px-1 ${f.kind === "warn" ? "bg-current/15" : "border border-current/30"}`}
            >
              {f.label}
            </span>
          </Fragment>
        ))}
        {banner.summary && (
          <>
            <Sep />
            <span data-testid="mode-banner-summary" className={`min-w-0 ${wrapBelowMd ? "md:truncate" : "truncate"} ${summaryClassName}`}>
              {banner.summary}
            </span>
          </>
        )}
      </span>
      <span className="sr-only">. {banner.detail}</span>
    </div>
  );
}
