"use client";

import { useEffect } from "react";
import { Shield, ShieldAlert, ShieldCheck, ShieldMinus, ShieldQuestion } from "lucide-react";
import { MANDATE_UPDATED_EVENT } from "@/lib/mission/events";
import { computeModeBanner, type ModeBannerTone } from "@/lib/mission/modeBanner";
import { useMandateStore } from "@/lib/stores/useMandateStore";

const POLL_MS = 15_000;

const TONE: Record<ModeBannerTone, { cls: string; Icon: typeof Shield }> = {
  unknown: { cls: "bg-zinc-900 border-zinc-700 text-zinc-300", Icon: ShieldQuestion },
  latched: { cls: "bg-red-950/50 border-red-800 text-red-300", Icon: ShieldAlert },
  idle: { cls: "bg-zinc-900 border-zinc-700 text-zinc-300", Icon: ShieldMinus },
  gated: { cls: "bg-cyan-950/40 border-cyan-900/60 text-cyan-300", Icon: ShieldCheck },
  acting: { cls: "bg-amber-950/40 border-amber-800/60 text-amber-300", Icon: Shield },
};

/**
 * Truthful mode banner (G1): venue, mandates by autonomy level and the kill switch, read from the
 * mandate store. Exactly one mounted instance should `poll` (the app header); others only read.
 */
export function ModeBanner({ poll = false, compact = false, className = "" }: { poll?: boolean; compact?: boolean; className?: string }) {
  const mandates = useMandateStore((s) => s.mandates);
  const status = useMandateStore((s) => s.status);
  const mandatesOk = useMandateStore((s) => s.mandatesOk);
  const statusOk = useMandateStore((s) => s.statusOk);
  const denied = useMandateStore((s) => s.denied);

  useEffect(() => {
    if (!poll) return;
    const refresh = () => void useMandateStore.getState().refresh();
    refresh();
    const t = setInterval(refresh, POLL_MS);
    window.addEventListener(MANDATE_UPDATED_EVENT, refresh);
    return () => {
      clearInterval(t);
      window.removeEventListener(MANDATE_UPDATED_EVENT, refresh);
    };
  }, [poll]);

  const banner = computeModeBanner({ mandates, status, mandatesOk, statusOk, denied });
  const { cls, Icon } = TONE[banner.tone];

  return (
    <div
      role="status"
      data-testid="mode-banner"
      data-tone={banner.tone}
      title={banner.detail}
      className={`flex min-w-0 items-center space-x-1.5 rounded border px-2 font-mono text-2xs ${compact ? "py-0.5" : "py-1"} ${cls} ${className}`}
    >
      <Icon className="h-3 w-3 flex-none" aria-hidden="true" />
      <span data-testid="mode-banner-text" className="truncate font-bold tracking-wide">{banner.text}</span>
      <span className="sr-only">. {banner.detail}</span>
    </div>
  );
}
