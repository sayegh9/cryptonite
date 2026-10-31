/** The four wizard field presets (spec "Four field presets"): starting values only; every field stays editable. */
import type { FieldPreset, ProfileDraft } from "./types";
import type { ProfileProposalSpec } from "@/lib/brain/types";

/** ProfileFields defaults (packages/schemas/cryptonite_schemas/profile.py); the first-start wizard suggests dry run. */
export const EMPTY_DRAFT: ProfileDraft = {
  name: "",
  coins: [],
  markets: ["SPOT"],
  capital_budget_usd: "1000",
  style: "SWING",
  direction: "LONG",
  max_risk_per_trade_pct: "1",
  daily_loss_limit_pct: "",
  max_drawdown_pct: "20",
  max_position_pct: "10",
  max_leverage: "1",
  max_open_positions: "3",
  max_debates_per_day: "6",
  exit_tp_r: "2",
  exit_tp1_scale_pct: "50",
  exit_trail_activate_pct: "",
  exit_trail_pct: "",
  exit_max_hold_hours: "",
  notify_level: "IMPORTANT",
  breaker_max_loss_streak: "4",
  breaker_max_reject_streak: "5",
  mode: "ASK",
  dry_run: true,
  preset: null,
};

export const FIELD_PRESETS: { id: FieldPreset; label: string; blurb: string; fields: Partial<ProfileDraft> }[] = [
  {
    id: "conservative-swing",
    label: "Conservative swing",
    blurb: "Spot only, long only, 1× leverage, 0.5% risk per trade.",
    fields: { markets: ["SPOT"], style: "SWING", direction: "LONG", max_leverage: "1", max_risk_per_trade_pct: "0.5", daily_loss_limit_pct: "1.5", max_drawdown_pct: "20" },
  },
  {
    id: "leveraged-trend",
    label: "Leveraged trend",
    blurb: "Perpetuals, long and short, up to 3× leverage, 1% risk per trade.",
    fields: { markets: ["PERP"], style: "SWING", direction: "BOTH", max_leverage: "3", max_risk_per_trade_pct: "1", daily_loss_limit_pct: "3", max_drawdown_pct: "20" },
  },
  {
    id: "intraday-active",
    label: "Intraday active",
    blurb: "Spot, long only, 15m/1h frames, up to 12 debates a day.",
    fields: { markets: ["SPOT"], style: "INTRADAY", direction: "LONG", max_leverage: "1", max_risk_per_trade_pct: "0.5", daily_loss_limit_pct: "1.5", max_debates_per_day: "12" },
  },
  {
    id: "short-only-hedge",
    label: "Short-only hedge",
    blurb: "Spot Margin and Perpetuals, short only, up to 2× leverage.",
    fields: { markets: ["SPOT_MARGIN", "PERP"], style: "SWING", direction: "SHORT", max_leverage: "2", max_risk_per_trade_pct: "0.5", daily_loss_limit_pct: "1.5", max_drawdown_pct: "20" },
  },
];

export function applyFieldPreset(draft: ProfileDraft, id: FieldPreset): ProfileDraft {
  const preset = FIELD_PRESETS.find((p) => p.id === id);
  if (!preset) return draft;
  // Fields a preset does not list go back to the schema defaults, so presets do not leak into each other
  const reset: Partial<ProfileDraft> = { max_debates_per_day: EMPTY_DRAFT.max_debates_per_day, max_drawdown_pct: EMPTY_DRAFT.max_drawdown_pct };
  return { ...draft, ...reset, ...preset.fields, preset: id };
}

/** Copy a Brain proposal into the existing persisted wizard draft without changing its risk choices. */
export function draftFromProposal(p: ProfileProposalSpec): ProfileDraft {
  const text = (value: string | number | null | undefined) => value == null ? "" : String(value);
  return {
    ...EMPTY_DRAFT,
    name: p.name,
    coins: [...p.coins],
    markets: [...p.markets],
    capital_budget_usd: text(p.capital_budget_usd),
    style: p.style,
    direction: p.direction,
    max_position_pct: text(p.max_position_pct),
    max_leverage: text(p.max_leverage),
    max_open_positions: text(p.max_open_positions),
    max_debates_per_day: text(p.max_debates_per_day),
    max_risk_per_trade_pct: text(p.max_risk_per_trade_pct),
    daily_loss_limit_pct: text(p.daily_loss_limit_pct),
    max_drawdown_pct: text(p.max_drawdown_pct),
    exit_tp_r: text(p.exit_tp_r),
    exit_tp1_scale_pct: text(p.exit_tp1_scale_pct),
    exit_trail_activate_pct: text(p.exit_trail_activate_pct),
    exit_trail_pct: text(p.exit_trail_pct),
    exit_max_hold_hours: text(p.exit_max_hold_hours),
    notify_level: p.notify_level,
    breaker_max_loss_streak: text(p.breaker_max_loss_streak),
    breaker_max_reject_streak: text(p.breaker_max_reject_streak),
    mode: p.mode,
    dry_run: p.dry_run,
    preset: null,
  };
}

const num = (s: string) => s.trim();
const optional = (s: string) => (s.trim() === "" ? undefined : s.trim());

/** POST /ai/mandates body from a draft. Instruments follow the markets: one per coin and market. */
export function createBody(d: ProfileDraft): Record<string, unknown> {
  const suffix = { SPOT: "", SPOT_MARGIN: "-MARGIN", PERP: "-PERP" } as const;
  const instruments = d.coins.flatMap((c) => d.markets.map((m) => `${c}${suffix[m]}.BINANCE`));
  const body: Record<string, unknown> = {
    name: d.name.trim(),
    capital_budget_usd: num(d.capital_budget_usd),
    instruments,
    max_position_pct: num(d.max_position_pct),
    markets: d.markets,
    allow_perps: d.markets.includes("PERP"),
    style: d.style,
    direction: d.direction,
    environment: "PAPER",
    max_risk_per_trade_pct: num(d.max_risk_per_trade_pct),
    max_drawdown_pct: num(d.max_drawdown_pct),
    max_leverage: Number(d.max_leverage),
    max_open_positions: Number(d.max_open_positions),
    max_debates_per_day: Number(d.max_debates_per_day),
    exit_tp_r: num(d.exit_tp_r),
    exit_tp1_scale_pct: num(d.exit_tp1_scale_pct),
    notify_level: d.notify_level,
    breaker_max_loss_streak: Number(d.breaker_max_loss_streak),
    breaker_max_reject_streak: Number(d.breaker_max_reject_streak),
    dry_run: d.dry_run,
  };
  for (const k of ["daily_loss_limit_pct", "exit_trail_activate_pct", "exit_trail_pct"] as const) {
    const v = optional(d[k]);
    if (v !== undefined) body[k] = v;
  }
  if (optional(d.exit_max_hold_hours) !== undefined) body.exit_max_hold_hours = Number(d.exit_max_hold_hours);
  return body;
}

export const parseCoins = (text: string) =>
  Array.from(
    new Set(
      text
        .split(/[\s,]+/)
        // a coin, not an instrument: the markets choose -MARGIN / -PERP
        .map((c) => c.trim().toUpperCase().replace(/\.BINANCE$/, "").replace(/-(PERP|MARGIN)$/, ""))
        .filter(Boolean),
    ),
  );
