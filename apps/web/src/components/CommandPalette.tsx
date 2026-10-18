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
  Gauge,
  SlidersHorizontal,
  Cpu,
  ShieldCheck,
  Key,
  Server,
} from "lucide-react";
import {
  dispatchSwitchWorkspace,
  NAV_GROUPS,
  VIEWS,
  type WorkspaceMode,
  type SettingsSection,
} from "@/lib/navigation";

const MODE_ICON: Record<WorkspaceMode, React.ComponentType<{ className?: string; "aria-hidden"?: boolean | "true" }>> = {
  terminal: Terminal,
  markets: TrendingUp,
  portfolio: Briefcase,
  research: FlaskConical,
  debates: BrainCircuit,
  mission: Gauge,
  operator: Activity,
  alerts: Bell,
  settings: Settings,
};

/** Header groups expanded with every sub-view (Brain Home/Debates, Mission tabs, Scanner). */
const PALETTE_GROUPS = NAV_GROUPS.map((g) => ({
  label: g.label,
  entries: g.items.flatMap((item) => {
    const icon = item.view === "scanner" ? Search : MODE_ICON[item.mode];
    const subViews = item.view ? [] : VIEWS[item.mode]?.filter((v) => v.id !== "scanner") ?? [];
    return [
      { mode: item.mode, view: item.view, label: item.label, icon },
      ...subViews.map((v) => ({ mode: item.mode, view: v.id, label: `${item.label} → ${v.label}`, icon })),
    ];
  }),
}));

const SETTINGS_PALETTE_ENTRIES: {
  id: SettingsSection;
  label: string;
  badge: string;
  icon: React.ComponentType<{ className?: string; "aria-hidden"?: boolean | "true" }>;
  keywords: string;
}[] = [
  {
    id: "preferences",
    label: "Settings: Desk Preferences (Theme, Audio, Density)",
    badge: "Preferences",
    icon: SlidersHorizontal,
    keywords: "desk preferences theme dark light system density compact comfortable audio volume chime pnl format currency timezone",
  },
  {
    id: "ai",
    label: "Settings: AI Gateway & Model Routing",
    badge: "AI Gateway",
    icon: Cpu,
    keywords: "ai gateway model routing providers gemini local fallback temperature tier",
  },
  {
    id: "exit",
    label: "Settings: Exit Rules & Stop Loss",
    badge: "Exit Rules",
    icon: ShieldAlert,
    keywords: "exit rules stop loss trailing takeprofit breakeven ladder liquidation rmultiples",
  },
  {
    id: "risk",
    label: "Settings: Risk & Guardrails",
    badge: "Risk",
    icon: ShieldCheck,
    keywords: "risk guardrails exposure caps leverage max drawdown mandate limits",
  },
  {
    id: "scanner",
    label: "Settings: Scanner & Universe",
    badge: "Scanner",
    icon: Search,
    keywords: "scanner universe polars filters momentum breakout screening refresh",
  },
  {
    id: "alerts",
    label: "Settings: Alerts & Integrations",
    badge: "Alerts",
    icon: Bell,
    keywords: "alerts integrations webhooks telegram discord slack routing",
  },
  {
    id: "security",
    label: "Settings: Security & Session Policy",
    badge: "Security",
    icon: Key,
    keywords: "security session policy totp 2fa operator auth timeout apikey",
  },
  {
    id: "system",
    label: "Settings: System Health & Audit Log",
    badge: "System",
    icon: Server,
    keywords: "system health audit log database postgres redis migration ledger",
  },
];


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

  // Listen for programmatic command palette trigger events
  React.useEffect(() => {
    const handleOpen = () => setOpen(true);
    window.addEventListener("cryptonite:open-command-palette", handleOpen);
    return () => window.removeEventListener("cryptonite:open-command-palette", handleOpen);
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

  const switchWorkspace = React.useCallback((mode: WorkspaceMode, view?: string) => {
    onClose();
    dispatchSwitchWorkspace({ mode, view });
  }, [onClose]);

  const switchSettingsSection = React.useCallback(
    (section: SettingsSection) => {
      onClose();
      dispatchSwitchWorkspace({ mode: "settings", section });
    },
    [onClose],
  );


  return (
    <CommandDialog
      open={open}
      onOpenChange={setOpen}
      className="backdrop-blur-md bg-zinc-950/90 border border-white/10 shadow-2xl shadow-cyan-950/20"
    >
      <CommandInput
        placeholder="Type a command, search pairs, or trigger AI debate... (⌘K)"
        aria-label="Search commands, markets, and settings"
        value={query}
        onValueChange={setQuery}
      />
      <CommandList className="max-h-96 overflow-y-auto p-2 font-sans text-xs">
        <CommandEmpty className="p-4 text-center text-zinc-400 font-mono text-xs">
          No matching commands or assets found.
        </CommandEmpty>

        {searchResults.length > 0 && (
          <>
            <CommandGroup heading="Markets" className="text-zinc-400 font-semibold px-2 py-1.5 uppercase text-2xs tracking-wider">
              {searchResults.map((result) => (
                <CommandItem
                  key={result.symbol}
                  value={result.symbol}
                  onSelect={() => selectSymbol(result.symbol)}
                  className="flex items-center justify-between px-3 py-2 rounded-lg cursor-pointer transition-colors text-zinc-200 data-[selected=true]:bg-cyan-500/10 data-[selected=true]:text-cyan-400"
                >
                  <div className="flex items-center space-x-2.5">
                    <TrendingUp className="mr-2 h-4 w-4 text-cyan-400" aria-hidden="true" />
                    <span className="font-mono font-bold">{result.symbol}</span>
                    <span className="ml-2 text-xs text-zinc-400">
                      {result.base} / USDT — Binance
                    </span>
                  </div>
                  <span className="font-mono text-2xs text-zinc-400">Binance</span>
                </CommandItem>
              ))}
            </CommandGroup>
            <CommandSeparator className="my-1 border-white/5" />
          </>
        )}

        <CommandGroup heading="AI Agents & Debates" className="text-zinc-400 font-semibold px-2 py-1.5 uppercase text-2xs tracking-wider">
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
              <BrainCircuit className="mr-2 h-4 w-4 text-purple-400" aria-hidden="true" />
              <span className="font-semibold">Trigger New Consensus Debate (BTCUSDT)</span>
            </div>
            <CommandShortcut className="font-mono text-2xs text-zinc-400">⌘D</CommandShortcut>
          </CommandItem>

          <CommandItem
            onSelect={() => switchWorkspace("debates")}
            className="flex items-center justify-between px-3 py-2 rounded-lg cursor-pointer transition-colors text-zinc-200 data-[selected=true]:bg-purple-500/10 data-[selected=true]:text-purple-400"
          >
            <div className="flex items-center space-x-2.5">
              <BrainCircuit className="mr-2 h-4 w-4 text-purple-400" aria-hidden="true" />
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
              <Sparkles className="mr-2 h-4 w-4 text-cyan-400" aria-hidden="true" />
              <span>Launch AI Copilot Assistant</span>
            </div>
            <span className="text-2xs font-mono text-zinc-400">Copilot</span>
          </CommandItem>
        </CommandGroup>

        <CommandSeparator className="my-1 border-white/5" />

        {/* Every workspace and sub-view, grouped like the header (R6.2) */}
        {PALETTE_GROUPS.map((group) => (
          <React.Fragment key={group.label}>
            <CommandGroup heading={group.label} className="text-zinc-400 font-semibold px-2 py-1.5 uppercase text-2xs tracking-wider">
              {group.entries.map((entry) => {
                const Icon = entry.icon;
                return (
                  <CommandItem
                    key={`${entry.mode}-${entry.view ?? ""}`}
                    value={`${group.label} ${entry.label}`}
                    onSelect={() => switchWorkspace(entry.mode, entry.view)}
                    className="flex items-center justify-between px-3 py-2 rounded-lg cursor-pointer transition-colors text-zinc-200 data-[selected=true]:bg-cyan-500/10 data-[selected=true]:text-cyan-400"
                  >
                    <div className="flex items-center space-x-2.5">
                      <Icon className="mr-2 h-4 w-4 text-cyan-400" aria-hidden="true" />
                      <span>{entry.label}</span>
                    </div>
                  </CommandItem>
                );
              })}
            </CommandGroup>
            <CommandSeparator className="my-1 border-white/5" />
          </React.Fragment>
        ))}

        {/* Settings & Configuration deep-link entries */}
        <CommandGroup heading="Settings & Configuration" className="text-zinc-400 font-semibold px-2 py-1.5 uppercase text-2xs tracking-wider">

          {SETTINGS_PALETTE_ENTRIES.map((entry) => {
            const Icon = entry.icon;
            return (
              <CommandItem
                key={entry.id}
                value={`${entry.label} ${entry.keywords}`}
                onSelect={() => switchSettingsSection(entry.id)}
                className="flex items-center justify-between px-3 py-2 rounded-lg cursor-pointer transition-colors text-zinc-200 data-[selected=true]:bg-cyan-500/10 data-[selected=true]:text-cyan-400"
              >
                <div className="flex items-center space-x-2.5">
                  <Icon className="mr-2 h-4 w-4 text-cyan-400" aria-hidden="true" />
                  <span>{entry.label}</span>
                </div>
                <span className="text-2xs font-mono text-zinc-400">{entry.badge}</span>
              </CommandItem>
            );
          })}
        </CommandGroup>

        <CommandSeparator className="my-1 border-white/5" />

        <CommandGroup heading="Risk & Controls" className="text-zinc-400 font-semibold px-2 py-1.5 uppercase text-2xs tracking-wider">

          <CommandItem
            onSelect={() => {
              onClose();
              window.dispatchEvent(new CustomEvent("cryptonite:trigger-kill-switch"));
            }}
            className="flex items-center justify-between px-3 py-2 rounded-lg cursor-pointer transition-colors text-rose-400 data-[selected=true]:bg-rose-500/15 data-[selected=true]:text-rose-300"
          >
            <div className="flex items-center space-x-2.5">
              <ShieldAlert className="mr-2 h-4 w-4 text-rose-500" aria-hidden="true" />
              <span className="font-bold">EMERGENCY KILL SWITCH — Flatten All Positions</span>
            </div>
            <span className="px-1.5 py-0.5 rounded bg-rose-950/60 border border-rose-800/50 text-2xs font-mono text-rose-400 font-bold">
              DANGER
            </span>
          </CommandItem>
        </CommandGroup>
      </CommandList>

      <div className="h-8 border-t border-white/5 bg-zinc-900/50 px-4 flex items-center justify-between text-2xs text-zinc-400 font-mono">
        <span>Navigate: ↑ ↓ | Select: ↵</span>
        <span>Cryptonite Institutional OS</span>
      </div>
    </CommandDialog>
  );
}
