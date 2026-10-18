"use client";

import React from "react";
import { Bell, Send, Radio, Smartphone, ExternalLink, Activity } from "lucide-react";
import { dispatchSwitchWorkspace } from "@/lib/navigation";
import { Button } from "@/components/ui/button";
import { SettingCard, type RegistryEntry } from "../controls/SettingCard";

export interface AlertsRoutingSectionProps {
  entries: RegistryEntry[];
  onReload: () => Promise<void>;
  highlightedKey?: string;
}

export const AlertsRoutingSection: React.FC<AlertsRoutingSectionProps> = ({
  entries,
  onReload,
  highlightedKey,
}) => {
  const heartbeatEntry = entries.find((e) => e.key === "alerts.heartbeat_grace_s");
  const sentimentEntry = entries.find((e) => e.key === "alerts.sentiment_stale_s");
  const routingEntry = entries.find((e) => e.key === "alerts.routing");

  return (
    <div className="space-y-8 max-w-5xl">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-4 border-b border-white/10 pb-4">
        <div>
          <h2 className="text-xl font-bold tracking-tight text-white flex items-center gap-2">
            <Bell className="w-5 h-5 text-cyan-400" aria-hidden="true" />
            Alerts & Notification Channels
          </h2>
          <p className="text-xs text-zinc-400 mt-1 leading-relaxed">
            Heartbeat timeout thresholds, sentiment staleness grace periods, and dispatch routing across mobile push, Telegram, and in-app feeds.
          </p>
        </div>
        <Button
          type="button"
          onClick={() => dispatchSwitchWorkspace({ mode: "alerts" })}
          className="bg-cyan-600 hover:bg-cyan-500 text-zinc-950 font-bold text-xs gap-1.5"
        >
          <ExternalLink className="w-3.5 h-3.5" aria-hidden="true" />
          Open Price Alerts Workspace
        </Button>
      </div>

      {/* Threshold Setting Cards */}
      <section className="space-y-4">
        <div>
          <h3 className="text-sm font-bold uppercase tracking-wider text-cyan-400 flex items-center gap-2">
            <Activity className="w-4 h-4" aria-hidden="true" />
            Operational Alert Thresholds (ADR 0004)
          </h3>
          <p className="text-xs text-zinc-400 mt-0.5">
            Grace windows before automated ops-monitor alerts are dispatched. Lower values trigger alerts sooner.
          </p>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {heartbeatEntry && (
            <SettingCard
              entry={heartbeatEntry}
              allEntries={entries}
              onSaved={() => void onReload()}
              highlight={highlightedKey === "alerts.heartbeat_grace_s"}
            />
          )}

          {sentimentEntry && (
            <SettingCard
              entry={sentimentEntry}
              allEntries={entries}
              onSaved={() => void onReload()}
              highlight={highlightedKey === "alerts.sentiment_stale_s"}
            />
          )}
        </div>
      </section>

      {/* Dispatch Channels and Routing Policy */}
      <section className="space-y-4">
        <div>
          <h3 className="text-sm font-bold uppercase tracking-wider text-purple-400 flex items-center gap-2">
            <Radio className="w-4 h-4" aria-hidden="true" />
            Notification Delivery Channels
          </h3>
          <p className="text-xs text-zinc-400 mt-0.5">
            Severity-based routing matrix configured in <span className="font-mono text-zinc-300">notifier/bot.py</span>.
          </p>
        </div>

        {routingEntry && (
          <SettingCard
            entry={routingEntry}
            allEntries={entries}
            onSaved={() => void onReload()}
            highlight={highlightedKey === "alerts.routing"}
          />
        )}

        {/* Visual Channel Cards */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          {/* Channel 1: Telegram */}
          <div className="rounded-xl border border-white/10 bg-zinc-950/60 p-4 space-y-3">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2 text-sky-400 font-semibold text-xs">
                <Send className="w-4 h-4" aria-hidden="true" />
                Telegram Bot
              </div>
              <span className="text-[10px] font-mono px-1.5 py-0.5 rounded border border-rose-900/60 bg-rose-950/40 text-rose-300 font-bold">
                CRITICAL ONLY
              </span>
            </div>
            <p className="text-xs text-zinc-400 leading-relaxed">
              Dispatches emergency liquidation risk breaches, unexpected container restarts, and circuit-breaker kill switch triggers to the operator channel.
            </p>
            <div className="pt-2 border-t border-white/5 text-[10px] text-zinc-400 font-mono">
              Configured via <span className="text-zinc-300">TELEGRAM_BOT_TOKEN</span>
            </div>
          </div>

          {/* Channel 2: ntfy.sh */}
          <div className="rounded-xl border border-white/10 bg-zinc-950/60 p-4 space-y-3">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2 text-emerald-400 font-semibold text-xs">
                <Smartphone className="w-4 h-4" aria-hidden="true" />
                ntfy.sh Mobile Push
              </div>
              <span className="text-[10px] font-mono px-1.5 py-0.5 rounded border border-rose-900/60 bg-rose-950/40 text-rose-300 font-bold">
                CRITICAL ONLY
              </span>
            </div>
            <p className="text-xs text-zinc-400 leading-relaxed">
              High-priority instant mobile notification push bypassing silent mode on iOS and Android devices for catastrophic market dislocation alerts.
            </p>
            <div className="pt-2 border-t border-white/5 text-[10px] text-zinc-400 font-mono">
              Topic: <span className="text-zinc-300">cryptonite-alerts</span>
            </div>
          </div>

          {/* Channel 3: In-App Stream */}
          <div className="rounded-xl border border-white/10 bg-zinc-950/60 p-4 space-y-3">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2 text-cyan-400 font-semibold text-xs">
                <Radio className="w-4 h-4" aria-hidden="true" />
                In-App Redis Stream
              </div>
              <span className="text-[10px] font-mono px-1.5 py-0.5 rounded border border-cyan-900/60 bg-cyan-950/40 text-cyan-300 font-bold">
                ALL SEVERITIES
              </span>
            </div>
            <p className="text-xs text-zinc-400 leading-relaxed">
              Live WebSocket / SSE event hub delivering real-time price alerts, trade fill confirmations, and AI consensus verdict updates directly into the UI.
            </p>
            <div className="pt-2 border-t border-white/5 text-[10px] text-zinc-400 font-mono">
              Channel: <span className="text-zinc-300">stream_hub:events</span>
            </div>
          </div>
        </div>
      </section>
    </div>
  );
};
