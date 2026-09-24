"use client";

import * as React from "react";
import { toast } from "sonner";
import {
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandSeparator,
  CommandShortcut,
} from "@/components/ui/command";
import {
  Terminal,
  Activity,
  Briefcase,
  Search,
  FlaskConical,
  TrendingUp,
  BrainCircuit,
  Sparkles,
  ShieldAlert,
  Bell,
  Settings,
} from "lucide-react";

interface MarketSearchResult {
  symbol: string;
  base: string;
  quote: string;
}

export function CommandPalette() {
  const [open, setOpen] = React.useState(false);
  const [query, setQuery] = React.useState("");
  const [searchResults, setSearchResults] = React.useState<MarketSearchResult[]>([]);

  // Keyboard shortcut listener: ⌘K / Ctrl+K for palette, ⌘D / Ctrl+D for debates
  React.useEffect(() => {
    const down = (e: KeyboardEvent) => {
      if ((e.key === "k" || e.key === "K") && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        setOpen((prev) => !prev);
      } else if ((e.key === "d" || e.key === "D") && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        window.dispatchEvent(new CustomEvent("cryptonite:switch-workspace", { detail: { mode: "debates" } }));
      }
    };
    document.addEventListener("keydown", down);
    return () => document.removeEventListener("keydown", down);
  }, []);

  // Reset query & results when palette is closed
  React.useEffect(() => {
    if (!open) {
      setQuery("");
      setSearchResults([]);
    }
  }, [open]);

  // Debounced market search — fires 300 ms after query settles, min 2 chars
  React.useEffect(() => {
    if (query.length < 2) {
      setSearchResults([]);
      return;
    }
    const timer = setTimeout(async () => {
      try {
        const res = await fetch(`/api/v1/market/search?q=${encodeURIComponent(query)}`);
        if (!res.ok) return;
        const data: MarketSearchResult[] = await res.json();
        setSearchResults(data);
      } catch {
        // Silently ignore network errors
      }
    }, 300);
    return () => clearTimeout(timer);
  }, [query]);

  const onClose = React.useCallback(() => setOpen(false), []);

  const selectSymbol = React.useCallback((symbol: string) => {
    onClose();
    window.dispatchEvent(new CustomEvent("cryptonite:select-symbol", { detail: { symbol } }));
  }, [onClose]);

  const switchWorkspace = React.useCallback((mode: string) => {
    onClose();
    window.dispatchEvent(new CustomEvent("cryptonite:switch-workspace", { detail: { mode } }));
  }, [onClose]);

  return (
    <CommandDialog
      open={open}
      onOpenChange={setOpen}
      className="backdrop-blur-md bg-zinc-950/90 border border-white/10 shadow-2xl shadow-cyan-950/20"
    >
      <CommandInput
        placeholder="Type a command, search pairs, or trigger AI debate... (⌘K)"
        value={query}
        onValueChange={setQuery}
      />
      <CommandList className="max-h-96 overflow-y-auto p-2 font-sans text-xs">
        <CommandEmpty className="p-4 text-center text-zinc-500 font-mono text-xs">
          No matching commands or assets found.
        </CommandEmpty>

        {searchResults.length > 0 && (
          <>
            <CommandGroup heading="Markets" className="text-zinc-500 font-semibold px-2 py-1.5 uppercase text-[10px] tracking-wider">
              {searchResults.map((result) => (
                <CommandItem
                  key={result.symbol}
                  value={result.symbol}
                  onSelect={() => selectSymbol(result.symbol)}
                  className="flex items-center justify-between px-3 py-2 rounded-lg cursor-pointer transition-colors text-zinc-200 data-[selected=true]:bg-cyan-500/10 data-[selected=true]:text-cyan-400"
                >
                  <div className="flex items-center space-x-2.5">
                    <TrendingUp className="mr-2 h-4 w-4 text-cyan-400" />
                    <span className="font-mono font-bold">{result.symbol}</span>
                    <span className="ml-2 text-xs text-zinc-500">
                      {result.base} / USDT — Binance
                    </span>
                  </div>
                  <span className="font-mono text-[11px] text-zinc-400">Binance</span>
                </CommandItem>
              ))}
            </CommandGroup>
            <CommandSeparator className="my-1 border-white/5" />
          </>
        )}

        <CommandGroup heading="AI Agents & Debates" className="text-zinc-500 font-semibold px-2 py-1.5 uppercase text-[10px] tracking-wider">
          <CommandItem
            onSelect={() => {
              fetch("/agent-api/debates/trigger", {
                method: "POST",
                credentials: "include",
                headers: { 
                  "Content-Type": "application/json",
                  "X-CSRF-Token": typeof window !== "undefined" ? localStorage.getItem("csrf_token") || "" : "",
                },
                body: JSON.stringify({ symbol: "BTCUSDT" }),
              })
                .then(async (res) => {
                  if (res.ok) {
                    toast.success("AI Debate Queued: BTCUSDT", {
                      description: "Bull vs Bear agents are reviewing order flow.",
                    });
                  }
                })
                .catch(() => {});
              switchWorkspace("debates");
            }}
            className="flex items-center justify-between px-3 py-2 rounded-lg cursor-pointer transition-colors text-zinc-200 data-[selected=true]:bg-purple-500/10 data-[selected=true]:text-purple-400"
          >
            <div className="flex items-center space-x-2.5">
              <BrainCircuit className="mr-2 h-4 w-4 text-purple-400" />
              <span className="font-semibold">Trigger New Consensus Debate (BTCUSDT)</span>
            </div>
            <CommandShortcut className="font-mono text-[10px] text-zinc-500">⌘D</CommandShortcut>
          </CommandItem>

          <CommandItem
            onSelect={() => switchWorkspace("debates")}
            className="flex items-center justify-between px-3 py-2 rounded-lg cursor-pointer transition-colors text-zinc-200 data-[selected=true]:bg-purple-500/10 data-[selected=true]:text-purple-400"
          >
            <div className="flex items-center space-x-2.5">
              <BrainCircuit className="mr-2 h-4 w-4 text-purple-400" />
              <span>Go to AI Debates Dashboard</span>
            </div>
          </CommandItem>

          <CommandItem
            onSelect={() => {
              onClose();
              window.dispatchEvent(new CustomEvent("cryptonite:open-copilot"));
            }}
            className="flex items-center justify-between px-3 py-2 rounded-lg cursor-pointer transition-colors text-zinc-200 data-[selected=true]:bg-cyan-500/10 data-[selected=true]:text-cyan-400"
          >
            <div className="flex items-center space-x-2.5">
              <Sparkles className="mr-2 h-4 w-4 text-cyan-400" />
              <span>Launch AI Copilot Assistant</span>
            </div>
            <span className="text-[10px] font-mono text-zinc-500">Copilot</span>
          </CommandItem>
        </CommandGroup>

        <CommandSeparator className="my-1 border-white/5" />

        <CommandGroup heading="Workspaces" className="text-zinc-500 font-semibold px-2 py-1.5 uppercase text-[10px] tracking-wider">
          <CommandItem
            onSelect={() => switchWorkspace("terminal")}
            className="flex items-center justify-between px-3 py-2 rounded-lg cursor-pointer transition-colors text-zinc-200 data-[selected=true]:bg-cyan-500/10 data-[selected=true]:text-cyan-400"
          >
            <div className="flex items-center space-x-2.5">
              <Terminal className="mr-2 h-4 w-4 text-cyan-400" />
              <span>Trading Terminal (3-Column Workspace)</span>
            </div>
          </CommandItem>

          <CommandItem
            onSelect={() => switchWorkspace("research")}
            className="flex items-center justify-between px-3 py-2 rounded-lg cursor-pointer transition-colors text-zinc-200 data-[selected=true]:bg-cyan-500/10 data-[selected=true]:text-cyan-400"
          >
            <div className="flex items-center space-x-2.5">
              <FlaskConical className="mr-2 h-4 w-4 text-emerald-400" />
              <span>Research Lab (Nautilus Backtester)</span>
            </div>
          </CommandItem>

          <CommandItem
            onSelect={() => switchWorkspace("portfolio")}
            className="flex items-center justify-between px-3 py-2 rounded-lg cursor-pointer transition-colors text-zinc-200 data-[selected=true]:bg-cyan-500/10 data-[selected=true]:text-cyan-400"
          >
            <div className="flex items-center space-x-2.5">
              <Briefcase className="mr-2 h-4 w-4 text-amber-400" />
              <span>Portfolio & Position Sizing Blotter</span>
            </div>
          </CommandItem>

          <CommandItem
            onSelect={() => switchWorkspace("alerts")}
            className="flex items-center justify-between px-3 py-2 rounded-lg cursor-pointer transition-colors text-zinc-200 data-[selected=true]:bg-cyan-500/10 data-[selected=true]:text-cyan-400"
          >
            <div className="flex items-center space-x-2.5">
              <Bell className="mr-2 h-4 w-4 text-cyan-400" />
              <span>Background Price & Indicator Alerts</span>
            </div>
          </CommandItem>

          <CommandItem
            onSelect={() => switchWorkspace("operator")}
            className="flex items-center justify-between px-3 py-2 rounded-lg cursor-pointer transition-colors text-zinc-200 data-[selected=true]:bg-cyan-500/10 data-[selected=true]:text-cyan-400"
          >
            <div className="flex items-center space-x-2.5">
              <Activity className="mr-2 h-4 w-4 text-zinc-300" />
              <span>Operator Governance & Strategy Instances</span>
            </div>
          </CommandItem>
        </CommandGroup>

        <CommandSeparator className="my-1 border-white/5" />

        <CommandGroup heading="Actions" className="text-zinc-500 font-semibold px-2 py-1.5 uppercase text-[10px] tracking-wider">
          <CommandItem
            onSelect={() => {
              switchWorkspace("terminal");
            }}
            className="flex items-center justify-between px-3 py-2 rounded-lg cursor-pointer transition-colors text-zinc-200 data-[selected=true]:bg-cyan-500/10 data-[selected=true]:text-cyan-400"
          >
            <div className="flex items-center space-x-2.5">
              <Search className="mr-2 h-4 w-4 text-cyan-400" />
              <span>Focus Vectorized Polars Scanner</span>
            </div>
          </CommandItem>

          <CommandItem
            onSelect={() => switchWorkspace("settings")}
            className="flex items-center justify-between px-3 py-2 rounded-lg cursor-pointer transition-colors text-zinc-200 data-[selected=true]:bg-cyan-500/10 data-[selected=true]:text-cyan-400"
          >
            <div className="flex items-center space-x-2.5">
              <Settings className="mr-2 h-4 w-4 text-zinc-400" />
              <span>System & Risk Configuration Settings</span>
            </div>
          </CommandItem>
        </CommandGroup>

        <CommandSeparator className="my-1 border-white/5" />

        <CommandGroup heading="Risk & Controls" className="text-zinc-500 font-semibold px-2 py-1.5 uppercase text-[10px] tracking-wider">
          <CommandItem
            onSelect={() => {
              onClose();
              window.dispatchEvent(new CustomEvent("cryptonite:trigger-kill-switch"));
            }}
            className="flex items-center justify-between px-3 py-2 rounded-lg cursor-pointer transition-colors text-rose-400 data-[selected=true]:bg-rose-500/15 data-[selected=true]:text-rose-300"
          >
            <div className="flex items-center space-x-2.5">
              <ShieldAlert className="mr-2 h-4 w-4 text-rose-500" />
              <span className="font-bold">EMERGENCY KILL SWITCH — Flatten All Positions</span>
            </div>
            <span className="px-1.5 py-0.5 rounded bg-rose-950/60 border border-rose-800/50 text-[10px] font-mono text-rose-400 font-bold">
              DANGER
            </span>
          </CommandItem>
        </CommandGroup>
      </CommandList>

      <div className="h-8 border-t border-white/5 bg-zinc-900/50 px-4 flex items-center justify-between text-[11px] text-zinc-500 font-mono">
        <span>Navigate: ↑ ↓ | Select: ↵</span>
        <span>Cryptonite Institutional OS</span>
      </div>
    </CommandDialog>
  );
}
