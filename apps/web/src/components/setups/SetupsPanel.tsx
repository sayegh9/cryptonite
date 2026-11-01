"use client";

import { useCallback, useEffect, useMemo } from "react";
import { applyFilters, useSetupsStore, type EligibilityFilter, type Setup } from "@/lib/stores/useSetupsStore";
import { useVisiblePoll } from "@/components/autotrade/AutoTradeWorkspace";
import { SetupDrawer } from "./SetupDrawer";
import { formatAge, formatPrice, reasonText, symbolOf } from "./format";

const POLL_MS = 30_000;
const selectCls = "h-8 rounded-lg border border-border bg-background px-2 text-sm";

function Filter({ label, value, onChange, children }: { label: string; value: string; onChange: (v: string) => void; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-0.5 text-[11px] text-muted-foreground">
      {label}
      <select aria-label={label} value={value} onChange={(e) => onChange(e.target.value)} className={selectCls}>
        {children}
      </select>
    </label>
  );
}

export function SetupsPanel() {
  const { filters, windowHours, selectedId, setups, status, error } = useSetupsStore();
  const { setFilter, resetFilters, setWindowHours, select, refresh } = useSetupsStore.getState();
  const tick = useCallback(() => void useSetupsStore.getState().refresh(), []);
  useVisiblePoll(tick, POLL_MS);
  useEffect(() => {
    void refresh();
  }, [windowHours, refresh]);

  const rows = useMemo(() => applyFilters(setups, filters), [setups, filters]);
  const profiles = useMemo(() => {
    const m = new Map<string, string>();
    for (const s of setups) if (s.profile) m.set(s.profile.id, s.profile.name);
    return [...m];
  }, [setups]);
  const presets = useMemo(() => [...new Set(setups.map((s) => s.provenance.preset).filter((p): p is string => !!p))].sort(), [setups]);
  const selected: Setup | undefined = setups.find((s) => s.id === selectedId);

  return (
    <div className="relative flex h-full min-h-0 flex-col gap-3 p-4" data-testid="setups-panel">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold">Setups</h2>
          <p className="text-xs text-muted-foreground">Fresh scanner candidates, who owns them and how far each got. Read-only.</p>
        </div>
        <div className="flex flex-wrap items-end gap-2">
          <Filter label="Window" value={String(windowHours)} onChange={(v) => setWindowHours(Number(v))}>
            {[1, 6, 24, 72].map((h) => <option key={h} value={h}>{h === 1 ? "Last hour" : `Last ${h} h`}</option>)}
          </Filter>
          <Filter label="Profile" value={filters.profile} onChange={(v) => setFilter("profile", v)}>
            <option value="all">All profiles</option>
            <option value="none">No owner</option>
            {profiles.map(([id, name]) => <option key={id} value={id}>{name}</option>)}
          </Filter>
          <Filter label="Preset" value={filters.preset} onChange={(v) => setFilter("preset", v)}>
            <option value="all">All presets</option>
            {presets.map((p) => <option key={p} value={p}>{p}</option>)}
          </Filter>
          <Filter label="Direction" value={filters.direction} onChange={(v) => setFilter("direction", v as "all" | "LONG" | "SHORT")}>
            <option value="all">Both</option>
            <option value="LONG">Long</option>
            <option value="SHORT">Short</option>
          </Filter>
          <Filter label="Eligibility" value={filters.eligibility} onChange={(v) => setFilter("eligibility", v as EligibilityFilter)}>
            <option value="all">All</option>
            <option value="eligible">Eligible</option>
            <option value="ineligible">Not eligible</option>
          </Filter>
          <button type="button" onClick={resetFilters} className="h-8 rounded-lg border border-border px-2 text-sm hover:bg-muted">
            Clear filters
          </button>
        </div>
      </header>

      {status === "error" && (
        <div role="alert" className="flex items-center justify-between rounded border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm">
          <span>The setups could not be loaded{setups.length ? "; showing the last good list" : ""}: {error}</span>
          <button type="button" onClick={() => void refresh()} className="underline">Retry</button>
        </div>
      )}

      <div className="min-h-0 flex-1 overflow-auto rounded border border-border">
        <table className="w-full text-sm" aria-label="Setups">
          <thead className="sticky top-0 bg-muted/60 text-left text-[11px] uppercase tracking-wider text-muted-foreground">
            <tr>
              {["Coin", "Side", "Preset", "TF", "Entry", "Stop", "Target", "Cost R", "Owner", "Stage", "Age"].map((h) => (
                <th key={h} scope="col" className="px-2 py-1.5 font-medium">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((s) => (
              <tr
                key={s.id}
                data-testid="setup-row"
                tabIndex={0}
                aria-selected={s.id === selectedId}
                onClick={() => select(s.id)}
                onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && select(s.id)}
                className="cursor-pointer border-t border-border hover:bg-muted/40 focus-visible:bg-muted/40"
              >
                <td className="px-2 py-1.5 font-medium">{symbolOf(s.instrument_id)}</td>
                <td className="px-2 py-1.5">{s.direction ?? "—"}</td>
                <td className="px-2 py-1.5">{s.provenance.preset ?? "—"}</td>
                <td className="px-2 py-1.5">{s.provenance.timeframe}</td>
                <td className="px-2 py-1.5 font-mono">{formatPrice(s.entry)}</td>
                <td className="px-2 py-1.5 font-mono">{formatPrice(s.stop)}</td>
                <td className="px-2 py-1.5 font-mono">{formatPrice(s.target)}</td>
                <td className="px-2 py-1.5 font-mono">{s.cost.cost_r === null ? "—" : s.cost.cost_r.toFixed(3)}</td>
                <td className="px-2 py-1.5" title={reasonText(s.eligibility.reason)}>
                  {s.profile ? s.profile.name : "none"}
                  <span className="ml-1 text-xs text-muted-foreground">{s.eligibility.eligible ? "eligible" : "not eligible"}</span>
                </td>
                <td className="px-2 py-1.5">
                  {s.funnel.furthest_stage ? `${s.funnel.furthest_stage} ${s.funnel.outcome ?? ""}` : "—"}
                </td>
                <td className="px-2 py-1.5">{formatAge(s.age_seconds)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {status === "loading" && rows.length === 0 && <p className="p-4 text-sm text-muted-foreground">Loading setups…</p>}
        {status === "ready" && rows.length === 0 && (
          <p className="p-4 text-sm text-muted-foreground">
            {setups.length === 0 ? "No candidates in this window." : "No setups match the filters."}
          </p>
        )}
      </div>

      {selected && <SetupDrawer setup={selected} onClose={() => select(null)} />}
    </div>
  );
}
