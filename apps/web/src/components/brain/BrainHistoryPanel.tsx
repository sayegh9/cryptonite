"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Archive, ArchiveRestore, Check, History, Loader2, Pencil, Plus, RefreshCcw, Search, Trash2, X } from "lucide-react";
import { useBrainStore } from "@/lib/stores/useBrainStore";
import { useSessionStore } from "@/lib/stores/useSessionStore";
import {
  BrainHistoryError, deleteArchivedBrainSessions, deleteBrainSession, listBrainSessions, openBrainSession, updateBrainSession,
} from "@/lib/brain/client";
import type { BrainSessionSummary } from "@/lib/brain/types";

const PAGE_SIZE = 30;
const SEARCH_DELAY_MS = 250;
const ALL_ARCHIVED = "all-archived"; // the confirm target of "Delete all archived"

type Tab = "recent" | "archived";
type LoadState = "loading" | "ready" | "error" | "signed-out";

/** Local calendar day, so "Today" means the operator's today */
function dayKey(d: Date): string {
  return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
}

/** The heading a conversation sits under: Today, Yesterday, a weekday this week, else the month */
export function dayGroup(iso: string, now: Date = new Date()): string {
  const d = new Date(iso);
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const days = Math.round((today.getTime() - new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime()) / 86_400_000);
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days < 7) return d.toLocaleDateString(undefined, { weekday: "long" });
  return d.toLocaleDateString(undefined, { month: "long", year: "numeric" });
}

/** Time of day for today's conversations, the date for older ones */
function stamp(iso: string, now: Date = new Date()): string {
  const d = new Date(iso);
  return dayKey(d) === dayKey(now)
    ? d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

function groupByDay(items: BrainSessionSummary[]): { label: string; items: BrainSessionSummary[] }[] {
  const groups: { label: string; items: BrainSessionSummary[] }[] = [];
  for (const s of items) {
    const label = dayGroup(s.updated_at);
    const last = groups[groups.length - 1];
    if (last && last.label === label) last.items.push(s);
    else groups.push({ label, items: [s] });
  }
  return groups;
}

/**
 * The Brain's conversation history: every conversation is kept on the server, so any of them can be opened
 * and continued. Search covers titles and what was said. Archiving hides a conversation from Recent; a new
 * message in it brings it back.
 */
export function BrainHistoryPanel({ onClose, onOpened }: { onClose?: () => void; onOpened?: () => void }) {
  const sessionId = useBrainStore((s) => s.sessionId);
  const streaming = useBrainStore((s) => s.streaming);
  const newConversation = useBrainStore((s) => s.newConversation);
  const session = useSessionStore((s) => s.status);

  const [tab, setTab] = useState<Tab>("recent");
  const [query, setQuery] = useState("");
  const [appliedQuery, setAppliedQuery] = useState("");
  const [items, setItems] = useState<BrainSessionSummary[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [state, setState] = useState<LoadState>("loading");
  const [loadingMore, setLoadingMore] = useState(false);
  const [editing, setEditing] = useState<{ id: string; title: string } | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null); // a session id, or ALL_ARCHIVED
  const request = useRef<AbortController | null>(null);

  // Search waits for a pause in typing
  useEffect(() => {
    const t = setTimeout(() => setAppliedQuery(query.trim()), SEARCH_DELAY_MS);
    return () => clearTimeout(t);
  }, [query]);

  const load = useCallback(async () => {
    request.current?.abort();
    const ctrl = new AbortController();
    request.current = ctrl;
    if (session === "anonymous") {
      setState("signed-out");
      setItems([]);
      return;
    }
    try {
      const page = await listBrainSessions({
        query: appliedQuery, archived: tab === "archived", limit: PAGE_SIZE, signal: ctrl.signal,
      });
      if (ctrl.signal.aborted) return;
      setItems(page.sessions);
      setCursor(page.next_cursor);
      setState("ready");
    } catch (e) {
      if (ctrl.signal.aborted) return;
      if (e instanceof BrainHistoryError && (e.status === 401 || e.status === 403)) {
        void useSessionStore.getState().check();
        setState("signed-out");
      } else {
        setState("error");
      }
    }
  }, [appliedQuery, tab, session]);

  useEffect(() => {
    setState((s) => (s === "ready" ? s : "loading"));
    void load();
    return () => request.current?.abort();
  }, [load]);

  // A finished turn changes the order (and a first message creates a conversation): refresh then
  const wasStreaming = useRef(streaming);
  useEffect(() => {
    if (wasStreaming.current && !streaming) void load();
    wasStreaming.current = streaming;
  }, [streaming, load]);

  const loadMore = async () => {
    if (!cursor || loadingMore) return;
    setLoadingMore(true);
    try {
      const page = await listBrainSessions({ query: appliedQuery, archived: tab === "archived", limit: PAGE_SIZE, before: cursor });
      setItems((prev) => [...prev, ...page.sessions.filter((s) => !prev.some((p) => p.id === s.id))]);
      setCursor(page.next_cursor);
    } catch {
      setNotice("Older conversations did not load. Try again.");
    } finally {
      setLoadingMore(false);
    }
  };

  const open = async (s: BrainSessionSummary) => {
    if (streaming || s.id === sessionId) {
      onOpened?.();
      return;
    }
    setBusyId(s.id);
    const ok = await openBrainSession(s.id);
    setBusyId(null);
    if (!ok) setNotice(`"${s.title || "Untitled"}" could not be opened. Try again.`);
    else onOpened?.();
  };

  const saveTitle = async () => {
    if (!editing) return;
    const title = editing.title.replace(/\s+/g, " ").trim();
    const current = items.find((s) => s.id === editing.id);
    if (!title || title === current?.title) {
      setEditing(null);
      return;
    }
    setBusyId(editing.id);
    try {
      const updated = await updateBrainSession(editing.id, { title });
      setItems((prev) => prev.map((s) => (s.id === updated.id ? { ...s, title: updated.title } : s)));
      setEditing(null);
    } catch {
      setNotice("The new name was not saved. Try again.");
    } finally {
      setBusyId(null);
    }
  };

  const setArchived = async (s: BrainSessionSummary, archived: boolean) => {
    setBusyId(s.id);
    try {
      await updateBrainSession(s.id, { archived });
      setItems((prev) => prev.filter((x) => x.id !== s.id));
      setNotice(archived ? `Archived "${s.title || "Untitled"}".` : `Restored "${s.title || "Untitled"}" to Recent.`);
    } catch {
      setNotice(archived ? "The conversation was not archived. Try again." : "The conversation was not restored. Try again.");
    } finally {
      setBusyId(null);
    }
  };

  // Deleting is permanent (O1): the messages go too. Deleting the open conversation starts a new one
  const remove = async (s: BrainSessionSummary) => {
    setConfirming(null);
    setBusyId(s.id);
    try {
      await deleteBrainSession(s.id);
      setItems((prev) => prev.filter((x) => x.id !== s.id));
      if (s.id === sessionId) newConversation();
      setNotice(`Deleted "${s.title || "Untitled"}".`);
    } catch {
      setNotice("The conversation was not deleted. Try again.");
    } finally {
      setBusyId(null);
    }
  };

  const removeArchived = async () => {
    setConfirming(null);
    setBusyId(ALL_ARCHIVED);
    try {
      const n = await deleteArchivedBrainSessions();
      if (sessionId && items.some((x) => x.id === sessionId)) newConversation();
      setItems([]);
      setCursor(null);
      setNotice(n === 1 ? "Deleted 1 archived conversation." : `Deleted ${n} archived conversations.`);
    } catch {
      setNotice("The archived conversations were not deleted. Try again.");
    } finally {
      setBusyId(null);
    }
  };

  const confirmLine = (onDelete: () => void, label: string) => (
    <div role="group" aria-label={label} className="flex items-center gap-2 px-3 py-2 text-2xs" data-testid="brain-history-confirm">
      <span className="flex-1 text-rose-300">Delete permanently? This can&apos;t be undone.</span>
      <button type="button" onClick={onDelete} className="rounded border border-rose-800 px-2 py-0.5 text-rose-200 hover:bg-rose-950/60">
        Delete
      </button>
      <button type="button" onClick={() => setConfirming(null)} className="rounded border border-zinc-700 px-2 py-0.5 text-zinc-300 hover:bg-zinc-800">
        Cancel
      </button>
    </div>
  );

  useEffect(() => {
    if (!notice) return;
    const t = setTimeout(() => setNotice(null), 5000);
    return () => clearTimeout(t);
  }, [notice]);

  const groups = useMemo(() => groupByDay(items), [items]);

  return (
    <aside aria-label="Conversation history" className="flex h-full min-h-0 flex-col bg-zinc-950" data-testid="brain-history">
      <div className="flex h-8 flex-none items-center justify-between border-b border-zinc-800 px-3">
        <h2 className="flex items-center gap-1.5 text-2xs font-bold uppercase tracking-wider text-zinc-400">
          <History className="h-3 w-3" aria-hidden="true" />
          Conversations
        </h2>
        <div className="flex items-center gap-1">
          <button
            type="button"
            disabled={streaming}
            onClick={() => {
              newConversation();
              onOpened?.();
            }}
            /* the header's New button is covered by the drawer on a phone; on wider screens it is right beside this */
            className="flex h-6 items-center gap-1 rounded px-1.5 text-2xs text-zinc-400 hover:bg-zinc-900 hover:text-zinc-100 disabled:opacity-40 md:hidden"
          >
            <Plus className="h-3 w-3" aria-hidden="true" /> New
          </button>
          {onClose && (
            <button
              type="button"
              onClick={onClose}
              aria-label="Close conversation history"
              className="flex h-6 w-6 items-center justify-center rounded text-zinc-500 hover:bg-zinc-900 hover:text-zinc-100"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          )}
        </div>
      </div>

      <div className="flex-none space-y-2 border-b border-zinc-800 p-3">
        <label className="flex items-center gap-2 rounded border border-zinc-800 bg-zinc-900/60 px-2 focus-within:border-cyan-700">
          <Search className="h-3.5 w-3.5 flex-none text-zinc-500" aria-hidden="true" />
          <span className="sr-only">Search conversations</span>
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search titles and messages"
            maxLength={200}
            className="h-7 w-full bg-transparent text-xs text-zinc-100 placeholder:text-zinc-600 focus:outline-none"
          />
        </label>
        <div role="tablist" aria-label="Which conversations" className="flex gap-1 text-2xs">
          {(["recent", "archived"] as const).map((t) => (
            <button
              key={t}
              type="button"
              role="tab"
              aria-selected={tab === t}
              onClick={() => setTab(t)}
              className={`h-6 rounded px-2 ${tab === t ? "bg-zinc-800 text-zinc-100" : "text-zinc-500 hover:text-zinc-200"}`}
            >
              {t === "recent" ? "Recent" : "Archived"}
            </button>
          ))}
        </div>
      </div>

      <p aria-live="polite" className="sr-only">{notice ?? ""}</p>
      {notice && (
        <p className="flex-none border-b border-zinc-800 px-3 py-1.5 text-2xs text-zinc-300" data-testid="brain-history-notice">
          {notice}
        </p>
      )}

      <div className="min-h-0 flex-1 overflow-y-auto">
        {state === "loading" && (
          <p className="flex items-center gap-2 p-3 text-2xs text-zinc-500">
            <Loader2 className="h-3 w-3 motion-safe:animate-spin" aria-hidden="true" /> Loading conversations…
          </p>
        )}
        {state === "signed-out" && <p className="p-3 text-2xs text-zinc-400">Sign in as operator to see your conversations.</p>}
        {state === "error" && (
          <div role="alert" className="space-y-2 p-3 text-2xs text-amber-200">
            <p>Conversations could not be loaded. Your history is kept on the server; nothing is lost.</p>
            <button
              type="button"
              onClick={() => void load()}
              className="flex items-center gap-1 rounded border border-amber-800/70 px-2 py-1 text-amber-100 hover:bg-amber-950/40"
            >
              <RefreshCcw className="h-3 w-3" aria-hidden="true" /> Retry
            </button>
          </div>
        )}
        {state === "ready" && items.length === 0 && (
          <p className="p-3 text-2xs text-zinc-500" data-testid="brain-history-empty">
            {appliedQuery
              ? `No conversation mentions "${appliedQuery}".`
              : tab === "archived"
                ? "Nothing archived. Archive a conversation to keep Recent short; it stays searchable here."
                : "No conversations yet. Ask the Brain something to start one."}
          </p>
        )}

        {state === "ready" && items.length > 0 && (
          <div className="pb-3">
            {groups.map((g) => (
              <section key={g.label} aria-label={g.label}>
                <h3 className="sticky top-0 z-10 bg-zinc-950/95 px-3 pb-1 pt-3 text-2xs text-zinc-500">{g.label}</h3>
                <ul>
                  {g.items.map((s) => {
                    const current = s.id === sessionId;
                    const busy = busyId === s.id;
                    const isEditing = editing?.id === s.id;
                    return (
                      <li
                        key={s.id}
                        className={`group relative border-l-2 ${current ? "border-cyan-400 bg-zinc-900/80" : "border-transparent hover:bg-zinc-900/50"}`}
                        data-testid="brain-history-row"
                      >
                        {confirming === s.id ? (
                          confirmLine(() => void remove(s), `Delete "${s.title || "Untitled"}"`)
                        ) : isEditing ? (
                          <form
                            className="flex items-center gap-1 px-3 py-2"
                            onSubmit={(e) => {
                              e.preventDefault();
                              void saveTitle();
                            }}
                          >
                            <label className="sr-only" htmlFor={`rename-${s.id}`}>Conversation name</label>
                            <input
                              id={`rename-${s.id}`}
                              autoFocus
                              value={editing.title}
                              maxLength={120}
                              onChange={(e) => setEditing({ id: s.id, title: e.target.value })}
                              onKeyDown={(e) => e.key === "Escape" && setEditing(null)}
                              className="h-7 min-w-0 flex-1 rounded border border-cyan-800 bg-zinc-900 px-2 text-sm text-zinc-100 focus:outline-none"
                            />
                            <button type="submit" disabled={busy} aria-label="Save name" className="flex h-7 w-7 items-center justify-center rounded text-cyan-300 hover:bg-zinc-800">
                              {busy ? <Loader2 className="h-3.5 w-3.5 motion-safe:animate-spin" /> : <Check className="h-3.5 w-3.5" />}
                            </button>
                            <button type="button" onClick={() => setEditing(null)} aria-label="Cancel rename" className="flex h-7 w-7 items-center justify-center rounded text-zinc-500 hover:bg-zinc-800">
                              <X className="h-3.5 w-3.5" />
                            </button>
                          </form>
                        ) : (
                          <>
                            <button
                              type="button"
                              onClick={() => void open(s)}
                              disabled={busy}
                              aria-current={current ? "true" : undefined}
                              title={streaming && !current ? "Wait for the current answer to finish" : undefined}
                              className="block w-full py-2 pl-3 pr-24 text-left disabled:opacity-60"
                            >
                              <span className="flex items-baseline justify-between gap-2">
                                <span className={`truncate text-sm ${current ? "text-zinc-50" : "text-zinc-200"}`}>{s.title || "Untitled"}</span>
                                <time dateTime={s.updated_at} className="flex-none font-mono text-2xs tabular-nums text-zinc-500">
                                  {stamp(s.updated_at)}
                                </time>
                              </span>
                              <span className="mt-0.5 flex items-baseline justify-between gap-2 text-2xs">
                                <span className="truncate text-zinc-500">
                                  {s.preview ? `${s.preview.role === "user" ? "You: " : ""}${s.preview.content}` : "No messages"}
                                </span>
                                {typeof s.message_count === "number" && (
                                  <span className="flex-none font-mono tabular-nums text-zinc-600" aria-label={`${s.message_count} messages`}>
                                    {s.message_count}
                                  </span>
                                )}
                              </span>
                            </button>
                            <div className="absolute right-2 top-1.5 flex gap-0.5 opacity-100 md:opacity-0 md:group-hover:opacity-100 md:group-focus-within:opacity-100">
                              <button
                                type="button"
                                disabled={busy}
                                onClick={() => setEditing({ id: s.id, title: s.title || "" })}
                                aria-label={`Rename "${s.title || "Untitled"}"`}
                                className="flex h-6 w-6 items-center justify-center rounded bg-zinc-950/90 text-zinc-500 hover:text-zinc-100"
                              >
                                <Pencil className="h-3 w-3" />
                              </button>
                              <button
                                type="button"
                                disabled={busy}
                                onClick={() => void setArchived(s, tab === "recent")}
                                aria-label={tab === "recent" ? `Archive "${s.title || "Untitled"}"` : `Restore "${s.title || "Untitled"}"`}
                                className="flex h-6 w-6 items-center justify-center rounded bg-zinc-950/90 text-zinc-500 hover:text-zinc-100"
                              >
                                {tab === "recent" ? <Archive className="h-3 w-3" /> : <ArchiveRestore className="h-3 w-3" />}
                              </button>
                              <button
                                type="button"
                                disabled={busy}
                                onClick={() => setConfirming(s.id)}
                                aria-label={`Delete "${s.title || "Untitled"}"`}
                                className="flex h-6 w-6 items-center justify-center rounded bg-zinc-950/90 text-zinc-500 hover:text-rose-300"
                              >
                                <Trash2 className="h-3 w-3" />
                              </button>
                            </div>
                          </>
                        )}
                      </li>
                    );
                  })}
                </ul>
              </section>
            ))}
            {cursor && (
              <div className="px-3 pt-2">
                <button
                  type="button"
                  onClick={() => void loadMore()}
                  disabled={loadingMore}
                  className="flex h-7 w-full items-center justify-center gap-1 rounded border border-zinc-800 text-2xs text-zinc-400 hover:text-zinc-100 disabled:opacity-60"
                >
                  {loadingMore && <Loader2 className="h-3 w-3 motion-safe:animate-spin" aria-hidden="true" />}
                  Show older conversations
                </button>
              </div>
            )}
            {tab === "archived" && (
              confirming === ALL_ARCHIVED ? (
                <div className="pt-2">{confirmLine(() => void removeArchived(), "Delete all archived conversations")}</div>
              ) : (
                <div className="px-3 pt-2">
                  <button
                    type="button"
                    disabled={busyId === ALL_ARCHIVED}
                    onClick={() => setConfirming(ALL_ARCHIVED)}
                    className="flex h-7 w-full items-center justify-center gap-1 rounded border border-rose-900/70 text-2xs text-rose-300 hover:bg-rose-950/40 disabled:opacity-60"
                  >
                    <Trash2 className="h-3 w-3" aria-hidden="true" /> Delete all archived
                  </button>
                </div>
              )
            )}
            {tab === "archived" && (
              <p className="px-3 pt-3 text-2xs text-zinc-600">Open an archived conversation to read it; sending a message moves it back to Recent.</p>
            )}
          </div>
        )}
      </div>
    </aside>
  );
}
