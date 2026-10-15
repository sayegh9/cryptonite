"use client";

import React, { useEffect, useState } from "react";
import { fetchApi } from "@/lib/api";
import { Save, Cpu, ShieldAlert, Key, SlidersHorizontal } from "lucide-react";
import { toast } from "sonner";
import { Button } from "./ui/button";
import { OperatorLoginModal } from "@/components/operator/OperatorLoginModal";
import { SettingsRegistry } from "./SettingsRegistry";
import { useSessionStore } from "@/lib/stores/useSessionStore";
import { useSlowFlag } from "@/lib/hooks/useSlowFlag";

/** A settings request that has not answered by then is reported, not waited on forever */
const SETTINGS_TIMEOUT_MS = 10_000;

interface AgentConfig {
  model_name: string | null;
  temperature: number | null;
  system_instructions: string | null;
}

interface EnvHealth {
  gemini_key_set: boolean;
  binance_key_set: boolean;
  environment_mode: string;
  kill_switch_active: boolean;
}

export const SettingsPanel: React.FC = () => {
  const [activeTab, setActiveTab] = useState<"agents" | "security" | "platform">("agents");
  const [agents, setAgents] = useState<Record<string, AgentConfig>>({});
  const [selectedAgent, setSelectedAgent] = useState<string | null>(null);
  const [envHealth, setEnvHealth] = useState<EnvHealth | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [showLoginModal, setShowLoginModal] = useState(false);
  const session = useSessionStore((s) => s.status);
  const agentsSlow = useSlowFlag(loading && Object.keys(agents).length === 0);
  
  // Edit state
  const [editModel, setEditModel] = useState<string>("");
  const [editTemp, setEditTemp] = useState<number>(0.7);
  const [editPrompt, setEditPrompt] = useState<string>("");

  /** Runs only with a confirmed operator session. A refusal is the session's to show, not a guess made here. */
  const loadData = async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const [agentData, envData] = await Promise.all([
        fetchApi("/settings/agents", { timeoutMs: SETTINGS_TIMEOUT_MS }),
        fetchApi("/settings/environment", { timeoutMs: SETTINGS_TIMEOUT_MS }),
      ]);
      setAgents(agentData ?? {});
      setEnvHealth(envData);
      if (!selectedAgent && Object.keys(agentData ?? {}).length > 0) {
        handleSelectAgent("research", agentData);
      }
    } catch (err: any) {
      if (err?.status === 401 || err?.status === 403) {
        // Ask /auth/me whether the session is really gone; if it is, the sign-in gate shows
        void useSessionStore.getState().check();
        setLoadError("The settings were refused. Your operator session may have ended.");
      } else {
        setLoadError(err?.timeout ? "The request timed out." : err?.message || "The settings could not be loaded.");
      }
    } finally {
      setLoading(false);
    }
  };

  // Load unless the session is known to be signed out; a refusal turns the session anonymous, which shows the gate
  useEffect(() => {
    if (session !== "anonymous") void loadData();
  }, [session]);

  const handleSelectAgent = (name: string, data = agents) => {
    setSelectedAgent(name);
    const cfg = data[name] || {};
    setEditModel(cfg.model_name || "gemini-1.5-pro");
    setEditTemp(cfg.temperature ?? 0.7);
    setEditPrompt(cfg.system_instructions || "");
  };

  const handleSaveAgent = async () => {
    if (!selectedAgent) return;
    try {
      await fetchApi(`/settings/agents/${selectedAgent}`, {
        method: "PATCH",
        headers: {
          "X-CSRF-Token": localStorage.getItem("csrf_token") || "",
        },
        body: JSON.stringify({
          model_name: editModel,
          temperature: editTemp,
          system_instructions: editPrompt,
        })
      });
      toast.success(`${selectedAgent} config saved!`);
      loadData();
    } catch (err: any) {
      if (err?.status === 401 || err?.status === 403) {
        void useSessionStore.getState().check();
        toast.error("The save was refused. Your operator session may have ended.");
      } else {
        toast.error(err?.message || "Failed to save config");
      }
    }
  };

  if (session === "anonymous") {
    return (
      <div className="flex flex-col items-center justify-center h-full min-h-[450px] bg-zinc-950 text-white rounded-lg border border-white/10 p-8">
        <div className="max-w-md w-full flex flex-col items-center text-center space-y-4">
          <div className="p-4 bg-cyan-950/40 border border-cyan-900/60 rounded-full">
            <ShieldAlert className="w-10 h-10 text-cyan-400" />
          </div>
          <div>
            <h2 className="text-xl font-bold text-zinc-100 uppercase tracking-widest font-mono">
              Operator Access Required
            </h2>
            <p className="text-xs text-zinc-400 mt-2 leading-relaxed">
              Viewing system health, environment keys, and modifying AI agent configurations requires an active operator session.
            </p>
          </div>
          <Button
            onClick={() => setShowLoginModal(true)}
            className="w-full bg-cyan-600 hover:bg-cyan-500 text-zinc-950 font-bold uppercase tracking-wider text-xs py-3 mt-4"
          >
            Authenticate as Operator
          </Button>
        </div>

        {showLoginModal && (
          <OperatorLoginModal
            onSuccess={() => setShowLoginModal(false)}
            onClose={() => setShowLoginModal(false)}
          />
        )}
      </div>
    );
  }

  return (
    <div className="flex h-full bg-zinc-950 text-white rounded-lg border border-white/10 overflow-hidden">
      {/* Sidebar */}
      <div className="w-64 border-r border-white/10 bg-zinc-900/30 flex flex-col">
        <div className="p-4 border-b border-white/10 font-bold text-lg text-cyan-400">
          System Settings
        </div>
        <div className="flex-1 p-2 space-y-1">
          <button
            onClick={() => setActiveTab("agents")}
            className={`w-full flex items-center gap-3 px-3 py-2 rounded-md transition-colors ${
              activeTab === "agents" ? "bg-cyan-900/40 text-cyan-400" : "hover:bg-white/5"
            }`}
          >
            <Cpu className="w-4 h-4" />
            AI Agents
          </button>
          <button
            onClick={() => setActiveTab("security")}
            className={`w-full flex items-center gap-3 px-3 py-2 rounded-md transition-colors ${
              activeTab === "security" ? "bg-cyan-900/40 text-cyan-400" : "hover:bg-white/5"
            }`}
          >
            <ShieldAlert className="w-4 h-4" />
            Security & Env
          </button>
          <button
            onClick={() => setActiveTab("platform")}
            className={`w-full flex items-center gap-3 px-3 py-2 rounded-md transition-colors ${
              activeTab === "platform" ? "bg-cyan-900/40 text-cyan-400" : "hover:bg-white/5"
            }`}
          >
            <SlidersHorizontal className="w-4 h-4" />
            Platform
          </button>
        </div>
      </div>

      {/* Content */}
      <div className="flex-1 flex flex-col min-h-0">
        {activeTab === "agents" && (
          <div className="flex flex-1 min-h-0">
            {/* Agent List */}
            <div className="w-48 border-r border-white/10 p-2 space-y-1">
              {["research", "explainer", "generator", "hunter", "strategist"].map((agentName) => (
                <button
                  key={agentName}
                  onClick={() => handleSelectAgent(agentName)}
                  className={`w-full text-left px-3 py-2 rounded-md text-sm capitalize ${
                    selectedAgent === agentName ? "bg-zinc-800 text-white" : "text-zinc-400 hover:bg-white/5"
                  }`}
                >
                  {agentName}
                </button>
              ))}
            </div>
            
            {/* Editor: only for a configuration that actually loaded (no default values shown as settings) */}
            {!selectedAgent || !agents[selectedAgent] ? (
              <div className="flex-1 p-6 text-sm text-zinc-400 space-y-3" role="status" data-testid="agent-config-unavailable">
                {loading && Object.keys(agents).length === 0 ? (
                  <p>{agentsSlow ? "Still loading agent configurations… this is taking longer than usual." : "Loading agent configurations…"}</p>
                ) : loadError ? (
                  <>
                    <p className="text-amber-200">Could not load agent configurations: {loadError}</p>
                    <Button onClick={() => void loadData()} className="bg-zinc-800 hover:bg-zinc-700 text-zinc-100 text-xs uppercase tracking-wider">
                      Retry
                    </Button>
                  </>
                ) : Object.keys(agents).length === 0 ? (
                  <>
                    <p>The API returned no agent configurations, so there is nothing to edit yet.</p>
                    <Button onClick={() => void loadData()} className="bg-zinc-800 hover:bg-zinc-700 text-zinc-100 text-xs uppercase tracking-wider">
                      Check again
                    </Button>
                  </>
                ) : (
                  <p>Select an agent to edit its configuration.</p>
                )}
              </div>
            ) : (
            <div className="flex-1 p-6 flex flex-col gap-6 overflow-y-auto">
              <div>
                <h2 className="text-xl font-bold capitalize text-white mb-1">{selectedAgent} Agent</h2>
                <p className="text-sm text-zinc-400">Configure LLM parameters and system prompts for this agent.</p>
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div className="space-y-2">
                  <label className="text-xs font-semibold text-zinc-400 uppercase tracking-wider">Model</label>
                  <select
                    aria-label="Model"
                    value={editModel}
                    onChange={(e) => setEditModel(e.target.value)}
                    className="w-full bg-zinc-900 border border-zinc-800 rounded-md p-2 text-sm text-white"
                  >
                    <option value="gemini-1.5-pro">Gemini 1.5 Pro</option>
                    <option value="gemini-1.5-flash">Gemini 1.5 Flash</option>
                    <option value="gemini-exp-1121">Gemini Exp 1121</option>
                  </select>
                </div>
                <div className="space-y-2">
                  <label className="text-xs font-semibold text-zinc-400 uppercase tracking-wider">
                    Temperature ({editTemp})
                  </label>
                  <input
                    type="range"
                    aria-label="Temperature"
                    min="0"
                    max="1"
                    step="0.1"
                    value={editTemp}
                    onChange={(e) => setEditTemp(parseFloat(e.target.value))}
                    className="w-full mt-2"
                  />
                </div>
              </div>

              <div className="flex-1 flex flex-col space-y-2 min-h-[300px]">
                <label className="text-xs font-semibold text-zinc-400 uppercase tracking-wider">
                  System Instructions
                </label>
                <textarea
                  aria-label="System prompt"
                  value={editPrompt}
                  onChange={(e) => setEditPrompt(e.target.value)}
                  className="flex-1 w-full bg-zinc-900 border border-zinc-800 rounded-md p-3 text-sm text-zinc-300 font-mono resize-none focus:border-cyan-500 focus:outline-none"
                  placeholder="Enter the agent's base system prompt here..."
                />
              </div>

              <div className="flex justify-end pt-2">
                <Button onClick={handleSaveAgent} className="bg-cyan-600 hover:bg-cyan-500 text-zinc-950 gap-2">
                  <Save className="w-4 h-4" />
                  Save Configuration
                </Button>
              </div>
            </div>
            )}
          </div>
        )}

        {activeTab === "security" && !envHealth && (
          <div role="status" className="p-6 text-sm text-zinc-400 space-y-3 max-w-2xl">
            {loading ? (
              <p>Loading environment health…</p>
            ) : (
              <>
                <p className={loadError ? "text-amber-200" : undefined}>
                  {loadError ? `Could not load environment health: ${loadError}` : "Environment health has not been loaded yet."}
                </p>
                <Button onClick={() => void loadData()} className="bg-zinc-800 hover:bg-zinc-700 text-zinc-100 text-xs uppercase tracking-wider">
                  Retry
                </Button>
              </>
            )}
          </div>
        )}

        {activeTab === "security" && envHealth && (
          <div className="p-6 space-y-6 max-w-2xl">
            <div>
              <h2 className="text-xl font-bold text-white mb-1">Environment Health</h2>
              <p className="text-sm text-zinc-400">Read-only view of `.env` configurations and system statuses.</p>
            </div>

            <div className="space-y-4">
              <div className="flex items-center justify-between p-4 bg-zinc-900/50 border border-zinc-800 rounded-lg">
                <div className="flex items-center gap-3">
                  <Key className="w-5 h-5 text-cyan-400" />
                  <div>
                    <div className="font-medium">Gemini API Key</div>
                    <div className="text-xs text-zinc-500">Required for the AI gateway (Gemini models)</div>
                  </div>
                </div>
                <div className={`px-3 py-1 rounded-full text-xs font-bold ${envHealth.gemini_key_set ? 'bg-green-900/30 text-green-400 border border-green-900/50' : 'bg-red-900/30 text-red-400 border border-red-900/50'}`}>
                  {envHealth.gemini_key_set ? "CONFIGURED" : "MISSING"}
                </div>
              </div>

              <div className="flex items-center justify-between p-4 bg-zinc-900/50 border border-zinc-800 rounded-lg">
                <div className="flex items-center gap-3">
                  <Key className="w-5 h-5 text-cyan-400" />
                  <div>
                    <div className="font-medium">Binance Demo Key</div>
                    <div className="text-xs text-zinc-500">Required for execution routing</div>
                  </div>
                </div>
                <div className={`px-3 py-1 rounded-full text-xs font-bold ${envHealth.binance_key_set ? 'bg-green-900/30 text-green-400 border border-green-900/50' : 'bg-amber-900/30 text-amber-400 border border-amber-900/50'}`}>
                  {envHealth.binance_key_set ? "CONFIGURED" : "MISSING"}
                </div>
              </div>

              <div className="flex items-center justify-between p-4 bg-zinc-900/50 border border-zinc-800 rounded-lg">
                <div className="flex items-center gap-3">
                  <ShieldAlert className="w-5 h-5 text-purple-400" />
                  <div>
                    <div className="font-medium">Execution Mode</div>
                    <div className="text-xs text-zinc-500">DEMO vs PROD live trading</div>
                  </div>
                </div>
                <div className="px-3 py-1 rounded-full text-xs font-bold bg-purple-900/30 text-purple-400 border border-purple-900/50">
                  {envHealth.environment_mode}
                </div>
              </div>

              <div className="flex items-center justify-between p-4 bg-zinc-900/50 border border-zinc-800 rounded-lg">
                <div className="flex items-center gap-3">
                  <ShieldAlert className="w-5 h-5 text-red-400" />
                  <div>
                    <div className="font-medium">Emergency Kill Switch</div>
                    <div className="text-xs text-zinc-500">Halts all execution when latched</div>
                  </div>
                </div>
                {/* The same words the operator console uses: ARMED (not latched) or LATCHED (trading halted) */}
                <div className={`px-3 py-1 rounded-full text-xs font-bold ${envHealth.kill_switch_active ? 'bg-red-900/30 text-red-400 border border-red-900/50' : 'bg-emerald-950/40 text-emerald-400 border border-emerald-900/50'}`}>
                  {envHealth.kill_switch_active ? "LATCHED · TRADING HALTED" : "ARMED"}
                </div>
              </div>
            </div>
            
            <div className="p-4 bg-blue-900/20 border border-blue-900/30 rounded-lg text-sm text-blue-300">
              <span className="font-bold">Note:</span> Modifying these environment variables requires editing the `.env` file on the host machine and restarting the Docker containers.
            </div>
          </div>
        )}

        {activeTab === "platform" && (
          <div className="flex-1 min-h-0 overflow-y-auto">
            <SettingsRegistry />
          </div>
        )}
      </div>
    </div>
  );
};
