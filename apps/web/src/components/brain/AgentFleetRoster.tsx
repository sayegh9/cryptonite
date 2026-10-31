"use client";

import React, { useState, useEffect } from "react";
import {
  BrainCircuit,
  Search,
  ShieldCheck,
  Zap,
  Sliders,
  TrendingUp,
  Cpu,
  Sparkles,
} from "lucide-react";


/** No heartbeat means the agent is not inside a debate right now: idle. */
const NO_HEARTBEAT = "Idle";
const NO_HEARTBEAT_COLOR = "text-zinc-400 bg-zinc-900 border-zinc-800";

interface AgentFleetRosterProps {
  selectedAgentId?: string;
  onSelectAgent?: (agentId: string) => void;
  activeDebateCount?: number;
}

export function AgentFleetRoster({
  selectedAgentId = "orchestrator",
  onSelectAgent,
}: AgentFleetRosterProps) {

  const [telemetry, setTelemetry] = useState<Record<string, { status: string, msg: string }>>({});

  useEffect(() => {
    let unsub: () => void;
    import("@/lib/ws").then(({ getStreamClient }) => {
      const client = getStreamClient();
      unsub = client.subscribe((data) => {
        if (data.type === "HEARTBEAT") {
          setTelemetry(prev => ({ ...prev, [data.agentId]: { status: data.status, msg: data.msg } }));
        }
      });
    });
    return () => unsub?.();
  }, []);

  const getAgentStatus = (id: string, defaultStatus: string, defaultColor: string) => {
    const live = telemetry[id];
    if (!live) return { status: defaultStatus, statusColor: defaultColor };
    
    let color = defaultColor;
    if (live.status === "PROCESSING") color = "text-amber-400 bg-amber-950/50 border-amber-800/60";
    else if (live.status === "ERROR") color = "text-rose-400 bg-rose-950/50 border-rose-800/60";
    else if (live.status === "ONLINE") color = "text-emerald-400 bg-emerald-950/50 border-emerald-800/60";
    
    const label = live.status === "PROCESSING" ? "Working" : live.status === "ERROR" ? "Error" : "Idle";
    return { status: label, statusColor: color, msg: live.msg };
  };

  const agents = [
    {
      id: "orchestrator",
      name: "Portfolio manager",
      title: "Judges each debate",
      icon: BrainCircuit,
      color: "text-cyan-400",
      bgBorder: "border-cyan-500/30 bg-cyan-950/20",
      ...getAgentStatus("orchestrator", NO_HEARTBEAT, NO_HEARTBEAT_COLOR),
      tagline: telemetry["orchestrator"]?.msg || "Decides each debate",
      desc: "Weighs the analysts, the Bull and Bear arguments and the risk panel, then decides whether to propose a trade.",
    },
    {
      id: "scout",
      name: "Analysts",
      title: "Market, flow and news",
      icon: Search,
      color: "text-blue-400",
      bgBorder: "border-blue-500/30 bg-blue-950/20",
      ...getAgentStatus("scout", NO_HEARTBEAT, NO_HEARTBEAT_COLOR),
      tagline: telemetry["scout"]?.msg || "Read the market for each debate.",
      desc: "Summarise price action, order flow and funding for the symbol under debate.",
    },
    {
      id: "debater",
      name: "Bull & Bear",
      title: "Adversarial Alpha Arena",
      icon: Sparkles,
      color: "text-purple-400",
      bgBorder: "border-purple-500/30 bg-purple-950/20",
      ...getAgentStatus("debater", NO_HEARTBEAT, NO_HEARTBEAT_COLOR),
      tagline: telemetry["debater"]?.msg || "Argue both sides.",
      desc: "Two specialized models argue opposite sides of every trade proposal. A manager model judges the winner.",
    },
    {
      id: "cro",
      name: "Risk panel",
      title: "Portfolio risk and margin",
      icon: ShieldCheck,
      color: "text-amber-400",
      bgBorder: "border-amber-500/30 bg-amber-950/20",
      ...getAgentStatus("cro", NO_HEARTBEAT, NO_HEARTBEAT_COLOR),
      tagline: telemetry["cro"]?.msg || "Reviews risk for each debate.",
      desc: "Reviews exposure, concentration and drawdown limits before a proposal reaches you.",
    },
    {
      id: "outbox",
      name: "Execution",
      title: "Order staging",
      icon: Zap,
      color: "text-emerald-400",
      bgBorder: "border-emerald-500/30 bg-emerald-950/20",
      ...getAgentStatus("outbox", NO_HEARTBEAT, NO_HEARTBEAT_COLOR),
      tagline: telemetry["outbox"]?.msg || "Stages approved orders.",
      desc: "Stages approved proposals for the execution engine; the hard risk limits still apply.",
    },
  ];
  const onlineCount = agents.filter((a) => a.status === "Working").length;

  return (
    <div className="flex flex-col h-full w-full min-h-0 bg-zinc-950 overflow-hidden">
      {/* Topology Header */}
      <div className="h-10 flex-none px-3 bg-zinc-900/60 border-b border-zinc-800 flex items-center justify-between">
        <div className="flex items-center space-x-2 text-xs font-mono font-bold text-zinc-200">
          <Cpu className="w-3.5 h-3.5 text-cyan-400" />
          <span className="uppercase tracking-wider">Agent Fleet Topology</span>
        </div>
        <div
          className={`flex items-center space-x-1 px-1.5 py-0.5 rounded border text-2xs font-mono ${
            onlineCount > 0 ? "bg-emerald-950/50 border-emerald-800/50 text-emerald-400" : "bg-zinc-900 border-zinc-800 text-zinc-400"
          }`}
          title="Agents working inside a debate right now"
        >
          <span className={`w-1.5 h-1.5 rounded-full ${onlineCount > 0 ? "bg-emerald-400" : "bg-zinc-500"}`} />
          <span>{onlineCount}/{agents.length} working</span>
        </div>
      </div>

      {/* How debates start (read-only: the pipeline has no trigger-mode setting) */}
      <div className="p-2.5 border-b border-zinc-800 bg-zinc-900/20 flex-none" data-testid="debate-trigger-info">
        <div className="text-2xs font-mono uppercase tracking-wider text-zinc-400 flex items-center space-x-1 mb-1">
          <Sliders className="w-3 h-3 text-zinc-500" aria-hidden="true" />
          <span>How debates start</span>
        </div>
        <p className="text-2xs text-zinc-400 leading-snug">
          From scanner candidates that pass the pre-debate gate (kill switch, daily loss, headroom, freshness,
          cooldown) and triage, or when you trigger one.
        </p>
      </div>

      {/* Agents Roster List */}
      <div className="flex-1 min-h-0 overflow-y-auto overflow-x-hidden p-2 space-y-2 overscroll-contain" tabIndex={0} role="region" aria-label="Agents">
        {agents.map((agent) => {
          const Icon = agent.icon;
          const isSelected = selectedAgentId === agent.id;
          return (
            <div
              key={agent.id}
              role="button"
              tabIndex={0}
              aria-pressed={isSelected}
              aria-label={`Select ${agent.name}`}
              onKeyDown={(e) => {
                if ((e.key === "Enter" || e.key === " ") && onSelectAgent) {
                  e.preventDefault();
                  onSelectAgent(agent.id);
                }
              }}
              onClick={() => {
                if (onSelectAgent) onSelectAgent(agent.id);
              }}
              className={`p-2.5 rounded-lg border transition-all cursor-pointer ${
                isSelected
                  ? `${agent.bgBorder} border-opacity-100 ring-1 ring-cyan-500/50 shadow-md`
                  : "bg-zinc-900/40 border-zinc-800/80 hover:bg-zinc-900/80 hover:border-zinc-700"
              }`}
            >
              <div className="flex items-center justify-between mb-1.5">
                <div className="flex items-center space-x-2">
                  <div className={`p-1 rounded bg-zinc-900 border border-zinc-800 ${agent.color}`}>
                    <Icon className="w-3.5 h-3.5" />
                  </div>
                  <div>
                    <div className="font-bold text-xs text-zinc-200 leading-none flex items-center space-x-1.5">
                      <span>{agent.name}</span>
                      {isSelected && (
                        <span className="px-1 py-0.2 bg-cyan-500/20 border border-cyan-500/40 text-cyan-300 text-2xs rounded font-mono font-bold">
                          SELECTED
                        </span>
                      )}
                    </div>
                    <div className="text-2xs text-zinc-400 font-mono mt-0.5">{agent.tagline}</div>
                  </div>
                </div>

                <span className={`px-1.5 py-0.5 rounded border text-2xs font-mono font-bold ${agent.statusColor}`}>
                  {agent.status}
                </span>
              </div>

              <p className="text-2xs text-zinc-400 leading-relaxed">
                {agent.desc}
              </p>
            </div>
          );
        })}
      </div>

      {/* Bottom Fleet Performance Stats */}
      <div className="flex-none p-2.5 bg-zinc-900/40 border-t border-zinc-800 font-mono text-2xs text-zinc-500">
        <span className="flex items-center space-x-1">
          <TrendingUp className="w-3 h-3 text-zinc-500" aria-hidden="true" />
          <span>Win rate, calibration and model routes: Mission Control → Autonomy &amp; Evals</span>
        </span>
      </div>
    </div>
  );
}
