"use client";

import { useId, useState, type ReactNode } from "react";
import { toast } from "sonner";
import { coinOf, errorCopy, isTotpError, MARKET_LABEL, MODE_LABEL } from "@/lib/autotrade/copy";
import { parseCoins } from "@/lib/autotrade/presets";
import type { ApiError, ExecutionMode, Market, Profile } from "@/lib/autotrade/types";
import { useAutoTradeStore } from "@/lib/stores/useAutoTradeStore";
import { isStaleBuild, STALE_BUILD_TITLE, useStaleBuild } from "@/lib/stores/useBuildStore";
import { btnPrimary, btnSecondary, ConfirmDialog, TotpDialog } from "../Dialogs";

type Kind = "text" | "number" | "select" | "bool" | "markets";
interface Row {
  key: string;
  label: string;
  kind: Kind;
  value: (p: Profile) => string;
  display?: (p: Profile) => string;
  options?: { value: string; label: string }[];
  /** Emptying it switches the setting off (sent in clear_limits) */
  clearable?: boolean;
  help?: string;
}

const NOTIFY = [
  { value: "ALL", label: "All" },
  { value: "IMPORTANT", label: "Important only" },
  { value: "NONE", label: "None" },
];
const s = (v: unknown) => (v == null ? "" : String(v));
const off = (v: unknown, unit = "") => (v == null || v === "" ? "Off" : `${v}${unit}`);

const ROWS: Row[] = [
  { key: "name", label: "Name", kind: "text", value: (p) => p.name },
  { key: "markets", label: "Coins and markets", kind: "markets", value: () => "", display: (p) => `${Array.from(new Set(p.instruments.map(coinOf))).join(", ")} · ${p.markets.map((m) => MARKET_LABEL[m]).join(", ")}` },
  { key: "capital_budget_usd", label: "Capital budget (USD)", kind: "number", value: (p) => s(p.capital_budget_usd) },
  { key: "style", label: "Style", kind: "select", value: (p) => p.style, options: [{ value: "SWING", label: "Swing (1h/4h)" }, { value: "INTRADAY", label: "Intraday (15m/1h)" }] },
  { key: "direction", label: "Direction", kind: "select", value: (p) => p.direction, options: [{ value: "LONG", label: "Long" }, { value: "SHORT", label: "Short" }, { value: "BOTH", label: "Long and short" }] },
  { key: "execution_mode", label: "Mode", kind: "select", value: (p) => p.execution_mode, display: (p) => MODE_LABEL[p.execution_mode],
    options: (Object.keys(MODE_LABEL) as ExecutionMode[]).map((m) => ({ value: m, label: MODE_LABEL[m] })) },
  { key: "veto_window_s", label: "Veto window (seconds)", kind: "number", value: (p) => s(p.veto_window_s) },
  { key: "dry_run", label: "Dry run", kind: "bool", value: (p) => (p.dry_run ? "true" : "false"), display: (p) => (p.dry_run ? "On: orders are simulated" : "Off") },
  { key: "max_risk_per_trade_pct", label: "Risk per trade (%)", kind: "number", value: (p) => s(p.max_risk_per_trade_pct) },
  { key: "daily_loss_limit_pct", label: "Daily loss limit (%)", kind: "number", value: (p) => s(p.daily_loss_limit_pct), display: (p) => off(p.daily_loss_limit_pct, "%"), clearable: true },
  { key: "max_drawdown_pct", label: "Maximum drawdown (%)", kind: "number", value: (p) => s(p.max_drawdown_pct) },
  { key: "max_position_pct", label: "Maximum position (% of capital)", kind: "number", value: (p) => s(p.max_position_pct) },
  { key: "max_leverage", label: "Maximum leverage", kind: "number", value: (p) => s(p.max_leverage), display: (p) => `${p.max_leverage}×` },
  { key: "max_open_positions", label: "Maximum open positions", kind: "number", value: (p) => s(p.max_open_positions) },
  { key: "max_debates_per_day", label: "Maximum debates per day", kind: "number", value: (p) => s(p.max_debates_per_day) },
  { key: "exit_tp_r", label: "Take profit at (R)", kind: "number", value: (p) => s(p.exit_tp_r) },
  { key: "exit_tp1_scale_pct", label: "Take profit 1 scale-out (%)", kind: "number", value: (p) => s(p.exit_tp1_scale_pct) },
  { key: "exit_trail_activate_pct", label: "Activate trailing exit at (%)", kind: "number", value: (p) => s(p.exit_trail_activate_pct), display: (p) => off(p.exit_trail_activate_pct, "%"), clearable: true },
  { key: "exit_trail_pct", label: "Trailing distance (%)", kind: "number", value: (p) => s(p.exit_trail_pct), display: (p) => off(p.exit_trail_pct, "%"), clearable: true },
  { key: "exit_max_hold_hours", label: "Maximum holding time (hours)", kind: "number", value: (p) => s(p.exit_max_hold_hours), display: (p) => off(p.exit_max_hold_hours, " h"), clearable: true },
  { key: "notify_level", label: "Notifications", kind: "select", value: (p) => p.notify_level, options: NOTIFY },
  { key: "breaker_max_loss_streak", label: "Losses before breaker", kind: "number", value: (p) => s(p.breaker_max_loss_streak) },
  { key: "breaker_max_reject_streak", label: "Rejected entries before breaker", kind: "number", value: (p) => s(p.breaker_max_reject_streak) },
];

const INT_KEYS = new Set(["max_leverage", "max_open_positions", "max_debates_per_day", "exit_max_hold_hours", "veto_window_s", "breaker_max_loss_streak", "breaker_max_reject_streak"]);

/**
 * The only profile editor (it replaces Mission Control's Mandate editor). One row is edited at a time and saved through
 * PATCH /ai/mandates/{id} (the mode through the existing mode route). A change is first sent without a code: the API
 * decides whether it adds risk, and when it answers TOTP_REQUIRED the code prompt opens and the same change is resent.
 */
export function SettingsTab({ profile: p }: { profile: Profile }) {
  const store = useAutoTradeStore();
  const staleBuild = useStaleBuild();
  const [editing, setEditing] = useState<string | null>(null);
  const [value, setValue] = useState("");
  const [markets, setMarkets] = useState<Market[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [totpFor, setTotpFor] = useState<null | { label: string; send: (code?: string) => Promise<unknown> }>(null);
  const [totpError, setTotpError] = useState<string | null>(null);
  const [discard, setDiscard] = useState<string | null>(null);
  const uid = useId();

  const begin = (row: Row, force = false) => {
    const go = () => {
      setEditing(row.key);
      setError(null);
      if (row.kind === "markets") {
        setValue(Array.from(new Set(p.instruments.map(coinOf))).join(", "));
        setMarkets(p.markets);
      } else setValue(row.value(p));
    };
    if (!force && editing && editing !== row.key && dirty()) setDiscard(row.key);
    else go();
  };
  const dirty = () => {
    const row = ROWS.find((r) => r.key === editing);
    if (!row) return false;
    if (row.kind === "markets") return value !== Array.from(new Set(p.instruments.map(coinOf))).join(", ") || markets.join() !== p.markets.join();
    return value !== row.value(p);
  };

  const patchFor = (row: Row): Record<string, unknown> => {
    if (row.kind === "markets") {
      const suffix = { SPOT: "", SPOT_MARGIN: "-MARGIN", PERP: "-PERP" } as const;
      const coins = parseCoins(value);
      return { instruments: coins.flatMap((c) => markets.map((m) => `${c}${suffix[m]}.BINANCE`)), markets, allow_perps: markets.includes("PERP") };
    }
    if (row.kind === "bool") return { [row.key]: value === "true" };
    if (row.clearable && value.trim() === "") return row.value(p) === "" ? {} : { clear_limits: [row.key] };
    if (row.kind === "number") return { [row.key]: INT_KEYS.has(row.key) ? Number(value) : value.trim() };
    return { [row.key]: value };
  };

  const save = async (row: Row) => {
    if (isStaleBuild()) return;
    const send =
      row.key === "execution_mode"
        ? (code?: string) => store.setMode(p.id, value as ExecutionMode, code)
        : (code?: string) => store.patchProfile(p.id, patchFor(row), code);
    setPending(true);
    setError(null);
    try {
      await send();
      toast.success("Changes saved.");
      setEditing(null);
    } catch (e) {
      const err = e as ApiError;
      if (isTotpError(err)) setTotpFor({ label: `Changing ${row.label.toLowerCase()}`, send });
      else setError(err.fields ? Object.values(err.fields).join(" ") : `Changes weren't saved. ${errorCopy(err, value as ExecutionMode)}`);
    } finally {
      setPending(false);
    }
  };

  const field = (row: Row): ReactNode => {
    const id = `${uid}-${row.key}`;
    const cls = "w-full rounded-md border border-zinc-700 bg-zinc-900 px-2 py-1.5 text-sm text-zinc-100 sm:w-64";
    if (row.kind === "markets")
      return (
        <div className="space-y-2">
          <label htmlFor={id} className="sr-only">Coins</label>
          <input id={id} className={cls} value={value} onChange={(e) => setValue(e.target.value)} />
          <fieldset className="flex flex-wrap gap-3">
            <legend className="sr-only">Markets</legend>
            {(["SPOT", "SPOT_MARGIN", "PERP"] as Market[]).map((m) => (
              <label key={m} className="inline-flex items-center gap-1.5 text-xs text-zinc-200">
                <input type="checkbox" checked={markets.includes(m)} onChange={(e) => setMarkets(e.target.checked ? [...markets, m] : markets.filter((x) => x !== m))} />
                {MARKET_LABEL[m]}
              </label>
            ))}
          </fieldset>
        </div>
      );
    if (row.kind === "select" || row.kind === "bool") {
      const opts = row.kind === "bool" ? [{ value: "true", label: "On" }, { value: "false", label: "Off" }] : row.options ?? [];
      return (
        <>
          <label htmlFor={id} className="sr-only">{row.label}</label>
          <select id={id} className={cls} value={value} onChange={(e) => setValue(e.target.value)}>
            {opts.map((o) => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>
        </>
      );
    }
    return (
      <>
        <label htmlFor={id} className="sr-only">{row.label}</label>
        <input id={id} className={cls} type={row.kind === "number" ? "number" : "text"} step="any" value={value}
          placeholder={row.clearable ? "empty: off" : undefined} onChange={(e) => setValue(e.target.value)} />
      </>
    );
  };

  return (
    <div className="space-y-3">
      <p className="text-xs text-zinc-400">
        Lowering risk saves directly. A change that gives the AI more room asks for your TOTP code. Environment: Paper (read-only).
      </p>
      <ul className="divide-y divide-zinc-800 rounded-xl border border-zinc-800" aria-label="Profile settings">
        {ROWS.map((row) => (
          <li key={row.key} className="flex flex-col gap-2 px-3 py-2.5 sm:flex-row sm:items-center">
            <span className="text-xs font-semibold text-zinc-300 sm:w-64">{row.label}</span>
            {editing === row.key ? (
              <form
                className="flex flex-1 flex-col gap-2 sm:flex-row sm:items-start"
                onSubmit={(e) => {
                  e.preventDefault();
                  void save(row);
                }}
                onKeyDown={(e) => {
                  if (e.key === "Escape") setEditing(null);
                }}
              >
                <div className="flex-1">{field(row)}</div>
                <div className="flex gap-2">
                  <button type="submit" className={btnPrimary} disabled={pending || staleBuild} title={staleBuild ? STALE_BUILD_TITLE : undefined}>
                    {pending ? "Saving…" : "Save"}
                  </button>
                  <button type="button" className={btnSecondary} onClick={() => setEditing(null)} disabled={pending}>
                    Cancel
                  </button>
                </div>
              </form>
            ) : (
              <>
                <span className="flex-1 text-sm text-zinc-100">{row.display ? row.display(p) : row.value(p) || "—"}</span>
                <button type="button" className={btnSecondary} onClick={() => begin(row)} aria-label={`Edit ${row.label}`}>
                  Edit
                </button>
              </>
            )}
            {editing === row.key && error && (
              <p role="alert" className="text-xs text-red-300 sm:basis-full">
                {error}
              </p>
            )}
          </li>
        ))}
      </ul>
      <TotpDialog
        open={totpFor !== null}
        change={totpFor?.label ?? "This change"}
        confirmLabel="Save changes"
        pending={pending}
        error={totpError}
        onClose={() => {
          setTotpFor(null);
          setTotpError(null);
        }}
        onSubmit={async (code) => {
          if (!totpFor) return;
          setPending(true);
          setTotpError(null);
          try {
            await totpFor.send(code);
            toast.success("Changes saved.");
            setTotpFor(null);
            setEditing(null);
          } catch (e) {
            const err = e as ApiError;
            if (isTotpError(err)) setTotpError(errorCopy(err));
            else {
              setTotpFor(null);
              setError(`Changes weren't saved. ${errorCopy(err)}`);
            }
          } finally {
            setPending(false);
          }
        }}
      />
      <ConfirmDialog
        open={discard !== null}
        title="Discard changes?"
        body="Your edit to this setting will be lost."
        confirmLabel="Discard changes"
        onClose={() => setDiscard(null)}
        onConfirm={() => {
          const row = ROWS.find((r) => r.key === discard);
          setDiscard(null);
          setEditing(null);
          if (row) begin(row, true);
        }}
      />
    </div>
  );
}
