"use client";

import { useEffect, useState } from "react";
import { Activity } from "lucide-react";
import type { AiStatus } from "@/lib/mission/types";
import { fetchBrainSpend } from "@/lib/brain/client";
import { ageLabel, freshness, metricOrNull } from "@/lib/freshness";

type Health = "up" | "down" | "unknown";

const DOT: Record<Health, string> = { up: "bg-emerald-400", down: "bg-red-500", unknown: "bg-zinc-500" };
const TEXT: Record<Health, string> = { up: "text-emerald-300", down: "text-red-300", unknown: "text-zinc-400" };

/** Sentiment is considered down when its last successful call is older than this. */
const SENTIMENT_MAX_AGE_MS = 30 * 60_000;

function Tile({ name, health, detail, testId }: { name: string; health: Health; detail: string; testId: string }) {
  return (
    <div className="rounded border border-zinc-800 bg-zinc-950 px-2.5 py-2" data-testid={testId} data-health={health}>
      <div className="flex items-center gap-1.5 text-2xs font-bold text-zinc-200">
        <span className={`h-2 w-2 rounded-full ${DOT[health]}`} aria-hidden="true" />
        {name}
      </div>
      <div className={`mt-0.5 text-2xs ${TEXT[health]}`}>{detail}</div>
    </div>
  );
}

/**
 * Mission Control landing health (R6.3): heartbeats and today's AI spend. A heartbeat the API does not
 * report is "not reported", never assumed up.
 */
export function MissionHealth({ status }: { status: AiStatus | null }) {
  const [spend, setSpend] = useState<{ spent: number; cap: number } | null>(null);
  const [spendError, setSpendError] = useState(false);

  useEffect(() => {
    let active = true;
    const load = () =>
      fetchBrainSpend()
        .then((s) => {
          if (!active) return;
          const spent = metricOrNull(s?.spent_usd);
          const cap = metricOrNull(s?.cap_usd);
          if (spent === null || cap === null) {
            setSpend(null);
            setSpendError(true);
          } else {
            setSpend({ spent, cap });
            setSpendError(false);
          }
        })
        .catch(() => active && setSpendError(true));
    void load();
    const t = setInterval(load, 60_000);
    return () => {
      active = false;
      clearInterval(t);
    };
  }, []);

  const flag = (v: boolean | null | undefined): Health => (status === null || v === null || v === undefined ? "unknown" : v ? "up" : "down");
  const tier = status?.local_tier;
  const tierHealth: Health = status === null || !tier ? "unknown" : tier.healthy ? "up" : "down";
  const sentimentAt = status?.sentiment_last_ok ?? null;
  const sentimentState = freshness(sentimentAt, SENTIMENT_MAX_AGE_MS);
  const sentimentHealth: Health = sentimentState === "unknown" ? "unknown" : sentimentState === "live" ? "up" : "down";
  const noStatus = "status unavailable";

  return (
    <section aria-label="System health" className="rounded border border-zinc-800 bg-zinc-900/30 p-3" data-testid="mission-health">
      <h3 className="mb-2 flex items-center gap-1.5 text-2xs font-bold uppercase tracking-wider text-zinc-400">
        <Activity className="h-3.5 w-3.5" aria-hidden="true" /> Health
      </h3>
      <div className="grid grid-cols-2 gap-2 md:grid-cols-3 xl:grid-cols-6">
        <Tile testId="health-exit-monitor" name="Exit monitor" health={flag(status?.exit_monitor_up)}
          detail={status === null ? noStatus : status.exit_monitor_up ? "heartbeat OK" : "no heartbeat"} />
        <Tile testId="health-notifier" name="Notifier" health={flag(status?.notifier_up)}
          detail={status === null ? noStatus : status.notifier_up ? "heartbeat OK" : "no heartbeat"} />
        <Tile testId="health-learner" name="Learner" health={flag(status?.learner_up)}
          detail={status === null ? noStatus : status.learner_up === undefined || status.learner_up === null ? "not reported" : status.learner_up ? "heartbeat OK" : "no heartbeat"} />
        <Tile testId="health-local-tier" name="Local tier" health={tierHealth}
          detail={status === null ? noStatus : !tier ? "not probed yet" : tier.healthy ? `${tier.models.length} model(s)` : tier.error || "unhealthy"} />
        <Tile testId="health-sentiment" name="Sentiment" health={sentimentHealth}
          detail={status === null ? noStatus : sentimentAt === null ? "not reported" : `last OK ${ageLabel(sentimentAt)}`} />
        <div className="rounded border border-zinc-800 bg-zinc-950 px-2.5 py-2" data-testid="health-ai-spend">
          <div className="text-2xs font-bold text-zinc-200">AI spend today</div>
          {spend ? (
            <>
              <div className="mt-0.5 text-2xs tabular-nums text-zinc-200">
                ${spend.spent.toFixed(2)} / ${spend.cap.toFixed(2)}
              </div>
              <div
                role="meter"
                aria-label="AI spend vs daily budget"
                aria-valuemin={0}
                aria-valuemax={spend.cap}
                aria-valuenow={spend.spent}
                className="mt-1 h-1.5 overflow-hidden rounded bg-zinc-800"
              >
                <div
                  className={`h-full ${spend.spent / spend.cap >= 0.7 ? "bg-amber-400" : "bg-cyan-500"}`}
                  style={{ width: `${Math.min(100, (spend.spent / spend.cap) * 100)}%` }}
                />
              </div>
            </>
          ) : (
            <div className="mt-0.5 text-2xs text-zinc-400">{spendError ? "unavailable" : "loading…"}</div>
          )}
        </div>
      </div>
    </section>
  );
}
