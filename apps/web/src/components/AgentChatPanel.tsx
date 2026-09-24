"use client";

import { useState, useEffect, useRef, useCallback } from "react";
import { fetchApi } from "@/lib/api";
import { useChatStore } from "@/lib/stores/useChatStore";

import {
  X,
  Send,
  User as UserIcon,
  Loader2,
  Info,
  Copy,
  Check,
  Maximize2,
  Minimize2,
  GripHorizontal,
  ShieldAlert,
  Activity,
  Search,
  Sparkles,
  TrendingUp,
  FlaskConical,
  Crosshair,
} from "lucide-react";

export type AgentMode = "research" | "strategist" | "explain" | "cro" | "onchain";

interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
}

const PERSONA_CONFIG: Record<
  AgentMode,
  {
    name: string;
    shortName: string;
    accentColor: string;
    badgeClass: string;
    borderClass: string;
    Icon: any;
    endpoint: string;
    placeholder: string;
    description: string;
  }
> = {
  research: {
    name: "Research Intelligence",
    shortName: "Research",
    accentColor: "text-cyan-400",
    badgeClass: "bg-cyan-500/10 text-cyan-400 border-cyan-500/30",
    borderClass: "border-cyan-500/40",
    Icon: Search,
    endpoint: "/agent-api/chat",
    placeholder: "Ask about Bitcoin market structure, volume profile, or technical setups...",
    description: "Real-time crypto market intelligence, technical indicators, and price action research.",
  },
  strategist: {
    name: "Execution Strategist",
    shortName: "Strategist",
    accentColor: "text-purple-400",
    badgeClass: "bg-purple-500/10 text-purple-400 border-purple-500/30",
    borderClass: "border-purple-500/40",
    Icon: Sparkles,
    endpoint: "/agent-api/strategist",
    placeholder: "Generate an EMA cross strategy in Python or hunt momentum opportunities...",
    description: "Quantitative Python strategy development for NautilusTrader and momentum alpha hunting.",
  },
  explain: {
    name: "Backtest Explainer",
    shortName: "Explainer",
    accentColor: "text-amber-400",
    badgeClass: "bg-amber-500/10 text-amber-400 border-amber-500/30",
    borderClass: "border-amber-500/40",
    Icon: Info,
    endpoint: "/agent-api/explain",
    placeholder: "Ask question about backtest run, or leave blank to explain...",
    description: "Institutional backtest analysis, risk metrics, Sharpe ratios, and max drawdown evaluation.",
  },
  cro: {
    name: "Chief Risk Officer",
    shortName: "Risk (CRO)",
    accentColor: "text-rose-400",
    badgeClass: "bg-rose-500/10 text-rose-400 border-rose-500/30",
    borderClass: "border-rose-500/40",
    Icon: ShieldAlert,
    endpoint: "/agent-api/cro",
    placeholder: "Evaluate portfolio drawdown if BTC drops 10%, or check margin concentration...",
    description: "Portfolio risk assessment, capital preservation limits, drawdown stress tests, and leverage bounds.",
  },
  onchain: {
    name: "On-Chain & Whale Intel",
    shortName: "On-Chain",
    accentColor: "text-emerald-400",
    badgeClass: "bg-emerald-500/10 text-emerald-400 border-emerald-500/30",
    borderClass: "border-emerald-500/40",
    Icon: Activity,
    endpoint: "/agent-api/onchain",
    placeholder: "Check Ethereum whale transfer flows, exchange reserves, or gas dynamics...",
    description: "Blockchain node intelligence, whale wallet accumulation, exchange netflows, and network gas dynamics.",
  },
};

function StrategyProposalCard({
  manifestStr,
  codeText,
}: {
  manifestStr: string;
  codeText: string;
  msgId: string;
}) {
  const [manifest, setManifest] = useState<any>(null);
  const [strategyName, setStrategyName] = useState("");
  const [instrumentId, setInstrumentId] = useState("");
  const [timeframe, setTimeframe] = useState("");
  const [allocation, setAllocation] = useState(0);
  const [deploying, setDeploying] = useState(false);
  const [deployedInstance, setDeployedInstance] = useState<string | null>(null);

  useEffect(() => {
    try {
      const parsed = JSON.parse(manifestStr);
      setManifest(parsed);
      setStrategyName(parsed.strategy_name || "AI_Strategy");
      setInstrumentId(parsed.instrument_id || "BTCUSDT.BINANCE");
      setTimeframe(parsed.timeframe || "15m");
      setAllocation(parsed.allocation_usd || 10000);
    } catch (e) {
      console.error("Failed to parse manifest", e);
    }
  }, [manifestStr]);

  const handleDeploy = async () => {
    if (!codeText) {
      alert("No Python code found to deploy.");
      return;
    }
    setDeploying(true);
    try {
      // 1. Save Strategy
      const saveRes = await fetchApi("/strategies", {
        method: "POST",
        body: JSON.stringify({
          name: strategyName,
          description: "AI Generated Strategy via Agent Manifest",
          python_code: codeText,
        }),
      });

      if (!saveRes.version_id) throw new Error("No version_id returned from strategies API");

      // 2. Deploy to paper
      const deployRes = await fetchApi("/paper/strategies/deploy", {
        method: "POST",
        headers: { "X-CSRF-Token": localStorage.getItem("csrf_token") || "" },
        body: JSON.stringify({
          strategy_name: strategyName,
          strategy_version_id: saveRes.version_id,
          instrument_id: instrumentId,
          timeframe: timeframe,
          allocation_usd: Number(allocation),
          parameters: manifest?.parameters || {},
          account_id: "PAPER-ACCOUNT-001"
        }),
      });

      setDeployedInstance(deployRes.command_id || "Command Enqueued");
      window.dispatchEvent(new CustomEvent("cryptonite:strategy-updated"));
    } catch (e: any) {
      alert("Deployment Error: " + e.message);
    } finally {
      setDeploying(false);
    }
  };

  const handleSendToLab = async () => {
    if (!codeText) {
      alert("No Python code found to deploy.");
      return;
    }
    setDeploying(true);
    try {
      // 1. Save Strategy first so the Research Lab can load it by slug
      const saveRes = await fetchApi("/strategies", {
        method: "POST",
        body: JSON.stringify({
          name: strategyName,
          description: "AI Generated Strategy via Agent Manifest",
          python_code: codeText,
        }),
      });
      
      // Dispatch load-strategy with the actual slug generated by the backend
      const savedSlug = saveRes.slug;
      
      window.dispatchEvent(new CustomEvent("cryptonite:strategy-updated"));
      window.dispatchEvent(new CustomEvent("cryptonite:switch-workspace", { detail: { mode: "research" } }));
      window.dispatchEvent(new CustomEvent("cryptonite:load-strategy", { 
        detail: { 
          manifest: { ...manifest, slug: savedSlug, name: strategyName }, 
          codeText 
        } 
      }));
    } catch (e: any) {
      alert("Failed to save strategy to Lab: " + e.message);
    } finally {
      setDeploying(false);
    }
  };

  if (!manifest) return null;

  return (
    <div className="my-3 bg-zinc-900 border border-purple-500/50 rounded-xl overflow-hidden select-text p-4 shadow-lg shadow-purple-900/10">
      <div className="flex items-center space-x-2 mb-4 border-b border-zinc-800 pb-2">
        <Sparkles className="w-4 h-4 text-purple-400" />
        <h3 className="font-bold text-sm text-zinc-100">Strategy Deployment Proposal</h3>
      </div>
      
      {deployedInstance ? (
        <div className="bg-emerald-500/10 border border-emerald-500/30 rounded p-3 text-emerald-400 text-xs font-mono">
          <Check className="w-4 h-4 inline mr-1" />
          Successfully deployed! Command ID: {deployedInstance}
        </div>
      ) : (
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="text-[10px] text-zinc-500 block mb-1 font-mono uppercase">Strategy Name</label>
              <input type="text" value={strategyName} onChange={e => setStrategyName(e.target.value)} className="w-full bg-zinc-950 border border-zinc-700 rounded px-2 py-1 text-xs text-zinc-200" />
            </div>
            <div>
              <label className="text-[10px] text-zinc-500 block mb-1 font-mono uppercase">Instrument ID</label>
              <input type="text" value={instrumentId} onChange={e => setInstrumentId(e.target.value)} className="w-full bg-zinc-950 border border-zinc-700 rounded px-2 py-1 text-xs text-zinc-200" />
            </div>
            <div>
              <label className="text-[10px] text-zinc-500 block mb-1 font-mono uppercase">Timeframe</label>
              <input type="text" value={timeframe} onChange={e => setTimeframe(e.target.value)} className="w-full bg-zinc-950 border border-zinc-700 rounded px-2 py-1 text-xs text-zinc-200" />
            </div>
            <div>
              <label className="text-[10px] text-zinc-500 block mb-1 font-mono uppercase">Allocation (USD)</label>
              <input type="number" value={allocation} onChange={e => setAllocation(Number(e.target.value))} className="w-full bg-zinc-950 border border-zinc-700 rounded px-2 py-1 text-xs text-zinc-200" />
            </div>
          </div>
          <button
            onClick={handleSendToLab}
            disabled={deploying}
            className="w-full mt-2 bg-zinc-700 hover:bg-zinc-600 text-white text-xs font-bold py-2 rounded transition-colors flex items-center justify-center space-x-2 disabled:opacity-50"
          >
            {deploying ? <Loader2 className="w-3 h-3 animate-spin" /> : <FlaskConical className="w-3 h-3" />}
            <span>Backtest in Research Lab</span>
          </button>
          <button 
            onClick={handleDeploy} 
            disabled={deploying}
            className="w-full mt-2 bg-purple-600 hover:bg-purple-500 text-white text-xs font-bold py-2 rounded transition-colors flex items-center justify-center space-x-2 disabled:opacity-50"
          >
            {deploying ? <Loader2 className="w-3 h-3 animate-spin" /> : <TrendingUp className="w-3 h-3" />}
            <span>Confirm & Deploy to Paper</span>
          </button>
        </div>
      )}
    </div>
  );
}

export function AgentChatPanel({
  isOpen,
  onClose,
  activeBacktestId,
}: {
  isOpen: boolean;
  onClose: () => void;
  activeBacktestId?: string | null;
}) {
  const [mode, setMode] = useState<AgentMode>("research");

  // Fully isolated conversation threads per agent persona
  const chatThreads = useChatStore((state) => state.threads);
  const addMessage = useChatStore((state) => state.addMessage);
  const setMessages = useChatStore((state) => state.setMessages);
  
  const threads = {
    research: (chatThreads.research || []) as Message[],
    strategist: (chatThreads.strategist || []) as Message[],
    explain: (chatThreads.explain || []) as Message[],
    cro: (chatThreads.cro || []) as Message[],
    onchain: (chatThreads.onchain || []) as Message[],
  };

  // Fully isolated input drafts per agent persona
  const [drafts, setDrafts] = useState<Record<AgentMode, string>>({
    research: "",
    strategist: "",
    explain: "",
    cro: "",
    onchain: "",
  });

  const [loading, setLoading] = useState(false);
  const [backtestIdInput, setBacktestIdInput] = useState("");
  const [, setSavingStrategy] = useState<string | null>(null);
  const [copiedId, setCopiedId] = useState<string | null>(null);

  // Window positioning and resizing state
  const [isDocked, setIsDocked] = useState<boolean>(false);
  const [position, setPosition] = useState<{ x: number; y: number }>({ x: 0, y: 0 });
  const [size, setSize] = useState<{ width: number; height: number }>({ width: 520, height: 740 });
  const isDraggingRef = useRef(false);
  const isResizingRef = useRef<"se" | "sw" | "ne" | "nw" | "w" | "e" | "s" | "n" | null>(null);
  const dragStartRef = useRef<{ x: number; y: number; posX: number; posY: number; width: number; height: number }>({
    x: 0,
    y: 0,
    posX: 0,
    posY: 0,
    width: 0,
    height: 0,
  });

  const panelRef = useRef<HTMLDivElement>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  // Active persona configuration
  const currentPersona = PERSONA_CONFIG[mode];
  const currentMessages = threads[mode];
  const currentDraft = drafts[mode];

  // Initialize position on open
  useEffect(() => {
    if (isOpen && typeof window !== "undefined") {
      const initW = Math.min(540, window.innerWidth - 40);
      const initH = Math.min(760, window.innerHeight - 80);
      const initX = Math.max(20, window.innerWidth - initW - 24);
      const initY = 56;

      setSize({ width: initW, height: initH });
      setPosition({ x: initX, y: initY });
    }
  }, [isOpen]);

  const handleResetPosition = () => {
    if (typeof window !== "undefined") {
      const initW = Math.min(540, window.innerWidth - 40);
      const initH = Math.min(760, window.innerHeight - 80);
      setSize({ width: initW, height: initH });
      setPosition({
        x: Math.max(20, (window.innerWidth - initW) / 2),
        y: Math.max(30, (window.innerHeight - initH) / 2),
      });
    }
  };

  useEffect(() => {
    if (isOpen && activeBacktestId) {
      setMode("explain");
      setBacktestIdInput(activeBacktestId);
    }
  }, [isOpen, activeBacktestId]);

  useEffect(() => {
    const handleCopilotEvent = (e: CustomEvent) => {
      if (e.detail) {
        const { mode: newMode, prompt, autoSend } = e.detail;
        if (newMode) setMode(newMode as AgentMode);
        if (prompt) {
          if (autoSend) {
            executePrompt((newMode as AgentMode) || "research", prompt);
          } else {
            setDrafts(prev => ({ ...prev, [newMode || "research"]: prompt }));
          }
        }
      }
    };
    window.addEventListener("cryptonite:open-copilot" as any, handleCopilotEvent);
    return () => {
      window.removeEventListener("cryptonite:open-copilot" as any, handleCopilotEvent);
    };
  }, [backtestIdInput]); // Need backtestIdInput for executePrompt closure

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [threads, mode]);

  // Dragging handling
  const handleHeaderPointerDown = (e: React.PointerEvent) => {
    if (isDocked) return;
    if (e.button !== 0 || (e.target as HTMLElement).closest("button")) return;

    isDraggingRef.current = true;
    dragStartRef.current = {
      x: e.clientX,
      y: e.clientY,
      posX: position.x,
      posY: position.y,
      width: size.width,
      height: size.height,
    };

    (e.target as HTMLElement).setPointerCapture(e.pointerId);
  };

  const handlePointerMove = useCallback((e: PointerEvent) => {
    if (isDraggingRef.current) {
      const deltaX = e.clientX - dragStartRef.current.x;
      const deltaY = e.clientY - dragStartRef.current.y;

      const maxX = Math.max(10, window.innerWidth - dragStartRef.current.width - 10);
      const maxY = Math.max(10, window.innerHeight - 80);

      const nextX = Math.max(10, Math.min(maxX, dragStartRef.current.posX + deltaX));
      const nextY = Math.max(10, Math.min(maxY, dragStartRef.current.posY + deltaY));

      setPosition({ x: nextX, y: nextY });
    } else if (isResizingRef.current) {
      const { x: startX, y: startY, width: startW, height: startH, posX: startPosX, posY: startPosY } = dragStartRef.current;
      const deltaX = e.clientX - startX;
      const deltaY = e.clientY - startY;
      const dir = isResizingRef.current;

      const minW = 380;
      const maxW = typeof window !== "undefined" ? window.innerWidth - 20 : 1200;
      const minH = 380;
      const maxH = typeof window !== "undefined" ? window.innerHeight - 30 : 1000;

      let nextW = startW;
      let nextH = startH;
      let nextX = startPosX;
      let nextY = startPosY;

      // Handle East / West horizontal resize
      if (dir.includes("e")) {
        nextW = Math.max(minW, Math.min(maxW, startW + deltaX));
      } else if (dir.includes("w")) {
        nextW = Math.max(minW, Math.min(startPosX + startW - 10, startW - deltaX));
        nextX = startPosX + (startW - nextW);
      }

      // Handle South / North vertical resize
      if (dir.includes("s")) {
        nextH = Math.max(minH, Math.min(maxH, startH + deltaY));
      } else if (dir.includes("n")) {
        nextH = Math.max(minH, Math.min(startPosY + startH - 30, startH - deltaY));
        nextY = startPosY + (startH - nextH);
      }

      setSize({ width: nextW, height: nextH });
      setPosition({ x: nextX, y: nextY });
    }
  }, [isDocked]);

  const handlePointerUp = useCallback(() => {
    if (isDraggingRef.current) {
      isDraggingRef.current = false;
    }
    if (isResizingRef.current) {
      isResizingRef.current = null;
    }
  }, []);

  useEffect(() => {
    window.addEventListener("pointermove", handlePointerMove);
    window.addEventListener("pointerup", handlePointerUp);
    return () => {
      window.removeEventListener("pointermove", handlePointerMove);
      window.removeEventListener("pointerup", handlePointerUp);
    };
  }, [handlePointerMove, handlePointerUp]);

  // Full 8-direction resize starter
  const startResize = (direction: "se" | "sw" | "ne" | "nw" | "w" | "e" | "s" | "n", e: React.PointerEvent) => {
    if (isDocked) return;
    e.preventDefault();
    e.stopPropagation();
    isResizingRef.current = direction;
    dragStartRef.current = {
      x: e.clientX,
      y: e.clientY,
      posX: position.x,
      posY: position.y,
      width: size.width,
      height: size.height,
    };
    (e.target as HTMLElement).setPointerCapture(e.pointerId);
  };

  const handleCopyText = async (id: string, text: string) => {
    let success = false;
    try {
      if (typeof navigator !== "undefined" && navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
        success = true;
      }
    } catch {
      // Fallback
    }

    if (!success && typeof document !== "undefined") {
      try {
        const textArea = document.createElement("textarea");
        textArea.value = text;
        textArea.style.position = "fixed";
        textArea.style.left = "-9999px";
        textArea.style.top = "-9999px";
        document.body.appendChild(textArea);
        textArea.focus();
        textArea.select();
        document.execCommand("copy");
        document.body.removeChild(textArea);
        success = true;
      } catch (e) {
        console.error("Fallback copy failed", e);
      }
    }

    if (success) {
      setCopiedId(id);
      setTimeout(() => {
        setCopiedId(null);
      }, 2000);
    }
  };

  const handleCopyEntireThread = () => {
    if (currentMessages.length === 0) return;
    const formatted = currentMessages
      .map((m) => `[${m.role === "user" ? "YOU" : currentPersona.name.toUpperCase()}]\n${m.content}\n`)
      .join("\n---\n\n");
    handleCopyText("all-thread", formatted);
  };

  const handleDraftChange = (val: string) => {
    setDrafts((prev) => ({ ...prev, [mode]: val }));
  };

  const handleClearThread = () => {
    if (confirm(`Clear conversation history for ${currentPersona.name}?`)) {
      setMessages(mode, []);
    }
  };

  const handleSaveStrategy = async (code: string) => {
    setSavingStrategy(code);
    try {
      const name = prompt("Enter a name for this strategy:", "AI_Generated_Strategy");
      if (!name) return;

      const payload = {
        name: name,
        description: "AI Generated Strategy via Agent",
        python_code: code,
      };

      const res = await fetch("/api/v1/strategies", {
        headers: { "Content-Type": "application/json" },
        method: "POST",
        body: JSON.stringify(payload),
      });

      if (!res.ok) throw new Error("Failed to save strategy");

      window.dispatchEvent(new CustomEvent("cryptonite:strategy-updated"));
      alert("Strategy saved successfully! You can now backtest or deploy it.");
    } catch (e: any) {
      alert("Error: " + e.message);
    } finally {
      setSavingStrategy(null);
    }
  };

  const renderMessageContent = (content: string, msgId: string) => {
    const codeBlockRegex = /```([a-zA-Z0-9:-]*)\n([\s\S]*?)```/g;
    const parts = [];
    let lastIndex = 0;
    let match;

    const pythonMatch = content.match(/```(?:python)?\n([\s\S]*?)```/);
    const pythonCodeForManifest = pythonMatch ? pythonMatch[1] : "";

    while ((match = codeBlockRegex.exec(content)) !== null) {
      if (match.index > lastIndex) {
        parts.push(<span key={lastIndex}>{content.substring(lastIndex, match.index)}</span>);
      }

      const lang = match[1].trim();
      const codeText = match[2];
      const codeId = `${msgId}-code-${match.index}`;
      const isCodeCopied = copiedId === codeId;

      let isManifest = lang === "json:manifest";
      if (lang === "json" || lang === "") {
        if (codeText.includes('"slug"') && codeText.includes('"universe"')) {
          isManifest = true;
        }
      }

      if (isManifest) {
        parts.push(
          <StrategyProposalCard
            key={match.index}
            manifestStr={codeText}
            codeText={pythonCodeForManifest}
            msgId={msgId}
          />
        );
      } else {
        parts.push(
          <div key={match.index} className="my-3 bg-zinc-950 border border-zinc-800 rounded-md overflow-hidden select-text">
            <div className="flex justify-between items-center bg-zinc-900 px-3 py-1.5 border-b border-zinc-800 select-none">
              <span className="text-[10px] text-zinc-400 font-bold font-mono">{lang.toUpperCase() || "CODE"}</span>
              <div className="flex items-center space-x-2">
                <button
                  type="button"
                  onClick={() => handleCopyText(codeId, codeText)}
                  className="text-[10px] bg-zinc-800 hover:bg-zinc-700 text-zinc-300 px-2 py-0.5 rounded transition-colors flex items-center space-x-1"
                  title="Copy code snippet"
                >
                  {isCodeCopied ? <Check className="w-3 h-3 text-emerald-400" /> : <Copy className="w-3 h-3" />}
                  <span>{isCodeCopied ? "Copied" : "Copy Code"}</span>
                </button>
                <button
                  type="button"
                  onClick={() => handleSaveStrategy(codeText)}
                  className="text-[10px] bg-cyan-600 hover:bg-cyan-500 text-white px-2 py-0.5 rounded transition-colors font-medium"
                >
                  Save Strategy
                </button>
              </div>
            </div>
            <pre className="p-3 overflow-x-auto text-xs text-zinc-300 font-mono select-text cursor-text">
              {codeText}
            </pre>
          </div>
        );
      }

      lastIndex = match.index + match[0].length;
    }

    if (lastIndex < content.length) {
      parts.push(<span key={lastIndex}>{content.substring(lastIndex)}</span>);
    }

    return parts.length > 0 ? parts : content;
  };

  const executePrompt = async (targetMode: AgentMode, text: string) => {
    const cleanInput = text.trim();
    const cleanBacktestId = backtestIdInput.trim();

    let userMsg = cleanInput;
    if (targetMode === "explain") {
      if (cleanInput) {
        userMsg = cleanBacktestId ? `[Backtest: ${cleanBacktestId}] ${cleanInput}` : cleanInput;
      } else {
        userMsg = `Explain backtest run: ${cleanBacktestId || "latest"}`;
      }
    }

    const newMessage: Message = { id: Date.now().toString(), role: "user", content: userMsg };
    
    addMessage(targetMode, newMessage as any);

    setLoading(true);

    try {
      const endpoint = PERSONA_CONFIG[targetMode].endpoint;
      let payload: any = { message: userMsg };

      if (targetMode === "explain") {
        payload = {
          backtest_id: cleanBacktestId || "latest",
          message: cleanInput || "Please explain this backtest's performance, Sharpe ratio, and drawdown.",
        };
      } else if (targetMode === "strategist") {
        payload = { message: userMsg, instructions: "" };
      }

      const data = await fetchApi(endpoint, {
        method: "POST",
        headers: { "X-CSRF-Token": localStorage.getItem("csrf_token") || "" },
        body: JSON.stringify(payload),
      });

      addMessage(targetMode, { id: (Date.now() + 1).toString(), role: "assistant", content: data.response } as any);
    } catch (err: any) {
      addMessage(targetMode, { id: (Date.now() + 1).toString(), role: "assistant", content: `❌ Error: ${err.message}` } as any);
    } finally {
      setLoading(false);
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const cleanInput = currentDraft.trim();
    const cleanBacktestId = backtestIdInput.trim();

    if (mode === "research" && !cleanInput) return;
    if (mode === "strategist" && !cleanInput) return;
    if (mode === "cro" && !cleanInput) return;
    if (mode === "onchain" && !cleanInput) return;
    if (mode === "explain" && !cleanInput && !cleanBacktestId) return;

    setDrafts((prev) => ({ ...prev, [mode]: "" }));
    await executePrompt(mode, cleanInput);
  };


  if (!isOpen) return null;

  return (
    <div
      ref={panelRef}
      style={
        isDocked
          ? undefined
          : {
              left: `${position.x}px`,
              top: `${position.y}px`,
              width: `${size.width}px`,
              height: `${size.height}px`,
            }
      }
      className={`fixed z-50 bg-zinc-950 border border-zinc-800/80 shadow-2xl shadow-black/80 flex flex-col rounded-xl overflow-hidden backdrop-blur-md transition-shadow ${
        isDocked
          ? "inset-y-0 right-0 w-full max-w-lg border-l rounded-none"
          : "ring-1 ring-white/10"
      }`}
    >


      {/* Draggable Header */}
      <div
        onPointerDown={handleHeaderPointerDown}
        className={`flex items-center justify-between px-4 py-3 border-b border-zinc-800 bg-zinc-900/90 select-none flex-none ${
          !isDocked ? "cursor-grab active:cursor-grabbing" : ""
        }`}
      >
        <div className="flex items-center space-x-2">
          {!isDocked && <GripHorizontal className="w-4 h-4 text-zinc-500 mr-0.5" />}
          <currentPersona.Icon className={`w-5 h-5 ${currentPersona.accentColor}`} />
          <h2 className="text-sm font-bold text-zinc-100 uppercase tracking-wider font-mono">
            {currentPersona.name}
          </h2>
          <span className="text-[10px] text-zinc-500 font-mono hidden sm:inline">
            {!isDocked ? "(drag to move)" : "(docked)"}
          </span>
        </div>

        <div className="flex items-center space-x-1">
          {/* Copy All / Export Thread */}
          {currentMessages.length > 0 && (
            <button
              type="button"
              onClick={handleCopyEntireThread}
              className="text-[10px] text-zinc-400 hover:text-cyan-300 px-2 py-1 rounded hover:bg-zinc-800 transition-colors font-mono flex items-center space-x-1 mr-1"
              title="Copy entire conversation transcript"
              aria-label="Copy entire conversation transcript"
            >
              {copiedId === "all-thread" ? (
                <>
                  <Check className="w-3 h-3 text-emerald-400" />
                  <span className="text-emerald-400 font-bold">Copied</span>
                </>
              ) : (
                <>
                  <Copy className="w-3 h-3" />
                  <span>Copy All</span>
                </>
              )}
            </button>
          )}

          {/* Clear Thread */}
          {currentMessages.length > 0 && (
            <button
              type="button"
              onClick={handleClearThread}
              className="text-[10px] text-zinc-500 hover:text-rose-300 px-2 py-1 rounded hover:bg-zinc-800 transition-colors font-mono mr-1"
              title="Clear conversation history for this agent"
            >
              Clear
            </button>
          )}

          {/* Re-center Position (only when floating) */}
          {!isDocked && (
            <button
              type="button"
              onClick={handleResetPosition}
              className="text-zinc-400 hover:text-zinc-200 p-1.5 rounded hover:bg-zinc-800 transition-colors focus-visible:ring-1 focus-visible:ring-cyan-500 outline-none"
              title="Center window on screen"
              aria-label="Center window on screen"
            >
              <Crosshair className="w-3.5 h-3.5" />
            </button>
          )}

          {/* Dock / Float Toggle */}
          <button
            type="button"
            onClick={() => setIsDocked(!isDocked)}
            className="text-zinc-400 hover:text-zinc-200 p-1.5 rounded hover:bg-zinc-800 transition-colors focus-visible:ring-1 focus-visible:ring-cyan-500 outline-none"
            title={isDocked ? "Float window" : "Dock to right sidebar"}
            aria-label={isDocked ? "Float window" : "Dock to right sidebar"}
          >
            {isDocked ? <Minimize2 className="w-3.5 h-3.5" /> : <Maximize2 className="w-3.5 h-3.5" />}
          </button>

          {/* Close */}
          <button
            type="button"
            onClick={onClose}
            className="text-zinc-400 hover:text-zinc-200 p-1.5 rounded hover:bg-zinc-800 transition-colors focus-visible:ring-1 focus-visible:ring-cyan-500 outline-none"
            title="Close AI Assistant"
            aria-label="Close AI Assistant"
          >
            <X className="w-4 h-4" />
          </button>
        </div>
      </div>

      {/* Agent Persona Selector - 5 Personas */}
      <div className="grid grid-cols-5 bg-zinc-900 border-b border-zinc-800 p-1 select-none flex-none font-mono text-[11px]">
        {(Object.keys(PERSONA_CONFIG) as AgentMode[]).map((pKey) => {
          const cfg = PERSONA_CONFIG[pKey];
          const isActive = mode === pKey;
          const msgCount = threads[pKey].length;

          return (
            <button
              key={pKey}
              onClick={() => setMode(pKey)}
              className={`py-1.5 px-1 font-bold uppercase rounded transition-all flex flex-col items-center justify-center gap-0.5 ${
                isActive
                  ? `bg-zinc-800 ${cfg.accentColor} shadow-sm border ${cfg.borderClass}`
                  : "text-zinc-500 hover:text-zinc-300 hover:bg-zinc-800/40 border border-transparent"
              }`}
            >
              <div className="flex items-center space-x-1">
                <cfg.Icon className="w-3 h-3" />
                <span>{cfg.shortName}</span>
              </div>
              {msgCount > 0 && (
                <span className="text-[9px] px-1 py-0.2 rounded-full bg-zinc-950 text-zinc-400 font-mono">
                  {msgCount}
                </span>
              )}
            </button>
          );
        })}
      </div>

      {/* 1-on-1 Context & Boundary Notice */}
      <div className="px-3 py-1.5 bg-zinc-900/40 border-b border-zinc-800/80 flex items-center justify-between text-[10px] font-mono select-none flex-none">
        <div className="flex items-center space-x-1.5">
          <span className="w-1.5 h-1.5 rounded-full bg-cyan-400 animate-pulse" />
          <span className="text-zinc-300 font-bold">1-on-1 Interactive Copilot:</span>
          <span className="text-zinc-400">{currentPersona.shortName}</span>
        </div>
        <span className="text-zinc-500 hidden sm:inline text-[9px]">
          Background debates run in AI Debates tab
        </span>
      </div>

      {/* Messages Scroll Area - Explicitly Selectable */}
      <div 
        style={{ userSelect: "text", WebkitUserSelect: "text" }}
        className="flex-1 overflow-y-auto p-4 space-y-4 bg-zinc-950/60 select-text cursor-auto"
      >
        {currentMessages.length === 0 && (
          <div className="text-center text-zinc-500 text-sm mt-10 select-none">
            <currentPersona.Icon className={`w-12 h-12 mx-auto mb-3 opacity-25 ${currentPersona.accentColor}`} />
            <p className="font-semibold text-zinc-200">{currentPersona.name}</p>
            <p className="mt-1 text-xs text-zinc-400 max-w-sm mx-auto leading-relaxed">
              {currentPersona.description}
            </p>
            <div className="mt-4 inline-block px-2.5 py-1 rounded-full bg-zinc-900 border border-white/5 text-[10px] font-mono text-zinc-400">
              Isolated conversation thread
            </div>
          </div>
        )}

        {currentMessages.map((msg) => {
          const isCopied = copiedId === msg.id;
          return (
            <div key={msg.id} className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"} group`}>
              <div
                className={`max-w-[88%] rounded-xl p-3.5 relative shadow-sm ${
                  msg.role === "user"
                    ? "bg-cyan-950/40 text-cyan-100 border border-cyan-800/50"
                    : "bg-zinc-900/90 text-zinc-200 border border-zinc-800/80"
                }`}
              >
                <div className="flex items-center justify-between mb-1.5 select-none opacity-80">
                  <div className="flex items-center space-x-1.5">
                    {msg.role === "user" ? (
                      <UserIcon className="w-3 h-3 text-cyan-400" />
                    ) : (
                      <currentPersona.Icon className={`w-3 h-3 ${currentPersona.accentColor}`} />
                    )}
                    <span className="text-[10px] uppercase font-bold tracking-wider font-mono">
                      {msg.role === "user" ? "You" : currentPersona.shortName}
                    </span>
                  </div>

                  {/* Copy Button on Message Header */}
                  <button
                    type="button"
                    onClick={() => handleCopyText(msg.id, msg.content)}
                    className="opacity-70 group-hover:opacity-100 hover:text-cyan-300 text-zinc-400 px-1.5 py-0.5 rounded bg-zinc-800/60 hover:bg-zinc-800 border border-white/5 transition-all flex items-center space-x-1 text-[10px] font-mono select-none"
                    title="Copy full message text"
                    aria-label="Copy full message text"
                  >
                    {isCopied ? (
                      <>
                        <Check className="w-3 h-3 text-emerald-400" />
                        <span className="text-emerald-400">Copied</span>
                      </>
                    ) : (
                      <>
                        <Copy className="w-3 h-3" />
                        <span>Copy</span>
                      </>
                    )}
                  </button>
                </div>

                {/* Message Body with Text Selection */}
                <div 
                  style={{ userSelect: "text", WebkitUserSelect: "text" }}
                  className="text-xs leading-relaxed whitespace-pre-wrap font-mono select-text cursor-text text-zinc-200"
                >
                  {renderMessageContent(msg.content, msg.id)}
                </div>
              </div>
            </div>
          );
        })}

        {loading && (
          <div className="flex justify-start">
            <div className="bg-zinc-900/90 text-zinc-400 border border-zinc-800 rounded-xl p-3 flex items-center space-x-2">
              <Loader2 className={`w-4 h-4 animate-spin ${currentPersona.accentColor}`} />
              <span className="text-xs font-mono">{currentPersona.shortName} analyzing...</span>
            </div>
          </div>
        )}
        <div ref={messagesEndRef} />
      </div>

      {/* Input Form */}
      <div className="p-3.5 bg-zinc-900 border-t border-zinc-800 flex-none select-none">
        <form onSubmit={handleSubmit} className="relative">
          {mode === "explain" && (
            <div className="mb-2">
              <label className="text-[11px] font-mono text-zinc-400 mb-1 block flex items-center space-x-1">
                <Info className="w-3 h-3 text-amber-400" />
                <span>Target Backtest ID (optional, defaults to latest)</span>
              </label>
              <input
                type="text"
                value={backtestIdInput}
                onChange={(e) => setBacktestIdInput(e.target.value)}
                placeholder="e.g. 7abf5fae-fe31-4348-b290-a2be86687275"
                className="w-full bg-zinc-950 border border-zinc-800 rounded px-3 py-1.5 text-xs text-zinc-200 focus:border-amber-500 focus:outline-none mb-2 font-mono select-text"
              />
            </div>
          )}

          <div className="flex items-center space-x-2">
            <input
              type="text"
              value={currentDraft}
              onChange={(e) => handleDraftChange(e.target.value)}
              placeholder={currentPersona.placeholder}
              className="flex-1 bg-zinc-950 border border-zinc-800 rounded-full px-4 py-2 text-xs text-zinc-200 focus:border-cyan-500 focus:outline-none font-sans select-text"
            />
            <button
              type="submit"
              disabled={
                loading ||
                (mode !== "explain" && !currentDraft.trim()) ||
                (mode === "explain" && !currentDraft.trim() && !backtestIdInput.trim())
              }
              className="p-2.5 bg-cyan-600 hover:bg-cyan-500 text-white rounded-full transition-colors disabled:opacity-50 disabled:cursor-not-allowed focus-visible:ring-2 focus-visible:ring-cyan-500 outline-none"
              title={`Send message to ${currentPersona.shortName}`}
              aria-label={`Send message to ${currentPersona.shortName}`}
            >
              <Send className="w-3.5 h-3.5" />
            </button>
          </div>
        </form>
      </div>

      {/* 8-Direction Resizing Handles (only when floating) */}
      {!isDocked && (
        <>
          {/* North Edge */}
          <div
            onPointerDown={(e) => startResize("n", e)}
            className="absolute top-0 inset-x-2 h-2 cursor-ns-resize hover:bg-cyan-500/50 z-30 transition-colors"
            title="Drag to resize height"
          />
          {/* South Edge */}
          <div
            onPointerDown={(e) => startResize("s", e)}
            className="absolute bottom-0 inset-x-2 h-2 cursor-ns-resize hover:bg-cyan-500/50 z-30 transition-colors"
            title="Drag to resize height"
          />
          {/* West Edge */}
          <div
            onPointerDown={(e) => startResize("w", e)}
            className="absolute inset-y-2 left-0 w-2 cursor-ew-resize hover:bg-cyan-500/50 z-30 transition-colors"
            title="Drag to resize width"
          />
          {/* East Edge */}
          <div
            onPointerDown={(e) => startResize("e", e)}
            className="absolute inset-y-2 right-0 w-2 cursor-ew-resize hover:bg-cyan-500/50 z-30 transition-colors"
            title="Drag to resize width"
          />
          {/* North-West Corner */}
          <div
            onPointerDown={(e) => startResize("nw", e)}
            className="absolute top-0 left-0 w-3 h-3 cursor-nwse-resize z-40 hover:bg-cyan-500/50"
            title="Resize window"
          />
          {/* North-East Corner */}
          <div
            onPointerDown={(e) => startResize("ne", e)}
            className="absolute top-0 right-0 w-3 h-3 cursor-nesw-resize z-40 hover:bg-cyan-500/50"
            title="Resize window"
          />
          {/* South-West Corner */}
          <div
            onPointerDown={(e) => startResize("sw", e)}
            className="absolute bottom-0 left-0 w-3 h-3 cursor-nesw-resize z-40 hover:bg-cyan-500/50"
            title="Resize window"
          />
          {/* South-East Corner (with visual grip indicator) */}
          <div
            onPointerDown={(e) => startResize("se", e)}
            className="absolute bottom-0 right-0 w-4 h-4 cursor-nwse-resize z-40 flex items-center justify-center group"
            title="Drag to resize window"
          >
            <div className="w-1.5 h-1.5 border-r-2 border-b-2 border-zinc-500 group-hover:border-cyan-400 transition-colors" />
          </div>
        </>
      )}
    </div>
  );
}
