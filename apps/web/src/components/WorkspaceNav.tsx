"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { NAV_GROUPS, type WorkspaceMode } from "@/lib/navigation";

/**
 * The header's grouped workspace tabs. When they do not fit (the mode banner takes priority) the nav
 * scrolls horizontally; fades and chevron buttons show that more tabs are off screen, and the current
 * tab is scrolled into view on every navigation.
 */
export function WorkspaceNav({
  workspaceMode,
  urlView,
  navigate,
  className = "",
}: {
  workspaceMode: WorkspaceMode;
  urlView: string | null;
  navigate: (mode: WorkspaceMode, view?: string | null) => void;
  className?: string;
}) {
  const navRef = useRef<HTMLElement>(null);
  const [edges, setEdges] = useState({ left: false, right: false });

  const measure = useCallback(() => {
    const el = navRef.current;
    if (!el) return;
    const left = el.scrollLeft > 1;
    const right = el.scrollLeft + el.clientWidth < el.scrollWidth - 1;
    setEdges((e) => (e.left === left && e.right === right ? e : { left, right }));
  }, []);

  const revealCurrent = useCallback(() => {
    navRef.current?.querySelector<HTMLElement>('[aria-current="page"]')?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, []);

  // The nav narrows when the mode banner grows (data arrives): keep the current tab in view
  useEffect(() => {
    const el = navRef.current;
    if (!el) return;
    measure();
    const ro = new ResizeObserver(() => {
      revealCurrent();
      measure();
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [measure, revealCurrent]);

  useEffect(revealCurrent, [revealCurrent, workspaceMode, urlView]);

  const scrollBy = (dir: 1 | -1) => {
    const el = navRef.current;
    if (el) el.scrollBy({ left: dir * Math.max(120, el.clientWidth * 0.6), behavior: "smooth" });
  };

  return (
    <div className={`relative flex min-w-0 items-center ${className}`}>
      <nav
        ref={navRef}
        aria-label="Workspaces"
        onScroll={measure}
        className="flex items-center h-full min-w-0 font-mono text-xs font-semibold overflow-x-auto scroll-px-8 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
      >
        {NAV_GROUPS.map((group, gi) => (
          <div
            key={group.id}
            role="group"
            aria-label={group.label}
            className={`flex flex-none items-center h-full gap-4 ${gi > 0 ? "ml-4 pl-4 border-l border-zinc-800" : ""}`}
          >
            {/* Below 2xl the group borders carry the grouping; the labels' room goes to tabs */}
            <span className="hidden 2xl:inline text-2xs uppercase tracking-widest text-zinc-500 select-none" aria-hidden="true">{group.label}</span>
            {group.items.map((tab) => {
              const active = workspaceMode === tab.mode && (tab.view ? urlView === tab.view : !(tab.mode === "terminal" && urlView === "scanner"));
              return (
                <button
                  key={`${tab.mode}-${tab.view ?? ""}`}
                  type="button"
                  onClick={() => navigate(tab.mode, tab.view)}
                  aria-current={active ? "page" : undefined}
                  className={`h-full flex items-center border-b-2 px-1 whitespace-nowrap transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-500/60 ${
                    active
                      ? "border-cyan-500 text-cyan-400"
                      : "border-transparent text-zinc-400 hover:text-zinc-200 hover:border-zinc-700"
                  }`}
                >
                  {tab.label}
                </button>
              );
            })}
          </div>
        ))}
      </nav>
      {/* Mouse affordance only: keyboard users reach every tab with Tab, which scrolls it into view */}
      {edges.left && (
        <button
          type="button"
          tabIndex={-1}
          aria-hidden="true"
          data-testid="nav-scroll-left"
          onClick={() => scrollBy(-1)}
          className="absolute inset-y-0 left-0 flex w-8 items-center justify-start bg-gradient-to-r from-zinc-950 via-zinc-950/90 to-transparent text-zinc-400 hover:text-zinc-100"
        >
          <ChevronLeft className="h-4 w-4" />
        </button>
      )}
      {edges.right && (
        <button
          type="button"
          tabIndex={-1}
          aria-hidden="true"
          data-testid="nav-scroll-right"
          onClick={() => scrollBy(1)}
          className="absolute inset-y-0 right-0 flex w-8 items-center justify-end bg-gradient-to-l from-zinc-950 via-zinc-950/90 to-transparent text-zinc-400 hover:text-zinc-100"
        >
          <ChevronRight className="h-4 w-4" />
        </button>
      )}
    </div>
  );
}
