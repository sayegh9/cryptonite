"use client";

import { useState, useEffect, useRef } from "react";
import { fetchApi } from "@/lib/api";
import { X, Send, Bot, User as UserIcon, Loader2, Info } from "lucide-react";

interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
}

export function AgentChatPanel({ 
  isOpen, 
  onClose,
  activeBacktestId
}: { 
  isOpen: boolean; 
  onClose: () => void;
  activeBacktestId?: string | null;
}) {
  const [mode, setMode] = useState<"research" | "explain">("research");
  const [messages, setMessages] = useState<Message[]>([]);
  const [inputValue, setInputValue] = useState("");
  const [loading, setLoading] = useState(false);
  const [backtestIdInput, setBacktestIdInput] = useState("");
  const messagesEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (isOpen && activeBacktestId) {
      setMode("explain");
      setBacktestIdInput(activeBacktestId);
    }
  }, [isOpen, activeBacktestId]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (mode === "research" && !inputValue.trim()) return;
    if (mode === "explain" && !backtestIdInput.trim()) return;

    let userMsg = inputValue;
    if (mode === "explain") {
      userMsg = `Explain backtest run: ${backtestIdInput}`;
    }

    const newMessage: Message = { id: Date.now().toString(), role: "user", content: userMsg };
    setMessages((prev) => [...prev, newMessage]);
    setInputValue("");
    setLoading(true);

    try {
      const endpoint = mode === "research" ? "/agents/chat" : "/agents/explain";
      const payload = mode === "research" 
        ? { message: userMsg } 
        : { backtest_id: backtestIdInput, message: "Please explain this backtest." };

      // Make raw fetch call since fetchApi uses /api/v1 prefix but agents uses the proxy to 8085
      const res = await fetch(`/api/v1${endpoint}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      
      const data = await res.json();
      
      if (!res.ok) throw new Error(data.detail || "Agent request failed");

      setMessages((prev) => [
        ...prev,
        { id: Date.now().toString(), role: "assistant", content: data.response }
      ]);
    } catch (err: any) {
      setMessages((prev) => [
        ...prev,
        { id: Date.now().toString(), role: "assistant", content: `❌ Error: ${err.message}` }
      ]);
    } finally {
      setLoading(false);
    }
  };

  if (!isOpen) return null;

  return (
    <div className="fixed inset-y-0 right-0 z-40 w-full max-w-md bg-zinc-950 border-l border-zinc-800 shadow-2xl flex flex-col transform transition-transform duration-300">
      <div className="flex items-center justify-between px-5 py-4 border-b border-zinc-800 bg-zinc-900/50">
        <div className="flex items-center space-x-2">
          <Bot className="w-5 h-5 text-cyan-400" />
          <h2 className="text-lg font-bold text-zinc-100 uppercase tracking-wide">Cryptonite AI</h2>
        </div>
        <button onClick={onClose} className="text-zinc-500 hover:text-zinc-300 transition-colors">
          <X className="w-5 h-5" />
        </button>
      </div>

      <div className="flex bg-zinc-900 border-b border-zinc-800 p-1">
        <button
          onClick={() => setMode("research")}
          className={`flex-1 py-2 text-xs font-bold uppercase rounded ${
            mode === "research" ? "bg-zinc-800 text-cyan-400" : "text-zinc-500 hover:text-zinc-300"
          }`}
        >
          Research Agent
        </button>
        <button
          onClick={() => setMode("explain")}
          className={`flex-1 py-2 text-xs font-bold uppercase rounded ${
            mode === "explain" ? "bg-zinc-800 text-cyan-400" : "text-zinc-500 hover:text-zinc-300"
          }`}
        >
          Explainer Agent
        </button>
      </div>

      <div className="flex-1 overflow-y-auto p-4 space-y-4 bg-zinc-950/50">
        {messages.length === 0 && (
          <div className="text-center text-zinc-500 text-sm mt-10">
            <Bot className="w-12 h-12 mx-auto mb-3 opacity-20" />
            <p>I am the Cryptonite AI Assistant.</p>
            <p className="mt-1">I can help you research markets, write custom trading strategies in Python, or analyze backtest results.</p>
          </div>
        )}
        
        {messages.map((msg) => (
          <div key={msg.id} className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}>
            <div className={`max-w-[85%] rounded-xl p-3 ${
              msg.role === "user" 
                ? "bg-cyan-900/40 text-cyan-100 border border-cyan-800/50" 
                : "bg-zinc-900 text-zinc-300 border border-zinc-800"
            }`}>
              <div className="flex items-center space-x-2 mb-1 opacity-70">
                {msg.role === "user" ? <UserIcon className="w-3 h-3" /> : <Bot className="w-3 h-3" />}
                <span className="text-[10px] uppercase font-bold tracking-wider">{msg.role}</span>
              </div>
              <div className="text-sm leading-relaxed whitespace-pre-wrap font-mono">
                {msg.content}
              </div>
            </div>
          </div>
        ))}
        {loading && (
          <div className="flex justify-start">
            <div className="bg-zinc-900 text-zinc-400 border border-zinc-800 rounded-xl p-3 flex items-center space-x-2">
              <Loader2 className="w-4 h-4 animate-spin" />
              <span className="text-sm font-mono">Agent thinking...</span>
            </div>
          </div>
        )}
        <div ref={messagesEndRef} />
      </div>

      <div className="p-4 bg-zinc-900 border-t border-zinc-800">
        <form onSubmit={handleSubmit} className="relative">
          {mode === "explain" && (
            <div className="mb-2">
              <label className="text-xs font-mono text-zinc-400 mb-1 block flex items-center space-x-1">
                <Info className="w-3 h-3" />
                <span>Target Backtest ID</span>
              </label>
              <input
                type="text"
                value={backtestIdInput}
                onChange={(e) => setBacktestIdInput(e.target.value)}
                placeholder="e.g. 12345678-1234..."
                className="w-full bg-zinc-950 border border-zinc-800 rounded px-3 py-2 text-sm text-zinc-200 focus:border-cyan-500 focus:outline-none mb-2 font-mono"
              />
            </div>
          )}
          
          <div className="flex items-center space-x-2">
            {mode === "research" && (
              <input
                type="text"
                value={inputValue}
                onChange={(e) => setInputValue(e.target.value)}
                placeholder="Ask about markets or strategy code..."
                className="flex-1 bg-zinc-950 border border-zinc-800 rounded-full px-4 py-2 text-sm text-zinc-200 focus:border-cyan-500 focus:outline-none"
              />
            )}
            <button
              type="submit"
              disabled={loading || (mode === "research" && !inputValue.trim()) || (mode === "explain" && !backtestIdInput.trim())}
              className="p-2.5 bg-cyan-600 hover:bg-cyan-500 text-white rounded-full transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <Send className="w-4 h-4" />
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
