"use client";

import React, { useState, useEffect, type CSSProperties, type FormEvent } from "react";
import { Building2, Cpu, Play, RefreshCw, Sparkles, CheckCircle2, Clock } from "lucide-react";
import { useDebateStore } from "@/lib/stores/useDebateStore";
import { useMandateStore } from "@/lib/stores/useMandateStore";
import { dispatchBrainAsk } from "@/lib/brain/events";
import { dispatchSwitchWorkspace } from "@/lib/navigation";

export interface PixelAgentDesk {
  id: string;
  name: string;
  role: string;
  rank: "Lead" | "Senior" | "Operator";
  color: string;
  defaultTask: string;
}

export interface PixelTask {
  id: string;
  title: string;
  description: string;
  assignedTo: string;
  priority: "low" | "normal" | "high";
  status: "running" | "queued" | "completed";
  result?: string;
}

const PIXEL_AGENTS: PixelAgentDesk[] = [
  {
    id: "orchestrator",
    name: "Lead Orchestrator",
    role: "Chief AI Copilot & Mandate Router",
    rank: "Lead",
    color: "#419547",
    defaultTask: "Synthesizing multi-timeframe swing briefs & routing mandate checks",
  },
  {
    id: "scout",
    name: "Market Data Scout",
    role: "L1/L2 Microstructure & Kline Ingest",
    rank: "Senior",
    color: "#38bdf8",
    defaultTask: "Streaming Binance klines & Polars momentum scan candidates",
  },
  {
    id: "debater",
    name: "Bull / Bear Debaters",
    role: "Adversarial Thesis Arena",
    rank: "Senior",
    color: "#a855f7",
    defaultTask: "Cross-examining long/short thesis R:R & invalidation levels",
  },
  {
    id: "cro",
    name: "Risk Guardian (CRO)",
    role: "GuardedOrderDispatcher & Drawdown Clamp",
    rank: "Lead",
    color: "#f59e0b",
    defaultTask: "Enforcing 25% max position sizing & daily loss circuit breakers",
  },
  {
    id: "outbox",
    name: "Execution Outbox Relay",
    role: "Transactional Outbox & Paper Engine",
    rank: "Operator",
    color: "#10b981",
    defaultTask: "Fencing approved orders to PostgreSQL outbox & Redis stream",
  },
];

interface PixelOfficeViewProps {
  selectedSymbol?: string;
  onTriggerDebate?: (symbol: string) => void;
}

export function PixelOfficeView({ selectedSymbol = "BTCUSDT", onTriggerDebate }: PixelOfficeViewProps) {
  const [selectedAgentId, setSelectedAgentId] = useState<string>("orchestrator");
  const [telemetry, setTelemetry] = useState<Record<string, { status: string; msg: string }>>({});
  const [ranks, setRanks] = useState<Record<string, "Lead" | "Senior" | "Operator">>(() =>
    Object.fromEntries(PIXEL_AGENTS.map((a) => [a.id, a.rank]))
  );
  const [tasks, setTasks] = useState<PixelTask[]>([
    {
      id: "task-swing-scan",
      title: `Multi-Timeframe Swing Audit (${selectedSymbol})`,
      description: "Evaluate 4h/1h/15m market structure, ATR stops, and mandate headroom.",
      assignedTo: "orchestrator",
      priority: "high",
      status: "running",
    },
    {
      id: "task-risk- fence",
      title: "GuardedOrderDispatcher Invariant Check",
      description: "Verify 25% single-position cap and L1 TOTP approval gate.",
      assignedTo: "cro",
      priority: "normal",
      status: "completed",
      result: "All 3 risk invariants nominal. 0 circuit breaker trips.",
    },
  ]);

  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [priority, setPriority] = useState<"low" | "normal" | "high">("normal");

  const activeDebates = useDebateStore((s) => s.activeDebates);
  const activeDebatesList = Object.values(activeDebates);
  const aiStatus = useMandateStore((s) => s.status);

  useEffect(() => {
    let unsub: (() => void) | undefined;
    import("@/lib/ws").then(({ getStreamClient }) => {
      const client = getStreamClient();
      unsub = client.subscribe((data) => {
        if (data.type === "HEARTBEAT" && data.agentId) {
          setTelemetry((prev) => ({
            ...prev,
            [data.agentId]: { status: data.status || "ONLINE", msg: data.msg || "" },
          }));
        }
      });
    });
    return () => unsub?.();
  }, []);

  const handleCreateTask = (e: FormEvent) => {
    e.preventDefault();
    const cleanTitle = title.trim();
    if (!cleanTitle) return;
    const newTask: PixelTask = {
      id: `task-${Date.now()}`,
      title: cleanTitle,
      description: description.trim() || `Assigned to ${selectedAgentId} for ${selectedSymbol}`,
      assignedTo: selectedAgentId,
      priority,
      status: "running",
    };
    setTasks((prev) => [newTask, ...prev]);
    setTitle("");
    setDescription("");
  };

  const handleDispatchToBrain = (task: PixelTask) => {
    dispatchSwitchWorkspace({ mode: "debates", view: "home" });
    setTimeout(() => {
      dispatchBrainAsk({
        prompt: `${task.title}: ${task.description}`,
        send: false,
      });
    }, 60);
  };

  const handleCompleteTask = (taskId: string) => {
    setTasks((prev) =>
      prev.map((t) =>
        t.id === taskId
          ? { ...t, status: "completed", result: `Completed by ${t.assignedTo} at ${new Date().toLocaleTimeString()}` }
          : t
      )
    );
  };

  return (
    <div
      data-testid="pixel-office-view"
      className="flex-1 h-full min-h-0 overflow-y-auto bg-background px-4 py-5 sm:px-6 text-foreground"
    >
      <div className="mx-auto max-w-7xl space-y-5">
        {/* Header */}
        <header className="flex flex-wrap items-start justify-between gap-4 rounded-2xl border border-border bg-card p-5">
          <div>
            <div className="flex items-center gap-2.5">
              <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-[#419547]/15 border border-[#419547]/40 text-[#58bd60]">
                <Building2 className="h-4 w-4" aria-hidden="true" />
              </div>
              <h1 className="text-lg font-semibold tracking-tight text-zinc-100">
                Pixel Office · Autonomous Quant Agent Floor
              </h1>
              <span className="rounded-full bg-[#419547]/20 border border-[#419547]/40 px-2.5 py-0.5 font-mono text-2xs font-bold text-[#8ed994]">
                {PIXEL_AGENTS.length} Desks Active
              </span>
            </div>
            <p className="mt-1.5 max-w-3xl text-xs text-zinc-400">
              Live visual command floor for Cryptonite&apos;s specialist agent team. Inspect real-time agent telemetry,
              assign research or risk tasks, or dispatch an adversarial debate on {selectedSymbol}.
            </p>
          </div>
          <div className="flex items-center gap-2">
            {onTriggerDebate && (
              <button
                type="button"
                onClick={() => onTriggerDebate(selectedSymbol)}
                className="flex items-center gap-1.5 rounded-lg bg-[#419547] hover:bg-[#357a3a] px-3 py-2 text-xs font-semibold text-white transition-colors shadow-xs"
              >
                <Sparkles className="h-3.5 w-3.5" aria-hidden="true" />
                <span>Dispatch {selectedSymbol} Debate</span>
              </button>
            )}
          </div>
        </header>

        {/* Pixel Office Floor */}
        <section
          className="pixel-office-floor rounded-2xl border border-border p-4 sm:p-6"
          aria-label="Quant Agent Office Floor"
        >
          <div className="mb-4 flex items-center justify-between">
            <h2 className="flex items-center gap-2 text-xs font-mono font-bold uppercase tracking-wider text-zinc-200">
              <Cpu className="h-3.5 w-3.5 text-[#58bd60]" aria-hidden="true" />
              <span>Neural Quant Floor · Click a desk to inspect or assign work</span>
            </h2>
            <span className="font-mono text-2xs text-zinc-400">
              Active Debates: {activeDebatesList.length}
            </span>
          </div>

          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
            {PIXEL_AGENTS.map((agent) => {
              const live = telemetry[agent.id];
              const agentTasks = tasks.filter((t) => t.assignedTo === agent.id && t.status !== "completed");
              const isSelected = selectedAgentId === agent.id;
              const isWorking = live?.status === "PROCESSING" || agentTasks.length > 0 || (agent.id === "debater" && activeDebatesList.length > 0);
              const isOnline =
                live?.status === "ONLINE" ||
                isWorking ||
                (aiStatus !== null
                  ? agent.id === "cro"
                    ? Boolean(aiStatus.exit_monitor_up)
                    : agent.id === "outbox"
                      ? Boolean(aiStatus.notifier_up ?? true)
                      : true
                  : true);
              const rank = ranks[agent.id] ?? agent.rank;

              return (
                <button
                  type="button"
                  key={agent.id}
                  data-testid={`pixel-desk-${agent.id}`}
                  onClick={() => setSelectedAgentId(agent.id)}
                  aria-pressed={isSelected}
                  className={`pixel-desk rounded-xl border p-4 text-left transition-all cursor-pointer ${
                    isSelected
                      ? "border-[#419547] ring-2 ring-[#419547]/25 shadow-lg"
                      : "border-zinc-800 hover:border-zinc-700"
                  }`}
                >
                  <div className="flex items-start justify-between gap-2">
                    <div
                      className="pixel-character"
                      style={{ "--pixel-color": agent.color } as CSSProperties}
                      aria-hidden="true"
                    >
                      <div className="pixel-character__shadow" />
                      <div className="pixel-character__body">
                        <span className="pixel-character__head" />
                        <span className="pixel-character__torso" />
                        <span className="pixel-character__arm pixel-character__arm--left" />
                        <span className="pixel-character__arm pixel-character__arm--right" />
                        <span className="pixel-character__leg pixel-character__leg--left" />
                        <span className="pixel-character__leg pixel-character__leg--right" />
                      </div>
                      {isWorking && <span className="pixel-character__typing">···</span>}
                    </div>
                    <span className="rounded-full bg-zinc-900/90 border border-zinc-800 px-2 py-0.5 font-mono text-2xs text-zinc-300">
                      {agentTasks.length} {agentTasks.length === 1 ? "task" : "tasks"}
                    </span>
                  </div>

                  <div className="mt-3">
                    <div className="flex items-center justify-between gap-1">
                      <p className="font-semibold text-xs text-zinc-100 truncate">{agent.name}</p>
                      <span
                        className={`inline-block h-2 w-2 rounded-full flex-none ${
                          isOnline ? "bg-[#58bd60]" : "bg-zinc-500"
                        }`}
                        title={live?.status || (isOnline ? "ONLINE" : "READY")}
                      />
                    </div>
                    <p className="text-2xs font-mono text-[#58bd60] mt-0.5">{rank} · {agent.role}</p>
                    <p className="mt-2 text-2xs text-zinc-400 line-clamp-2 leading-snug">
                      {live?.msg || agent.defaultTask}
                    </p>
                  </div>
                </button>
              );
            })}
          </div>
        </section>

        {/* Task Board + Task Creator */}
        <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_360px]">
          <section className="rounded-2xl border border-border bg-card p-5 space-y-4">
            <div className="flex items-center justify-between">
              <h2 className="text-sm font-semibold text-zinc-100">Agent Task &amp; Mandate Operations Board</h2>
              <span className="font-mono text-2xs text-zinc-400">
                {tasks.filter((t) => t.status !== "completed").length} active · {tasks.filter((t) => t.status === "completed").length} completed
              </span>
            </div>

            <div className="space-y-3">
              {tasks.map((task) => {
                const desk = PIXEL_AGENTS.find((a) => a.id === task.assignedTo);
                return (
                  <article
                    key={task.id}
                    className="rounded-xl border border-zinc-800 bg-zinc-950/60 p-4 transition-colors hover:border-zinc-700"
                  >
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div className="flex items-center gap-2">
                        {task.status === "completed" ? (
                          <CheckCircle2 className="h-4 w-4 text-[#58bd60] flex-none" aria-hidden="true" />
                        ) : (
                          <Clock className="h-4 w-4 text-amber-400 flex-none" aria-hidden="true" />
                        )}
                        <h3 className="text-xs font-semibold text-zinc-100">{task.title}</h3>
                      </div>
                      <div className="flex items-center gap-2 font-mono text-2xs">
                        <span className="rounded border border-zinc-800 bg-zinc-900 px-2 py-0.5 text-zinc-300 uppercase">
                          {task.priority}
                        </span>
                        <span
                          className={`rounded px-2 py-0.5 font-semibold uppercase ${
                            task.status === "completed"
                              ? "bg-[#419547]/20 text-[#8ed994]"
                              : "bg-amber-950/60 text-amber-300"
                          }`}
                        >
                          {task.status}
                        </span>
                      </div>
                    </div>

                    <p className="mt-1.5 text-xs text-zinc-400 leading-relaxed">{task.description}</p>

                    <div className="mt-3 flex flex-wrap items-center justify-between gap-2 pt-2 border-t border-zinc-800/80">
                      <span className="font-mono text-2xs text-[#58bd60]">
                        Desk: {desk?.name ?? task.assignedTo}
                      </span>

                      <div className="flex items-center gap-2">
                        <button
                          type="button"
                          onClick={() => handleDispatchToBrain(task)}
                          className="flex items-center gap-1 rounded-md border border-zinc-700 bg-zinc-900 hover:bg-zinc-800 px-2.5 py-1 text-2xs font-mono text-zinc-200 transition-colors"
                        >
                          <Play className="h-3 w-3 text-[#58bd60]" aria-hidden="true" />
                          <span>Open in Brain</span>
                        </button>
                        {task.status !== "completed" && (
                          <button
                            type="button"
                            onClick={() => handleCompleteTask(task.id)}
                            className="rounded-md bg-[#419547]/20 hover:bg-[#419547]/30 border border-[#419547]/40 px-2.5 py-1 text-2xs font-mono font-semibold text-[#8ed994] transition-colors"
                          >
                            Mark Done
                          </button>
                        )}
                      </div>
                    </div>

                    {task.result && (
                      <p className="mt-2 rounded-lg bg-zinc-900/80 border border-zinc-800 px-3 py-2 font-mono text-2xs text-zinc-300">
                        {task.result}
                      </p>
                    )}
                  </article>
                );
              })}
            </div>
          </section>

          {/* Right Column: Assign Task & Agent Rank Configuration */}
          <div className="space-y-4">
            <section className="rounded-2xl border border-border bg-card p-5">
              <h2 className="mb-3 text-sm font-semibold text-zinc-100">Assign Desk Task</h2>
              <form onSubmit={handleCreateTask} className="space-y-3">
                <label className="block text-2xs font-mono text-zinc-400">
                  Target Agent Desk
                  <select
                    value={selectedAgentId}
                    onChange={(e) => setSelectedAgentId(e.target.value)}
                    className="mt-1 w-full rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 text-xs text-zinc-100 focus:border-[#419547] focus:outline-none"
                  >
                    {PIXEL_AGENTS.map((a) => (
                      <option key={a.id} value={a.id}>
                        {a.name} ({ranks[a.id] ?? a.rank})
                      </option>
                    ))}
                  </select>
                </label>

                <label className="block text-2xs font-mono text-zinc-400">
                  Task Title
                  <input
                    type="text"
                    required
                    maxLength={200}
                    value={title}
                    onChange={(e) => setTitle(e.target.value)}
                    placeholder={`e.g. Scan ${selectedSymbol} 4h liquidity sweep`}
                    className="mt-1 w-full rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 text-xs text-zinc-100 placeholder:text-zinc-500 focus:border-[#419547] focus:outline-none"
                  />
                </label>

                <label className="block text-2xs font-mono text-zinc-400">
                  Instructions
                  <textarea
                    rows={3}
                    maxLength={1000}
                    value={description}
                    onChange={(e) => setDescription(e.target.value)}
                    placeholder="Specify timeframe, R:R floor, or risk constraint..."
                    className="mt-1 w-full rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 text-xs text-zinc-100 placeholder:text-zinc-500 focus:border-[#419547] focus:outline-none"
                  />
                </label>

                <label className="block text-2xs font-mono text-zinc-400">
                  Priority
                  <select
                    value={priority}
                    onChange={(e) => setPriority(e.target.value as "low" | "normal" | "high")}
                    className="mt-1 w-full rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 text-xs text-zinc-100 focus:border-[#419547] focus:outline-none"
                  >
                    <option value="low">Low</option>
                    <option value="normal">Normal</option>
                    <option value="high">High</option>
                  </select>
                </label>

                <button
                  type="submit"
                  disabled={!title.trim()}
                  className="w-full rounded-lg bg-[#419547] hover:bg-[#357a3a] px-3 py-2 text-xs font-semibold text-white transition-colors disabled:opacity-40"
                >
                  Assign to Desk
                </button>
              </form>
            </section>

            <section className="rounded-2xl border border-border bg-card p-5">
              <h2 className="mb-3 text-sm font-semibold text-zinc-100">Agent Fleet Ranks</h2>
              <div className="space-y-2.5">
                {PIXEL_AGENTS.map((agent) => (
                  <label key={agent.id} className="flex items-center justify-between gap-2 text-xs text-zinc-300">
                    <span className="truncate">{agent.name}</span>
                    <select
                      value={ranks[agent.id] ?? agent.rank}
                      onChange={(e) =>
                        setRanks((prev) => ({
                          ...prev,
                          [agent.id]: e.target.value as "Lead" | "Senior" | "Operator",
                        }))
                      }
                      aria-label={`${agent.name} rank`}
                      className="rounded-md border border-zinc-800 bg-zinc-950 px-2 py-1 font-mono text-2xs text-zinc-200 focus:border-[#419547] focus:outline-none"
                    >
                      <option value="Operator">Operator</option>
                      <option value="Senior">Senior</option>
                      <option value="Lead">Lead</option>
                    </select>
                  </label>
                ))}
              </div>
            </section>
          </div>
        </div>
      </div>
    </div>
  );
}
