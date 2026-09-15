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
  FlaskConical
} from "lucide-react"

export function CommandPalette() {
  const [open, setOpen] = React.useState(false)
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

  const runCommand = React.useCallback((command: () => void) => {
    setOpen(false)
    command()
  }, [])

  return (
    <CommandDialog open={open} onOpenChange={setOpen}>
        <CommandInput placeholder="Type a command or search..." />
        <CommandList>
          <CommandEmpty>No results found.</CommandEmpty>
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
