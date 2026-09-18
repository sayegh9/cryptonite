"use client"

import * as React from "react"
import { useRouter, usePathname } from "next/navigation"
import {
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandSeparator,
  CommandShortcut,
  Command
} from "@/components/ui/command"
import {
  Terminal,
  Activity,
  Briefcase,
  AlertOctagon,
  Search,
  FlaskConical,
  TrendingUp
} from "lucide-react"

interface MarketSearchResult {
  symbol: string;
  base: string;
  quote: string;
}

export function CommandPalette() {
  const [open, setOpen] = React.useState(false)
  const [query, setQuery] = React.useState("")
  const [searchResults, setSearchResults] = React.useState<MarketSearchResult[]>([])
  const router = useRouter()
  const pathname = usePathname()

  React.useEffect(() => {
    const down = (e: KeyboardEvent) => {
      if (e.key === "k" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault()
        setOpen((open) => !open)
      }
    }
    document.addEventListener("keydown", down)
    return () => document.removeEventListener("keydown", down)
  }, [])

  // Reset query & results when palette is closed
  React.useEffect(() => {
    if (!open) {
      setQuery("")
      setSearchResults([])
    }
  }, [open])

  // Debounced market search — fires 300 ms after query settles, min 2 chars
  React.useEffect(() => {
    if (query.length < 2) {
      setSearchResults([])
      return
    }
    const timer = setTimeout(async () => {
      try {
        const res = await fetch(`/api/v1/market/search?q=${encodeURIComponent(query)}`)
        if (!res.ok) return
        const data: MarketSearchResult[] = await res.json()
        setSearchResults(data)
      } catch (_e) {
        // Silently ignore network errors
      }
    }, 300)
    return () => clearTimeout(timer)
  }, [query])

  const onClose = React.useCallback(() => setOpen(false), [])

  const runCommand = React.useCallback((command: () => void) => {
    setOpen(false)
    command()
  }, [])

  const selectSymbol = React.useCallback((symbol: string) => {
    onClose()
    window.dispatchEvent(new CustomEvent("cryptonite:select-symbol", { detail: { symbol } }))
  }, [onClose])

  return (
    <CommandDialog open={open} onOpenChange={setOpen}>
        <CommandInput
          placeholder="Type a command or search..."
          value={query}
          onValueChange={setQuery}
        />
        <CommandList>
          <CommandEmpty>No results found.</CommandEmpty>

          {searchResults.length > 0 && (
            <>
              <CommandGroup heading="Markets">
                {searchResults.map((result) => (
                  <CommandItem
                    key={result.symbol}
                    value={result.symbol}
                    onSelect={() => selectSymbol(result.symbol)}
                  >
                    <TrendingUp className="mr-2 h-4 w-4 text-cyan-400" />
                    <span className="font-mono font-semibold">{result.symbol}</span>
                    <span className="ml-2 text-xs text-zinc-500">
                      {result.base} / USDT — Binance
                    </span>
                  </CommandItem>
                ))}
              </CommandGroup>
              <CommandSeparator />
            </>
          )}

          <CommandGroup heading="Workspaces">
            <CommandItem onSelect={() => runCommand(() => router.push("/"))}>
              <Terminal className="mr-2 h-4 w-4" />
              <span>Trading Terminal</span>
            </CommandItem>
            <CommandItem onSelect={() => runCommand(() => router.push("/operator"))}>
              <Activity className="mr-2 h-4 w-4" />
              <span>Operator Dashboard</span>
            </CommandItem>
          </CommandGroup>
          <CommandSeparator />
          <CommandGroup heading="Actions">
            <CommandItem onSelect={() => runCommand(() => console.log("Run scanner"))}>
              <Search className="mr-2 h-4 w-4" />
              <span>Run Vectorized Scanner</span>
            </CommandItem>
            <CommandItem onSelect={() => runCommand(() => console.log("Open tearsheet"))}>
              <FlaskConical className="mr-2 h-4 w-4" />
              <span>Open Research Tearsheet</span>
            </CommandItem>
          </CommandGroup>
          <CommandSeparator />
          <CommandGroup heading="Danger Zone">
            <CommandItem
              onSelect={() => runCommand(() => console.log("Kill switch"))}
              className="text-red-500 data-[selected=true]:bg-red-500/10 data-[selected=true]:text-red-500"
            >
              <AlertOctagon className="mr-2 h-4 w-4" />
              <span>Trigger Emergency Stop</span>
              <CommandShortcut>⌘K</CommandShortcut>
            </CommandItem>
          </CommandGroup>
        </CommandList>
    </CommandDialog>
  )
}
