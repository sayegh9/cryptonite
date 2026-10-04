"use client";

import React, { useState, useRef, useEffect } from "react";
import { BrainCircuit, Send, Loader2, Trash2, Zap, Tag } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { fetchApi } from "@/lib/api";
import { AGENT_CONFIGS, newId, parseTargetSymbol as parseSymbol, starterChips, type Dispatch, type Message } from "./canvas/agents";
import { InlineActionCard } from "./canvas/InlineActionCard";

interface OrchestratorCanvasProps {
  selectedAgentId?: string;
  onSelectAgent?: (agentId: string) => void;
  activeSymbol: string;
  activeTimeframe?: string;
  onTriggerDebate?: (symbol: string) => void;
}


export function OrchestratorCanvas({
  selectedAgentId = "orchestrator",
  onSelectAgent,
  activeSymbol,
  activeTimeframe = "1m",
  onTriggerDebate,
}: OrchestratorCanvasProps) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [liveTelemetry, setLiveTelemetry] = useState<Dispatch[]>([]);
  const scrollRef = useRef<HTMLDivElement>(null);

  const currentAgent = AGENT_CONFIGS[selectedAgentId] || AGENT_CONFIGS.orchestrator;

  // Auto-scroll on new messages
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages, loading, liveTelemetry]);

  // Live Telemetry Subscription
  useEffect(() => {
    let unsub: () => void;
    import("@/lib/ws").then(({ getStreamClient }) => {
      const client = getStreamClient();
      unsub = client.subscribe((data) => {
        if (data.type === "HEARTBEAT") {
          setLiveTelemetry((prev) => {
            const existing = prev.find((t) => t.agent === data.agentId);
            if (existing && existing.status === data.msg) return prev; // Avoid duplicates
            return [...prev, { agent: data.agentId, latencyMs: null, status: data.msg }].slice(-5);
          });
        }
      });
    });
    return () => unsub?.();
  }, []);


  const handleSend = async (customPrompt?: string, retryOf?: string) => {
    const textToSend = customPrompt || input;
    if (!textToSend.trim() || loading) return;

    const targetSym = parseSymbol(textToSend, activeSymbol);

    if (retryOf) {
      // Retry replaces its error notice; the operator's message is already on the canvas
      setMessages((prev) => prev.filter((m) => m.id !== retryOf));
    } else {
      const userMsg: Message = {
        id: newId("user"),
        role: "user",
        agentId: selectedAgentId,
        agentName: "Operator",
        content: textToSend,
        timestamp: Date.now(),
      };
      setMessages((prev) => [...prev, userMsg]);
    }
    if (!customPrompt) setInput("");
    setLoading(true);
    setLiveTelemetry([]); // Clear trace for new request

    try {
      // If user asks to debate, trigger background debate event (not again on retry)
      if (!retryOf && textToSend.toLowerCase().includes("debate") && onTriggerDebate) {
        onTriggerDebate(targetSym);
      }

      let finalPrompt = textToSend;
      if (finalPrompt.includes("@Backtest")) {
        finalPrompt += "\n\n[System Auto-Inject: The user referenced a backtest. Please use the read_nautilus_tearsheet tool to pull its metrics and evaluate the Sharpe Ratio and Drawdown.]";
      }
      if (finalPrompt.includes("/delta") || finalPrompt.includes("@PaperPosition")) {
        finalPrompt += "\n\n[System Auto-Inject: The user requested a performance delta diagnostic. Use the compare_live_vs_backtest tool to analyze slippage between live fills and the backtest.]";
      }

      // Call the corresponding agent endpoint
      const endpoint = currentAgent.endpoint || "/agent-api/chat";
      const res = await fetchApi(endpoint, {
        method: "POST",
        headers: { "X-CSRF-Token": localStorage.getItem("csrf_token") || "" },
        body: JSON.stringify({
          message: finalPrompt,
          symbol: targetSym,
          timeframe: activeTimeframe,
        }),
      });

      const responseText = typeof res?.response === "string" && res.response ? res.response
        : typeof res?.message === "string" && res.message ? res.message : "";
      if (!responseText) throw Object.assign(new Error("empty response"), { status: "empty response" });

      // Capture current liveTelemetry before it gets cleared or updated by next turn
      const capturedTelemetry = [...liveTelemetry];

      const assistantMsg: Message = {
        id: newId("asst"),
        role: "assistant",
        agentId: selectedAgentId,
        agentName: currentAgent.name,
        content: responseText,
        dispatches: capturedTelemetry.length > 0 ? capturedTelemetry : undefined,
        timestamp: Date.now(),
      };

      setMessages((prev) => [...prev, assistantMsg]);
    } catch (err: any) {
      // Never invent an answer: the canvas shows that the agent did not respond, with a Retry
      const status = err?.status ?? (err instanceof TypeError ? "network error" : err?.message || "unknown error");
      const errorMsg: Message = {
        id: newId("sys"),
        role: "system",
        agentId: selectedAgentId,
        agentName: currentAgent.name,
        content: `agent unavailable: ${status}`,
        retryPrompt: textToSend,
        timestamp: Date.now(),
      };
      setMessages((prev) => [...prev, errorMsg]);
    } finally {
      setLoading(false);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const insertTag = (tag: string) => {
    // If user clicked an agent tag like @scout, also switch active agent
    if (tag === "@scout" && onSelectAgent) onSelectAgent("scout");
    else if (tag === "@cro" && onSelectAgent) onSelectAgent("cro");
    else if (tag === "@debater" && onSelectAgent) onSelectAgent("debater");
    else if (tag === "@outbox" && onSelectAgent) onSelectAgent("outbox");
    else if (tag === "@lead" && onSelectAgent) onSelectAgent("orchestrator");

    setInput((prev) => prev + (prev.length > 0 && !prev.endsWith(" ") ? " " : "") + tag + " ");
  };

  const IconComponent = currentAgent.icon;

  return (
    <div className="flex flex-col h-full w-full bg-zinc-950 overflow-hidden">
      {/* Top Agent Switcher & Status Bar */}
      <div className="flex-none bg-zinc-900/60 border-b border-zinc-800 px-3 sm:px-4 py-2 flex flex-wrap items-center justify-between gap-2 z-10">
        {/* Active Agent Info & Tabs */}
        <div className="flex items-center space-x-2 overflow-x-auto min-w-0 py-0.5">
          <div className="flex items-center space-x-1.5 mr-2 flex-none">
            <div className={`p-1 rounded bg-zinc-900 border border-zinc-700 ${currentAgent.color}`}>
              <IconComponent className="w-3.5 h-3.5" />
            </div>
            <div className="hidden md:block">
              <div className="font-bold text-xs text-zinc-100 leading-none">{currentAgent.name} Canvas</div>
              <div className="text-2xs text-zinc-400 font-mono mt-0.5">{currentAgent.roleTitle}</div>
            </div>
          </div>

          {/* Quick Agent Switching Tabs */}
          <div className="flex items-center space-x-1 bg-zinc-950/80 p-0.5 rounded-lg border border-zinc-800 flex-none">
            {Object.values(AGENT_CONFIGS).map((ag) => {
              const isAgSelected = selectedAgentId === ag.id;
              const AgIcon = ag.icon;
              return (
                <button
                  key={ag.id}
                  onClick={() => onSelectAgent && onSelectAgent(ag.id)}
                  className={`px-2 py-1 rounded text-2xs font-mono transition-all flex items-center space-x-1 ${
                    isAgSelected
                      ? `${ag.badgeColor} font-bold shadow-sm`
                      : "text-zinc-400 hover:text-zinc-200 hover:bg-zinc-900"
                  }`}
                  title={`Switch chat to ${ag.name}`}
                >
                  <AgIcon className={`w-3 h-3 ${isAgSelected ? ag.color : "text-zinc-500"}`} />
                  <span className="hidden sm:inline">{ag.shortName}</span>
                </button>
              );
            })}
          </div>
        </div>

        {/* Right Info & Actions */}
        <div className="flex items-center space-x-2 flex-none">
          <span className="text-2xs font-mono px-2 py-0.5 rounded bg-zinc-800 text-cyan-300 border border-zinc-700 hidden lg:inline">
            Symbol Context: {activeSymbol} ({activeTimeframe})
          </span>

          {messages.length > 0 && (
            <button
              onClick={() => setMessages([])}
              className="flex items-center space-x-1 text-2xs font-mono text-zinc-500 hover:text-zinc-300 px-2 py-0.5 rounded hover:bg-zinc-900 transition-colors"
              title="Clear canvas history"
            >
              <Trash2 className="w-3 h-3" />
              <span>Clear</span>
            </button>
          )}
        </div>
      </div>

      {/* Messages Scroll Area */}
      <div ref={scrollRef} className="flex-1 min-h-0 overflow-y-auto p-3 sm:p-5 space-y-4">
        <div className="max-w-4xl mx-auto w-full space-y-4">
          {messages.length === 0 ? (
            <div className="py-6 px-4 flex flex-col items-center justify-center max-w-xl mx-auto text-center space-y-4">
              <div className={`w-12 h-12 rounded-2xl ${currentAgent.bgBorder} border flex items-center justify-center ${currentAgent.color} shadow-lg`}>
                <IconComponent className="w-6 h-6" />
              </div>

              <div className="space-y-1">
                <h3 className="text-base font-bold text-zinc-100 tracking-tight flex items-center justify-center space-x-2">
                  <span>Welcome to the Cryptonite AI Brain</span>
                </h3>
                <p className="text-xs text-zinc-400 leading-relaxed max-w-md mx-auto">
                  {currentAgent.description}
                </p>
              </div>

              {/* 1-Touch Starter Prompt Chips tailored to Active Agent */}
              <div className="w-full grid grid-cols-1 sm:grid-cols-2 gap-2 text-left pt-2">
                {starterChips(selectedAgentId, activeSymbol || "BTCUSDT").map((chip, i) => {
                  const Icon = chip.icon;
                  return (
                    <button
                      key={i}
                      onClick={() => handleSend(chip.prompt)}
                      className={`p-3 rounded-xl border text-xs font-mono transition-all text-left flex items-start space-x-2.5 ${chip.color}`}
                    >
                      <Icon className="w-4 h-4 flex-none mt-0.5" />
                      <div className="min-w-0 flex-1">
                        <div className="font-bold text-zinc-200 truncate">{chip.title}</div>
                        <div className="text-2xs text-zinc-400 line-clamp-1 mt-0.5">
                          {chip.prompt}
                        </div>
                      </div>
                    </button>
                  );
                })}
              </div>
            </div>
          ) : (
            messages.map((msg) => {
              const msgAgentConfig = AGENT_CONFIGS[msg.agentId] || AGENT_CONFIGS.orchestrator;
              const MsgIcon = msgAgentConfig.icon;
              if (msg.role === "system") {
                return (
                  <div
                    key={msg.id}
                    role="alert"
                    data-testid="canvas-agent-error"
                    className="flex items-center justify-between gap-3 rounded-lg border border-rose-900/60 bg-rose-950/30 px-3 py-2 font-mono text-2xs text-rose-300"
                  >
                    <span>
                      <span className="text-zinc-500">{msg.agentName} · {new Date(msg.timestamp).toLocaleTimeString()} · </span>
                      {msg.content}
                    </span>
                    {msg.retryPrompt && (
                      <button
                        type="button"
                        onClick={() => handleSend(msg.retryPrompt, msg.id)}
                        disabled={loading}
                        className="flex-none rounded border border-rose-800 px-2 py-0.5 text-rose-200 hover:bg-rose-900/40 disabled:opacity-50"
                      >
                        Retry
                      </button>
                    )}
                  </div>
                );
              }
              return (
                <div
                  key={msg.id}
                  className={`flex flex-col space-y-1 ${
                    msg.role === "user" ? "items-end" : "items-start"
                  }`}
                >
                  <div className="flex items-center space-x-1.5 text-2xs font-mono text-zinc-500 px-1">
                    {msg.role === "assistant" && (
                      <span className={`flex items-center space-x-1 font-semibold ${msgAgentConfig.color}`}>
                        <MsgIcon className="w-3 h-3" />
                        <span>{msg.agentName}</span>
                      </span>
                    )}
                    {msg.role === "user" && <span>You (Operator)</span>}
                    <span>•</span>
                    <span>{new Date(msg.timestamp).toLocaleTimeString()}</span>
                  </div>

                  <div
                    className={`text-xs leading-relaxed ${
                      msg.role === "user"
                        ? "max-w-[85%] sm:max-w-lg rounded-2xl rounded-tr-none px-4 py-3 bg-cyan-950/70 border border-cyan-800/80 text-cyan-100 shadow-sm"
                        : `max-w-[95%] sm:max-w-2xl rounded-2xl rounded-tl-none p-4 bg-zinc-900/95 border ${msgAgentConfig.bgBorder} text-zinc-200 shadow-md space-y-2`
                    }`}
                  >
                    {/* Visual Micro-Dispatch Trace if assistant */}
                    {msg.dispatches && msg.dispatches.length > 0 && (
                      <div className="mb-2 p-2.5 bg-zinc-950/90 border border-zinc-800/80 rounded-xl font-mono text-2xs space-y-1">
                        <div className="text-zinc-500 uppercase tracking-wider text-2xs font-bold">
                          Specialist Execution Trace:
                        </div>
                        {msg.dispatches.map((d, idx) => (
                          <div key={idx} className="flex items-center justify-between text-zinc-400 gap-2">
                            <span className="flex items-center space-x-1 truncate">
                              <span className={msgAgentConfig.color}>↳</span>
                              <span className="text-zinc-300 font-semibold">{d.agent}:</span>
                              <span className="truncate">{d.status}</span>
                            </span>
                            {d.latencyMs !== null && <span className="text-zinc-500 text-2xs flex-none">{d.latencyMs}ms</span>}
                          </div>
                        ))}
                      </div>
                    )}

                    {msg.role === "assistant" ? (
                      <div className="prose prose-invert prose-xs max-w-none prose-p:my-1 prose-headings:text-zinc-100 prose-headings:my-2 prose-strong:text-cyan-300">
                        <ReactMarkdown 
                          remarkPlugins={[remarkGfm]}
                          components={{
                            code(props) {
                              const {children, className, node, ...rest} = props;
                              const match = /language-(\w+)/.exec(className || '');
                              const codeStr = String(children).replace(/\n$/, '');
                              
                              // Check if this looks like a strategy manifest JSON block
                              if (match && match[1].includes('json') && (codeStr.includes('allocation_usd') || codeStr.includes('instrument_id') || codeStr.includes('max_position_notional_usd'))) {
                                return <InlineActionCard codeStr={codeStr} onTriggerDebate={onTriggerDebate} fallbackSymbol={activeSymbol || "BTCUSDT"} />;
                              }
                              
                              const isInline = !match && !codeStr.includes('\n');
                              
                              return !isInline ? (
                                <pre className="bg-zinc-900/80 p-3 rounded-lg overflow-x-auto border border-zinc-800 my-2 text-2xs">
                                  <code className={className} {...rest}>{children}</code>
                                </pre>
                              ) : (
                                <code className="bg-zinc-800/80 text-cyan-200 px-1.5 py-0.5 rounded text-2xs" {...rest}>{children}</code>
                              );
                            }
                          }}
                        >
                          {msg.content}
                        </ReactMarkdown>
                      </div>

                    ) : (
                      <p className="whitespace-pre-wrap">{msg.content}</p>
                    )}
                  </div>
                </div>
              );
            })
          )}

          {loading && (
            <div className="flex flex-col space-y-3 p-4 bg-zinc-900/50 border border-zinc-800/80 rounded-2xl max-w-2xl">
              <div className="flex items-start space-x-2 text-xs font-mono text-zinc-400">
                <Loader2 className={`w-4 h-4 ${currentAgent.color} animate-spin mt-0.5 flex-none`} />
                <div className="space-y-0.5">
                  <div className="text-zinc-300">{currentAgent.name} processing query...</div>
                  <div className="text-2xs text-zinc-500">Executing background diagnostics...</div>
                </div>
              </div>
              
              {/* Live Telemetry Display while loading */}
              {liveTelemetry.length > 0 && (
                <div className="p-2.5 bg-zinc-950/90 border border-zinc-800/80 rounded-xl font-mono text-2xs space-y-1">
                  <div className="text-zinc-500 uppercase tracking-wider text-2xs font-bold">
                    Live Specialist Telemetry:
                  </div>
                  {liveTelemetry.map((d, idx) => (
                    <div key={idx} className="flex items-center justify-between text-zinc-400 gap-2 animate-in fade-in slide-in-from-bottom-1 duration-300">
                      <span className="flex items-center space-x-1 truncate">
                        <span className={currentAgent.color}>↳</span>
                        <span className="text-zinc-300 font-semibold">{d.agent}:</span>
                        <span className="truncate">{d.status}</span>
                      </span>
                      {d.latencyMs !== null && <span className="text-zinc-500 text-2xs flex-none">{d.latencyMs}ms</span>}
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {/* Input Rail & Context Stashers */}
      <div className="flex-none p-3 sm:p-4 bg-zinc-900/40 border-t border-zinc-800">
        <div className="max-w-4xl mx-auto w-full space-y-2">
          {/* Quick Context Stash Tags */}
          <div className="flex items-center space-x-1.5 overflow-x-auto text-2xs font-mono text-zinc-400 py-0.5">
            <span className="text-zinc-500 text-2xs uppercase tracking-wider flex-none">Quick Mentions:</span>
            {["@scout", "@cro", "@debater", "@outbox", "@lead"].map((agTag, idx) => (
              <button
                key={idx}
                onClick={() => insertTag(agTag)}
                className="px-2 py-0.5 bg-zinc-900 hover:bg-zinc-800 border border-zinc-800 hover:border-zinc-700 rounded text-cyan-300 transition-colors flex-none"
              >
                {agTag}
              </button>
            ))}
            <span className="text-zinc-600">|</span>
            {["@XRP", "@BTC", "@ETH", "@SOL"].map((sym, idx) => (
              <button
                key={idx}
                onClick={() => insertTag(sym)}
                className="px-2 py-0.5 bg-zinc-900 hover:bg-zinc-800 border border-zinc-800 hover:border-zinc-700 rounded text-zinc-300 transition-colors flex-none"
              >
                {sym}
              </button>
            ))}
            <button
              onClick={() => insertTag(`@LiveChart(${activeSymbol || "BTCUSDT"}, ${activeTimeframe})`)}
              className="px-2 py-0.5 bg-cyan-950/50 hover:bg-cyan-950 border border-cyan-800/60 rounded text-cyan-300 transition-colors flex items-center space-x-1 flex-none"
            >
              <Tag className="w-2.5 h-2.5" />
              <span>+ Attach Live Chart</span>
            </button>
            <button
              onClick={() => insertTag(`@Backtest(latest)`)}
              className="px-2 py-0.5 bg-blue-950/50 hover:bg-blue-950 border border-blue-800/60 rounded text-blue-300 transition-colors flex items-center space-x-1 flex-none"
            >
              <BrainCircuit className="w-2.5 h-2.5" />
              <span>+ Attach Backtest</span>
            </button>
            <button
              onClick={() => insertTag(`/delta @PaperPosition(latest)`)}
              className="px-2 py-0.5 bg-purple-950/50 hover:bg-purple-950 border border-purple-800/60 rounded text-purple-300 transition-colors flex items-center space-x-1 flex-none"
            >
              <Zap className="w-2.5 h-2.5" />
              <span>+ Delta Diagnostic</span>
            </button>
          </div>

          {/* Input Box */}
          <div className="relative flex items-center">
            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder={currentAgent.placeholder}
              rows={2}
              className="w-full bg-zinc-900/90 border border-zinc-800 focus:border-cyan-500 rounded-xl py-3 pl-3.5 pr-11 text-xs text-zinc-100 placeholder-zinc-500 resize-none outline-none font-sans transition-colors"
            />
            <button
              onClick={() => handleSend()}
              disabled={!input.trim() || loading}
              className={`absolute right-3 bottom-3.5 p-1.5 rounded-lg bg-cyan-600 hover:bg-cyan-500 disabled:bg-zinc-800 text-zinc-950 disabled:text-zinc-600 transition-colors`}
              title="Send prompt"
            >
              <Send className="w-3.5 h-3.5" />
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
