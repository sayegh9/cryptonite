"use client";

import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import { toast } from "sonner";
import { coinOf, errorCopy, isTotpError, MARKET_LABEL, MODE_LABEL } from "@/lib/autotrade/copy";
import { EMPTY_DRAFT, FIELD_PRESETS, parseCoins } from "@/lib/autotrade/presets";
import { getFeasibility } from "@/lib/autotrade/client";
import type { ApiError, Direction, Feasibility, ExecutionMode, Market, NotifyLevel, ProfileDraft, Style } from "@/lib/autotrade/types";
import { dispatchSwitchWorkspace } from "@/lib/navigation";
import { useAutoTradeStore } from "@/lib/stores/useAutoTradeStore";
import { isStaleBuild, STALE_BUILD_TITLE, useStaleBuild } from "@/lib/stores/useBuildStore";
import { btnPrimary, btnSecondary, ConfirmDialog, TotpDialog } from "../Dialogs";
import { FeasibilityNotes } from "../FeasibilityNotes";
import { effectivePresets, effectiveTimeframes } from "@/lib/autotrade/picks";
import { ReplayStats } from "./ReplayStats";
import { StrategyPicker } from "./StrategyPicker";

const STEPS = ["Markets and capital", "Style and direction", "Risk and leverage", "Mode and review"] as const;
const LOCK_HOLDING = new Set(["ACTIVE", "PAUSED", "HALTED", "STOPPING"]);
/** The mode change that follows a create: the draft is cleared by then, so the chosen mode travels with it */
type ModeStep = { id: string; name: string; mode: ExecutionMode };
type Errors = Partial<Record<keyof ProfileDraft | "coins", string>>;
const input = "w-full rounded-md border border-zinc-700 bg-zinc-900 px-2.5 py-2 text-sm text-zinc-100 aria-[invalid=true]:border-red-500";

function Field({ id, label, help, error, children }: { id: string; label: string; help?: ReactNode; error?: string; children: ReactNode }) {
  return (
    <div className="space-y-1">
      <label htmlFor={id} className="block text-xs font-semibold text-zinc-300">
        {label}
      </label>
      {children}
      {help && (
        <p id={`${id}-help`} className="text-2xs text-zinc-500">
          {help}
        </p>
      )}
      {error && (
        <p id={`${id}-error`} className="text-xs text-red-300">
          {error}
        </p>
      )}
    </div>
  );
}

function RadioCards<T extends string>({ legend, name, value, options, onChange }: {
  legend: string;
  name: string;
  value: T;
  options: { value: T; label: string; help?: string }[];
  onChange: (v: T) => void;
}) {
  return (
    <fieldset className="space-y-2">
      <legend className="text-xs font-semibold text-zinc-300">{legend}</legend>
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
        {options.map((o) => (
          <label key={o.value} className={`flex cursor-pointer gap-2 rounded-lg border p-3 text-sm ${value === o.value ? "border-cyan-500 bg-cyan-950/30" : "border-zinc-800"}`}>
            <input type="radio" name={name} value={o.value} checked={value === o.value} onChange={() => onChange(o.value)} className="mt-0.5" />
            <span>
              <span className="block font-semibold text-zinc-100">{o.label}</span>
              {o.help && <span className="block text-xs text-zinc-400">{o.help}</span>}
            </span>
          </label>
        ))}
      </div>
    </fieldset>
  );
}

/** Client-side checks that only mirror the schema's plain bounds; the API stays the authority and its errors are shown too. */
function validate(step: number, d: ProfileDraft): Errors {
  const e: Errors = {};
  const n = (s: string) => Number(s);
  if (step === 0) {
    if (!d.name.trim()) e.name = "Enter a profile name.";
    if (d.coins.length === 0) e.coins = "Choose at least one coin.";
    if (d.markets.length === 0) e.markets = "Choose at least one market.";
    if (!(n(d.capital_budget_usd) > 0)) e.capital_budget_usd = "Enter a capital budget greater than zero.";
  }
  if (step === 2) {
    if (!(n(d.max_risk_per_trade_pct) > 0)) e.max_risk_per_trade_pct = "Enter a risk per trade greater than zero.";
    if (!(n(d.max_drawdown_pct) > 0)) e.max_drawdown_pct = "Enter a maximum drawdown greater than zero.";
    if (!(n(d.max_position_pct) >= 1 && n(d.max_position_pct) <= 25)) e.max_position_pct = "Enter a position cap from 1 to 25%.";
    if (d.daily_loss_limit_pct.trim()) {
      if (n(d.daily_loss_limit_pct) < n(d.max_risk_per_trade_pct)) e.daily_loss_limit_pct = "Daily loss limit must be at least risk per trade.";
      else if (n(d.daily_loss_limit_pct) > n(d.max_drawdown_pct)) e.daily_loss_limit_pct = "Daily loss limit cannot exceed maximum drawdown.";
    }
    if (d.exit_trail_pct.trim() && d.exit_trail_activate_pct.trim() && n(d.exit_trail_pct) > n(d.exit_trail_activate_pct))
      e.exit_trail_pct = "Trail distance cannot exceed its activation threshold.";
    if (n(d.max_leverage) > 1 && !d.markets.some((m) => m !== "SPOT")) e.max_leverage = "Leverage above 1× needs Spot Margin or Perpetuals.";
  }
  if (step === 1 && d.direction !== "LONG" && !d.markets.some((m) => m !== "SPOT"))
    e.direction = "Short profiles need Spot Margin or Perpetuals. Add one of these markets or choose Long.";
  return e;
}

export function ProfileWizard({ onCancel, onCreated, onOpenProfile }: { onCancel: () => void; onCreated: (id: string) => void; onOpenProfile: (id: string) => void }) {
  const store = useAutoTradeStore();
  const draft = useAutoTradeStore((s) => s.wizardDraft) ?? EMPTY_DRAFT;
  const profiles = useAutoTradeStore((s) => s.profiles);
  const presets = useAutoTradeStore((s) => s.presets);
  const estimate = useAutoTradeStore((s) => s.estimate);
  const staleBuild = useStaleBuild();
  const [step, setStep] = useState(0);
  const [errors, setErrors] = useState<Errors>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [coinText, setCoinText] = useState(draft.coins.join(", "));
  const [discardOpen, setDiscardOpen] = useState(false);
  const [totp, setTotp] = useState<null | "create" | ModeStep>(null);
  const [totpError, setTotpError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const formRef = useRef<HTMLFormElement>(null);
  const uid = useId();
  const fid = (k: string) => `${uid}-${k}`;

  const set = (patch: Partial<ProfileDraft>) => store.setWizardDraft({ ...draft, ...patch });
  const touched = useMemo(() => JSON.stringify(draft) !== JSON.stringify(EMPTY_DRAFT), [draft]);

  useEffect(() => {
    void store.loadPresetStats();
    if (store.listUpdatedAt == null) void store.loadProfiles();
  }, []);
  // The estimate follows the coins, style and direction (debounced)
  const coinsKey = draft.coins.join(",");
  useEffect(() => {
    const t = setTimeout(() => void store.estimateProfile({ coins: draft.coins, style: draft.style, direction: draft.direction }), 400);
    return () => clearTimeout(t);
  }, [coinsKey, draft.style, draft.direction]);
  useEffect(() => {
    headingRef.current?.focus();
  }, [step]);

  // A coin another profile holds (its lock lasts until that profile is stopped and flat)
  const lockHolder = (coin: string) =>
    profiles.find((p) => LOCK_HOLDING.has(p.status) && p.instruments.some((i) => coinOf(i) === coin)) ?? null;
  const notInFeed = new Set(estimate.data?.feed.not_in_feed.map((x) => x.coin) ?? []);
  const switchOff = estimate.data?.master_switch === "OFF";

  // H3: on the review step, warn when the budget cannot place an order above the minimum (a warning, never a block)
  const [feasibility, setFeasibility] = useState<Feasibility>();
  const reviewTimeframes = presets.data ? effectiveTimeframes(presets.data, draft.style, draft.direction, { strategies: draft.strategies ?? [], timeframes: draft.timeframes ?? [] }) : [];
  useEffect(() => {
    if (step !== 3) return;
    let live = true;
    getFeasibility({ budget: draft.capital_budget_usd, maxPositionPct: draft.max_position_pct, maxRiskPct: draft.max_risk_per_trade_pct, maxLeverage: draft.max_leverage, markets: draft.markets, timeframes: reviewTimeframes })
      .then((f) => live && setFeasibility(f))
      .catch(() => live && setFeasibility(undefined));
    return () => {
      live = false;
    };
  }, [step, draft.capital_budget_usd, draft.max_position_pct, draft.max_risk_per_trade_pct, draft.max_leverage, draft.markets.join(","), reviewTimeframes.join(",")]);

  const next = () => {
    const e = validate(step, draft);
    setErrors(e);
    if (Object.keys(e).length > 0) {
      const first = Object.keys(e)[0];
      requestAnimationFrame(() => formRef.current?.querySelector<HTMLElement>(`#${CSS.escape(fid(first))}, [data-field="${first}"] input`)?.focus());
      return;
    }
    setFormError(null);
    setStep((s) => Math.min(3, s + 1));
  };

  const apiFail = (err: ApiError) => {
    if (err.fields) {
      const { instruments, ...rest } = err.fields;
      setErrors({ ...rest, ...(instruments ? { coins: instruments } : {}) } as Errors);
      const stepOf = (k: string) => (["name", "instruments", "markets", "capital_budget_usd"].includes(k) ? 0 : ["style", "direction"].includes(k) ? 1 : k === "dry_run" ? 3 : 2);
      setStep(stepOf(Object.keys(err.fields)[0]));
    }
    setFormError(errorCopy(err, draft.mode));
  };

  const create = async (code: string) => {
    if (isStaleBuild()) return;
    setPending(true);
    setTotpError(null);
    const snapshot = draft;
    try {
      const created = await store.createProfile(snapshot, code);
      setTotp(null);
      if (!created.started) {
        toast.warning(`Profile created, but couldn't start: ${errorCopy({ code: created.start_error?.code, message: created.start_error?.detail ?? "" }, snapshot.mode)} Review the profile and try Start again.`);
        onCreated(created.id);
        return;
      }
      if (snapshot.mode !== "ASK") {
        // A new profile starts in Ask me (ADR 0011); raising the mode is its own risk change and needs the next code
        toast.success(`Profile started in Ask me${snapshot.dry_run ? " and dry run" : ""}.`);
        setTotp({ id: created.id, name: created.name, mode: snapshot.mode });
        return;
      }
      toast.success(snapshot.dry_run ? "Profile started in dry run." : "Profile started.");
      onCreated(created.id);
    } catch (e) {
      const err = e as ApiError;
      if (isTotpError(err)) setTotpError(errorCopy(err));
      else {
        setTotp(null);
        apiFail(err);
      }
    } finally {
      setPending(false);
    }
  };

  const raiseMode = async (target: ModeStep, code: string) => {
    setPending(true);
    setTotpError(null);
    try {
      await store.setMode(target.id, target.mode, code);
      toast.success(`${target.name} is now in ${MODE_LABEL[target.mode]}.`);
      setTotp(null);
      onCreated(target.id);
    } catch (e) {
      const err = e as ApiError;
      if (isTotpError(err)) setTotpError(errorCopy(err));
      else {
        toast.error(`${target.name} stays in Ask me: ${errorCopy(err, target.mode)}`);
        setTotp(null);
        onCreated(target.id);
      }
    } finally {
      setPending(false);
    }
  };

  const err = (k: keyof Errors) => errors[k];
  const aria = (k: string, help = false) => ({
    id: fid(k),
    "aria-invalid": !!errors[k as keyof Errors],
    "aria-describedby": [errors[k as keyof Errors] ? `${fid(k)}-error` : "", help ? `${fid(k)}-help` : ""].filter(Boolean).join(" ") || undefined,
  });

  const numberField = (k: keyof ProfileDraft, label: string, opts: { help?: string; min?: number; max?: number; step?: number; placeholder?: string } = {}) => (
    <Field id={fid(k)} label={label} help={opts.help} error={err(k)}>
      <input
        {...aria(k, !!opts.help)}
        className={input}
        type="number"
        inputMode="decimal"
        min={opts.min}
        max={opts.max}
        step={opts.step ?? "any"}
        placeholder={opts.placeholder}
        value={String(draft[k] ?? "")}
        onChange={(e) => set({ [k]: e.target.value } as Partial<ProfileDraft>)}
      />
    </Field>
  );

  return (
    <section aria-labelledby={`${uid}-h`} className="mx-auto max-w-[960px] space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <h2 id={`${uid}-h`} ref={headingRef} tabIndex={-1} className="mr-auto text-lg font-bold text-zinc-100 outline-none">
          Create profile · Step {step + 1} of 4: {STEPS[step]}
        </h2>
        <button type="button" className={btnSecondary} onClick={() => (touched ? setDiscardOpen(true) : onCancel())}>
          Cancel
        </button>
      </div>
      <ol aria-label="Steps" className="flex flex-wrap gap-2 text-xs">
        {STEPS.map((s, i) => (
          <li key={s} aria-current={i === step ? "step" : undefined}
            className={`rounded-full border px-3 py-1 ${i === step ? "border-cyan-500 text-cyan-200" : i < step ? "border-zinc-600 text-zinc-300" : "border-zinc-800 text-zinc-500"}`}>
            {i + 1} {s}
          </li>
        ))}
      </ol>

      <form
        ref={formRef}
        noValidate
        onSubmit={(e) => {
          e.preventDefault();
          if (step < 3) next();
          else {
            setTotpError(null);
            setTotp("create");
          }
        }}
        className="space-y-5 rounded-xl border border-zinc-800 bg-zinc-950 p-4 sm:p-6"
      >
        {step === 0 && (
          <>
            <Field id={fid("name")} label="Profile name" error={err("name")}>
              <input {...aria("name")} className={input} maxLength={80} placeholder="e.g. BTC swing paper" value={draft.name} onChange={(e) => set({ name: e.target.value })} />
            </Field>
            <Field id={fid("coins")} label="Coins" help="Choose coins the live feed can cover. Separate with commas, e.g. BTCUSDT, ETHUSDT." error={err("coins")}>
              <input
                {...aria("coins", true)}
                className={input}
                value={coinText}
                onChange={(e) => {
                  setCoinText(e.target.value);
                  set({ coins: parseCoins(e.target.value) });
                }}
              />
            </Field>
            {draft.coins.length > 0 && (
              <ul className="space-y-1 text-xs" aria-label="Selected coins">
                {draft.coins.map((c) => {
                  const holder = lockHolder(c);
                  return (
                    <li key={c} className="flex flex-wrap items-center gap-2">
                      <span className="rounded bg-zinc-800 px-2 py-0.5 font-mono text-zinc-100">{c}</span>
                      {holder && (
                        <span className="text-amber-300">
                          {c} is used by profile {holder.name}.{" "}
                          <button type="button" className="underline" onClick={() => onOpenProfile(holder.id)}>
                            Open profile
                          </button>
                        </span>
                      )}
                      {notInFeed.has(c) && (
                        <span className="text-amber-300">
                          {c} is not in the live feed. Onboard it in Scanner Universe before starting this profile.{" "}
                          <button type="button" className="underline" onClick={() => dispatchSwitchWorkspace({ mode: "settings", section: "scanner" })}>
                            Open Scanner Universe
                          </button>
                        </span>
                      )}
                    </li>
                  );
                })}
              </ul>
            )}
            <fieldset className="space-y-2" data-field="markets">
              <legend className="text-xs font-semibold text-zinc-300">Markets</legend>
              <div className="flex flex-wrap gap-4">
                {(["SPOT", "SPOT_MARGIN", "PERP"] as Market[]).map((m) => (
                  <label key={m} className="inline-flex items-center gap-2 text-sm text-zinc-200">
                    <input
                      type="checkbox"
                      checked={draft.markets.includes(m)}
                      onChange={(e) => set({ markets: e.target.checked ? [...draft.markets, m] : draft.markets.filter((x) => x !== m) })}
                    />
                    {MARKET_LABEL[m]}
                  </label>
                ))}
              </div>
              {err("markets") && <p className="text-xs text-red-300">{err("markets")}</p>}
            </fieldset>
            {numberField("capital_budget_usd", "Capital budget (USD)", { min: 1 })}
            <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-3 text-xs text-zinc-300" aria-live="polite">
              {estimate.status === "loading" && <p role="status">Estimating feed coverage and daily cost…</p>}
              {estimate.status === "error" && <p>Estimate unavailable. You can save this draft, but feed coverage and daily cost are unknown.</p>}
              {estimate.status !== "loading" && estimate.data && (
                <>
                  <p>
                    The live feed supports {estimate.data.feed.ingest_max_symbols} symbols. {estimate.data.feed.not_in_feed.length} selected symbol
                    {estimate.data.feed.not_in_feed.length === 1 ? " is" : "s are"} outside the feed.
                  </p>
                  <p>
                    About {estimate.data.estimate.debates_per_day ?? "—"} debates a day, about $
                    {estimate.data.estimate.llm_usd_per_day ?? "—"} of model cost a day.
                  </p>
                  <p className="text-zinc-500">{estimate.data.estimate.method}</p>
                </>
              )}
              {estimate.status === "idle" && <p>Add coins to estimate feed coverage and daily cost.</p>}
            </div>
          </>
        )}

        {step === 1 && (
          <>
            <fieldset className="space-y-2">
              <legend className="text-xs font-semibold text-zinc-300">Start from a preset (optional)</legend>
              <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                {FIELD_PRESETS.map((p) => (
                  <label key={p.id} className={`flex cursor-pointer gap-2 rounded-lg border p-3 text-sm ${draft.preset === p.id ? "border-cyan-500 bg-cyan-950/30" : "border-zinc-800"}`}>
                    <input type="radio" name={`${uid}-preset`} checked={draft.preset === p.id} onChange={() => store.applyPreset(p.id)} className="mt-0.5" />
                    <span>
                      <span className="block font-semibold text-zinc-100">{p.label}</span>
                      <span className="block text-xs text-zinc-400">{p.blurb} Every value stays editable.</span>
                    </span>
                  </label>
                ))}
              </div>
            </fieldset>
            <RadioCards<Style>
              legend="Style"
              name={`${uid}-style`}
              value={draft.style}
              onChange={(style) => set({ style, strategies: [], timeframes: [] })}
              options={[
                { value: "SWING", label: "Swing", help: "1h and 4h frames." },
                { value: "INTRADAY", label: "Intraday", help: "15m and 1h frames." },
              ]}
            />
            <div data-field="direction">
              <RadioCards<Direction>
                legend="Direction"
                name={`${uid}-direction`}
                value={draft.direction}
                onChange={(direction) => set({
                  direction,
                  strategies: (draft.strategies ?? []).filter((s) => direction === "BOTH" || presets.data?.catalog[s]?.side === direction),
                })}
                options={[
                  { value: "LONG", label: "Long" },
                  { value: "SHORT", label: "Short", help: "Needs Spot Margin or Perpetuals." },
                  { value: "BOTH", label: "Long and short", help: "Shorts need Spot Margin or Perpetuals." },
                ]}
              />
              {err("direction") && <p className="mt-1 text-xs text-red-300">{err("direction")}</p>}
            </div>
            {presets.data && (
              <StrategyPicker stats={presets.data} style={draft.style} direction={draft.direction}
                value={{ strategies: draft.strategies ?? [], timeframes: draft.timeframes ?? [] }} onChange={(p) => set(p)} />
            )}
            <ReplayStats presets={presets} coins={draft.coins}
              ids={presets.data ? effectivePresets(presets.data, draft.style, draft.direction, draft.strategies ?? []) : []} />
          </>
        )}

        {step === 2 && (
          <>
            <p className="text-xs text-zinc-400">Daily loss limit must be at least risk per trade and no greater than maximum drawdown.</p>
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
              {numberField("max_risk_per_trade_pct", "Risk per trade (%)", { min: 0.1, max: 2, step: 0.1 })}
              {numberField("daily_loss_limit_pct", "Daily loss limit (%)", { help: "Leave blank for the default: 3 × risk per trade, capped at maximum drawdown.", placeholder: "default" })}
              {numberField("max_drawdown_pct", "Maximum drawdown (%)", { min: 0.1, max: 20 })}
              {numberField("max_position_pct", "Maximum position (% of capital)", { min: 1, max: 25, help: "Size for A and B grade setups; C grade uses half. The AI decides whether to trade, not how much." })}
              {numberField("max_leverage", "Maximum leverage", { min: 1, max: 10, step: 1 })}
              {numberField("max_open_positions", "Maximum open positions", { min: 1, max: 20, step: 1 })}
              {numberField("max_debates_per_day", "Maximum debates per day", { min: 0, max: 48, step: 1 })}
              {numberField("exit_tp_r", "Take profit at (R)", { min: 0.5, max: 20, step: 0.1 })}
              {numberField("exit_tp1_scale_pct", "Take profit 1 scale-out (%)", { min: 0, max: 100 })}
              {numberField("exit_trail_activate_pct", "Activate trailing exit at (%)", { placeholder: "off" })}
              {numberField("exit_trail_pct", "Trailing distance (%)", { placeholder: "off" })}
              {numberField("exit_max_hold_hours", "Maximum holding time (hours)", { placeholder: "off", step: 1 })}
              {numberField("breaker_max_loss_streak", "Losses before breaker", { min: 1, max: 50, step: 1 })}
              {numberField("breaker_max_reject_streak", "Rejected entries before breaker", { min: 1, max: 50, step: 1 })}
              <Field id={fid("notify_level")} label="Notifications">
                <select {...aria("notify_level")} className={input} value={draft.notify_level} onChange={(e) => set({ notify_level: e.target.value as NotifyLevel })}>
                  <option value="ALL">All</option>
                  <option value="IMPORTANT">Important only</option>
                  <option value="NONE">None</option>
                </select>
              </Field>
            </div>
            {draft.markets.some((m) => m !== "SPOT") && (
              <p className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-3 text-xs text-zinc-300">
                Up to {draft.max_leverage}× leverage. Each trade still risks at most {draft.max_risk_per_trade_pct}% of capital at its stop; leverage changes how
                close liquidation is. Liquidation prices appear per position on the Positions tab once a trade opens.
              </p>
            )}
          </>
        )}

        {step === 3 && (
          <>
            <RadioCards<ExecutionMode>
              legend="Mode"
              name={`${uid}-mode`}
              value={draft.mode}
              onChange={(mode) => set({ mode })}
              options={[
                { value: "ASK", label: "Ask me", help: "Review and approve each new entry." },
                { value: "VETO", label: "Veto window", help: "New entries are placed after 600 seconds unless you veto them." },
                { value: "AUTO", label: "Fully automatic", help: "New entries can be placed automatically when the auto-trade switch is on." },
              ]}
            />
            {draft.mode !== "ASK" && (
              <p className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-3 text-xs text-zinc-300">
                A new profile starts in Ask me. After it is created you&apos;ll be asked for a second TOTP code (each code works once) to switch it to{" "}
                {MODE_LABEL[draft.mode]}.
              </p>
            )}
            {switchOff && draft.mode !== "ASK" && (
              <p role="alert" className="rounded-lg border border-amber-800/60 bg-amber-950/20 p-3 text-xs text-amber-200">
                The auto-trade switch is off, so this profile can&apos;t start in {MODE_LABEL[draft.mode]}. Choose Ask me, or create it now and start it after
                turning the switch on in Settings.
              </p>
            )}
            <label className="flex items-start gap-2 text-sm text-zinc-200">
              <input type="checkbox" checked={draft.dry_run} onChange={(e) => set({ dry_run: e.target.checked })} className="mt-1" />
              <span>
                Start in dry run
                <span className="block text-xs text-zinc-400">Simulate orders first. You can turn dry run off later.</span>
              </span>
            </label>
            <FeasibilityNotes feasibility={feasibility} budget={draft.capital_budget_usd} riskPct={draft.max_risk_per_trade_pct} />
            <Summary draft={draft} estimateText={estimate.data ? `${estimate.data.feed.not_in_feed.length} coin(s) outside the live feed` : "Feed coverage unknown"} presetIds={presets.data ? effectivePresets(presets.data, draft.style, draft.direction, draft.strategies ?? []) : []}
              timeframes={presets.data ? effectiveTimeframes(presets.data, draft.style, draft.direction, { strategies: draft.strategies ?? [], timeframes: draft.timeframes ?? [] }) : []} />
          </>
        )}

        {formError && (
          <p role="alert" className="text-sm text-red-300">
            {formError}
          </p>
        )}

        <div className="sticky bottom-0 -mx-4 flex flex-wrap justify-end gap-2 border-t border-zinc-800 bg-zinc-950 px-4 pt-3 sm:static sm:mx-0 sm:px-0">
          {step > 0 && (
            <button type="button" className={btnSecondary} onClick={() => setStep((s) => s - 1)}>
              Back
            </button>
          )}
          {step === 3 && (
            <button type="button" className={btnSecondary} onClick={() => toast.success("Draft saved on this device.")}>
              Save draft
            </button>
          )}
          <button type="submit" className={btnPrimary} disabled={step === 3 && staleBuild} title={staleBuild ? STALE_BUILD_TITLE : undefined}>
            {step < 3 ? "Continue" : "Create and start"}
          </button>
        </div>
      </form>

      <ConfirmDialog
        open={discardOpen}
        title="Discard profile draft?"
        body="Your changes will be lost."
        confirmLabel="Discard draft"
        danger
        onClose={() => setDiscardOpen(false)}
        onConfirm={() => {
          store.setWizardDraft(null);
          setDiscardOpen(false);
          onCancel();
        }}
      />
      <TotpDialog
        open={totp !== null}
        change={totp === "create" ? `Creating ${draft.name || "this profile"}` : `Switching ${typeof totp === "object" && totp ? `${totp.name} to ${MODE_LABEL[totp.mode]}` : ""}`}
        confirmLabel={totp === "create" ? "Confirm and start" : "Confirm mode"}
        pending={pending}
        error={totpError}
        onClose={() => {
          const t = totp;
          setTotp(null);
          setTotpError(null);
          if (t && typeof t === "object") onCreated(t.id); // created already; it stays in Ask me
        }}
        onSubmit={(code) => (totp === "create" ? void create(code) : totp && typeof totp === "object" ? void raiseMode(totp, code) : undefined)}
      />
    </section>
  );
}

function Summary({ draft: d, estimateText, presetIds, timeframes }: { draft: ProfileDraft; estimateText: string; presetIds: string[]; timeframes: string[] }) {
  const rows: [string, string][] = [
    ["Name", d.name || "—"],
    ["Coins", d.coins.join(", ") || "—"],
    ["Markets", d.markets.map((m) => MARKET_LABEL[m]).join(", ")],
    ["Capital budget", `$${d.capital_budget_usd}`],
    ["Style and direction", `${d.style === "SWING" ? "Swing" : "Intraday"} · ${d.direction === "BOTH" ? "Long and short" : d.direction === "LONG" ? "Long" : "Short"}`],
    ["Strategies", `${presetIds.join(", ") || "—"}${d.strategies?.length ? "" : " (style defaults)"}`],
    ["Timeframes", `${timeframes.join(", ") || "—"}${d.timeframes?.length ? "" : " (style defaults)"}`],
    ["Risk per trade", `${d.max_risk_per_trade_pct}%`],
    ["Daily loss limit", d.daily_loss_limit_pct ? `${d.daily_loss_limit_pct}%` : "Default (3 × risk per trade, capped at maximum drawdown)"],
    ["Maximum drawdown", `${d.max_drawdown_pct}%`],
    ["Maximum position", `${d.max_position_pct}% of capital`],
    ["Leverage", `up to ${d.max_leverage}×`],
    ["Open positions / debates a day", `${d.max_open_positions} / ${d.max_debates_per_day}`],
    ["Exits", `take profit at ${d.exit_tp_r}R (${d.exit_tp1_scale_pct}% at TP1)${d.exit_trail_pct ? `, trail ${d.exit_trail_pct}% after +${d.exit_trail_activate_pct || "?"}%` : ""}${d.exit_max_hold_hours ? `, max hold ${d.exit_max_hold_hours} h` : ""}`],
    ["Breakers", `${d.breaker_max_loss_streak} losses or ${d.breaker_max_reject_streak} rejected entries in a row`],
    ["Notifications", d.notify_level === "ALL" ? "All" : d.notify_level === "IMPORTANT" ? "Important only" : "None"],
    ["Mode", MODE_LABEL[d.mode]],
    ["Environment", "Paper"],
    ["Dry run", d.dry_run ? "Yes: orders are simulated" : "No"],
    ["Feed", estimateText],
  ];
  return (
    <dl className="grid grid-cols-1 gap-x-4 gap-y-1 rounded-lg border border-zinc-800 p-3 text-xs sm:grid-cols-[max-content_1fr]">
      {rows.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-zinc-500">{k}</dt>
          <dd className="text-zinc-100">{v}</dd>
        </div>
      ))}
    </dl>
  );
}
