"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ChevronLeft, ChevronRight, MoreHorizontal } from "lucide-react";
import { NAV_GROUPS, type NavItem, type WorkspaceMode } from "@/lib/navigation";

// Written out in full so the class scanner sees each utility as a literal.
// Below 1100px the secondary destinations (overflow items) move into the "More" menu, so the primary tabs fit a
// narrow pane. Above it every tab is inline, as before.
const SHOW_ON_WIDE = "hidden min-[1100px]:flex";
const HIDE_ON_WIDE = "min-[1100px]:hidden";

const OVERFLOW_ITEMS: NavItem[] = NAV_GROUPS.flatMap((g) => g.items.filter((i) => i.overflow));

const isActiveTab = (tab: NavItem, workspaceMode: WorkspaceMode, urlView: string | null) =>
  workspaceMode === tab.mode && (tab.view ? urlView === tab.view : !(tab.mode === "terminal" && urlView === "scanner"));

/**
 * The header's grouped workspace tabs. When they do not fit (the mode banner takes priority) the nav
 * scrolls horizontally; fades and chevron buttons show that more tabs are off screen, and the current
 * tab is scrolled into view on every navigation. Narrow panes get the overflow items under "More" instead.
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
    <div className={`flex min-w-0 items-center ${className}`}>
      <div className="relative flex min-w-0 flex-1 items-center h-full">
        <nav
          ref={navRef}
          aria-label="Workspaces"
          onScroll={measure}
          className="flex items-center h-full min-w-0 font-mono text-xs font-semibold overflow-x-auto scroll-px-8 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
        >
          {NAV_GROUPS.map((group, gi) => {
            const allOverflow = group.items.every((i) => i.overflow);
            return (
              <div
                key={group.id}
                role="group"
                aria-label={group.label}
                className={`${allOverflow ? SHOW_ON_WIDE : "flex"} flex-none items-center h-full gap-4 ${gi > 0 ? "ml-4 pl-4 border-l border-zinc-800" : ""}`}
              >
                {/* Below 2xl the group borders carry the grouping; the labels' room goes to tabs */}
                <span className="hidden 2xl:inline text-2xs uppercase tracking-widest text-zinc-500 select-none" aria-hidden="true">{group.label}</span>
                {group.items.map((tab) => {
                  const active = isActiveTab(tab, workspaceMode, urlView);
                  return (
                    <button
                      key={`${tab.mode}-${tab.view ?? ""}`}
                      type="button"
                      onClick={() => navigate(tab.mode, tab.view)}
                      aria-current={active ? "page" : undefined}
                      className={`h-full flex items-center border-b-2 px-1 whitespace-nowrap transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-500/60 ${
                        tab.overflow ? SHOW_ON_WIDE : "flex"
                      } ${
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
            );
          })}
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
      {/* Beside the strip, not in it: it stays visible, and as an icon it costs little room for the current tab */}
      <OverflowMenu workspaceMode={workspaceMode} urlView={urlView} navigate={navigate} />
    </div>
  );
}

/**
 * The secondary destinations, below 1100px. The button sits in the scrolling strip; the menu is portalled to the
 * body so the strip's overflow cannot clip it. It closes on outside press, Escape, and any scroll (it is fixed to
 * the button's position when it opens).
 */
function OverflowMenu({
  workspaceMode,
  urlView,
  navigate,
}: {
  workspaceMode: WorkspaceMode;
  urlView: string | null;
  navigate: (mode: WorkspaceMode, view?: string | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const [anchor, setAnchor] = useState<{ top: number; right: number } | null>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const anyActive = OVERFLOW_ITEMS.some((tab) => isActiveTab(tab, workspaceMode, urlView));

  const toggle = () => {
    if (!open && buttonRef.current) {
      const r = buttonRef.current.getBoundingClientRect();
      setAnchor({ top: r.bottom + 4, right: Math.max(0, window.innerWidth - r.right) });
    }
    setOpen((o) => !o);
  };

  useEffect(() => {
    if (!open) return;
    const close = () => setOpen(false);
    const onPointer = (e: MouseEvent) => {
      const target = e.target as Node;
      if (!buttonRef.current?.contains(target) && !menuRef.current?.contains(target)) close();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    document.addEventListener("mousedown", onPointer);
    document.addEventListener("keydown", onKey);
    document.addEventListener("scroll", close, true);
    window.addEventListener("resize", close);
    return () => {
      document.removeEventListener("mousedown", onPointer);
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("scroll", close, true);
      window.removeEventListener("resize", close);
    };
  }, [open]);

  return (
    <div className={`flex h-full flex-none items-center ${HIDE_ON_WIDE}`}>
      <button
        ref={buttonRef}
        type="button"
        aria-label="More workspaces"
        aria-haspopup="menu"
        aria-expanded={open}
        title="More workspaces"
        onClick={toggle}
        className={`flex h-full items-center border-b-2 px-2 transition-colors ${
          anyActive ? "border-cyan-500 text-cyan-400" : "border-transparent text-zinc-400 hover:text-zinc-200"
        }`}
      >
        <MoreHorizontal className="h-4 w-4" aria-hidden="true" />
      </button>
      {open &&
        anchor &&
        createPortal(
          <div
            ref={menuRef}
            role="menu"
            aria-label="More workspaces"
            style={{ position: "fixed", top: anchor.top, right: anchor.right }}
            className="z-50 min-w-40 rounded border border-white/10 bg-zinc-950 py-1 shadow-xl"
          >
            {OVERFLOW_ITEMS.map((tab) => {
              const active = isActiveTab(tab, workspaceMode, urlView);
              return (
                <button
                  key={`${tab.mode}-${tab.view ?? ""}`}
                  type="button"
                  role="menuitem"
                  aria-current={active ? "page" : undefined}
                  onClick={() => {
                    navigate(tab.mode, tab.view);
                    setOpen(false);
                  }}
                  className={`block w-full px-3 py-2 text-left font-mono text-xs ${
                    active ? "text-cyan-400" : "text-zinc-300 hover:bg-zinc-900"
                  }`}
                >
                  {tab.label}
                </button>
              );
            })}
          </div>,
          document.body,
        )}
    </div>
  );
}
