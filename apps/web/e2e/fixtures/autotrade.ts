import type { Page, Route } from "@playwright/test";

/**
 * Auto-Trade backend stubs. Every /api/v1/ai/* call is answered here (the offline fixture answers the rest with 503),
 * so these specs never reach a real API. Writes are recorded with their bodies for assertions.
 */
export const P1 = "aaaaaaaa-1111-4111-8111-111111111111";
export const P2 = "bbbbbbbb-2222-4222-8222-222222222222";
export const VALID_TOTP = "123456";

export function profile(over: Record<string, unknown> = {}) {
  return {
    id: P1,
    name: "BTC swing paper",
    capital_budget_usd: "1000",
    instruments: ["BTCUSDT-PERP.BINANCE"],
    max_position_pct: "10",
    max_drawdown_pct: "20",
    allow_perps: true,
    max_leverage: 2,
    max_risk_per_trade_pct: "1",
    strategies: [],
    autonomy_level: 1,
    level_name: "L1 Copilot",
    status: "ACTIVE",
    active_hours_utc: null,
    veto_window_s: 600,
    halted_reason: null,
    halted_at: null,
    level_since: "2026-10-28T00:00:00Z",
    equity: "1010",
    hwm: "1020",
    drawdown_pct: "0.98",
    exposure_usd: "200",
    perp_exposure_usd: "200",
    budget_used_pct: "20",
    positions: [
      {
        instrument_id: "BTCUSDT-PERP.BINANCE", side: "LONG", quantity: "0.002", entry_price: "100000", current_price: "101000",
        unrealized_pnl: "2", mark_source: "live", mark_stale: false, mark_age_s: 1, leverage: 2, margin_mode: "ISOLATED",
        isolated_margin: "100", notional_usd: "202", liquidation_price: "50500", liquidation_distance_pct: "50.0",
      },
    ],
    pending_approvals: 0,
    pending_proposal: null,
    daily_loss_limit_pct: "3",
    daily_profit_target_pct: "5",
    execution_mode: "ASK",
    markets: ["PERP"],
    direction: "BOTH",
    style: "SWING",
    environment: "PAPER",
    max_open_positions: 3,
    max_debates_per_day: 6,
    exit_tp_r: "2",
    exit_tp1_scale_pct: "50",
    exit_trail_activate_pct: null,
    exit_trail_pct: null,
    exit_max_hold_hours: null,
    dry_run: false,
    notify_level: "IMPORTANT",
    breaker_max_loss_streak: 4,
    breaker_max_reject_streak: 5,
    paused_reason: null,
    ...over,
  };
}

export const PAUSED = profile({ id: P2, name: "XRP short hedge", status: "PAUSED", paused_reason: "BREAKER_LOSS_STREAK", instruments: ["XRPUSDT-MARGIN.BINANCE"], markets: ["SPOT_MARGIN"], direction: "SHORT", positions: [] });

export function summary(id: string, over: Record<string, unknown> = {}) {
  return {
    profile_id: id,
    status: "ACTIVE",
    today: { date_utc: "2026-10-30", realized_pnl: "-4", unrealized_pnl: "2", pnl: "-2", session_pnl: "-2", daily_loss_limit_usd: "30", daily_profit_target_usd: "50" },
    open_positions_count: 1,
    last_decision: { id: 9, mandate_id: id, ts: new Date(Date.now() - 600_000).toISOString(), instrument_id: "BTCUSDT-PERP.BINANCE", stage: "GATE", outcome: "REFUSED", reason: "max open positions", detail: {}, run_id: null, command_id: null },
    last_decision_at: new Date(Date.now() - 600_000).toISOString(),
    last_decision_age_seconds: 600,
    breaker: { active: false, state: "CLEAR", reason: null },
    dry_run: false,
    mode: { level: 1, label: "Ask me" },
    master_switch: "OFF",
    health_score: {
      score: 100,
      running: true,
      components: {
        freshness: { points: 40, fresh_instruments: 1, instruments: [{ instrument_id: "BTCUSDT-PERP.BINANCE", fresh: true, age_seconds: 1 }] },
        recent_failures: { points: 30, failed_debates_24h: 0 },
        breaker: { points: 30, active: false, reason: null },
      },
    },
    ...over,
  };
}

const PRESET_STATS = {
  generated_at: "2026-10-30T09:34:25Z",
  source_command: "python /app/preset_replay.py --lake /lake",
  results: [
    { preset: "swing-squeeze-breakdown-short-1h", coin: "BTCUSDT", trades: 49, win_rate: 0.224, avg_r: -0.63, quality: "negative" },
    { preset: "swing-squeeze-breakdown-short-1h", coin: "ETHUSDT", trades: 56, win_rate: 0.464, avg_r: 0.17, quality: "ok" },
    { preset: "swing-trend-pullback-short-1h", coin: "ETHUSDT", trades: 3, win_rate: 0.33, avg_r: 0.1, quality: "thin" },
  ],
  preset_ids: {
    "SWING:LONG": ["swing-trend-pullback-1h", "swing-squeeze-breakout-1h", "swing-sniper-pullback-15m"],
    "SWING:SHORT": ["swing-trend-pullback-short-1h", "swing-squeeze-breakdown-short-1h", "swing-sniper-pullback-short-15m"],
    "SWING:BOTH": ["swing-trend-pullback-1h", "swing-squeeze-breakout-1h", "swing-sniper-pullback-15m", "swing-trend-pullback-short-1h", "swing-squeeze-breakdown-short-1h", "swing-sniper-pullback-short-15m"],
    "INTRADAY:LONG": ["scalp-multi-tf", "trend-pullback", "bollinger-breakout"],
    "INTRADAY:SHORT": ["scalp-multi-tf-short", "trend-pullback-short", "bollinger-breakdown-short"],
    "INTRADAY:BOTH": ["scalp-multi-tf", "trend-pullback", "bollinger-breakout", "scalp-multi-tf-short", "trend-pullback-short", "bollinger-breakdown-short"],
  },
};

export interface Backend {
  writes: { method: string; path: string; body: Record<string, unknown> }[];
  profiles: ReturnType<typeof profile>[];
}

const json = (route: Route, body: unknown, status = 200) => route.fulfill({ status, json: body });

export async function mockAutoTrade(page: Page, opts: { profiles?: ReturnType<typeof profile>[] } = {}): Promise<Backend> {
  const be: Backend = { writes: [], profiles: opts.profiles ?? [profile(), PAUSED] };
  await page.addInitScript(() => {
    localStorage.setItem("csrf_token", "csrf-test");
    localStorage.setItem("cryptonite:ai-brain-tour-seen", "true");
  });
  await page.route(/\/api\/v1\/ai\//, async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const path = url.pathname.replace(/^\/api\/v1\/ai/, "");
    const method = req.method();
    const body = method === "GET" ? {} : (req.postDataJSON() ?? {});
    if (method !== "GET") be.writes.push({ method, path, body });
    const byId = (id: string) => be.profiles.find((p) => p.id === id);
    let m: RegExpMatchArray | null;

    if (method === "GET" && path === "/mandates") return json(route, { mandates: be.profiles });
    if (method === "GET" && (m = path.match(/^\/mandates\/([^/]+)$/))) return byId(m[1]) ? json(route, byId(m[1])) : json(route, { detail: "Mandate not found" }, 404);
    if (method === "GET" && path === "/profiles/summaries")
      return json(route, { as_of: "2026-10-30T10:00:00Z", summaries: be.profiles.filter((p) => p.status !== "ARCHIVED").map((p) => summary(p.id, { status: p.status })) });
    if (method === "GET" && (m = path.match(/^\/profiles\/([^/]+)\/summary$/))) return json(route, summary(m[1], { status: byId(m[1])?.status }));
    if (method === "GET" && (m = path.match(/^\/profiles\/([^/]+)\/decisions$/)))
      return json(route, {
        profile_id: m[1],
        decisions: [
          { id: 9, mandate_id: m[1], ts: "2026-10-30T10:00:00Z", instrument_id: "BTCUSDT-PERP.BINANCE", stage: "GATE", outcome: "REFUSED", reason: "max open positions", detail: {}, run_id: null, command_id: null },
          { id: 8, mandate_id: m[1], ts: "2026-10-30T09:00:00Z", instrument_id: null, stage: "LIFECYCLE", outcome: "STARTED", reason: null, detail: {}, run_id: null, command_id: null },
        ],
        next_cursor: url.searchParams.get("cursor") ? null : "older",
        has_more: !url.searchParams.get("cursor"),
      });
    if (method === "GET" && (m = path.match(/^\/profiles\/([^/]+)\/performance$/)))
      return json(route, {
        profile_id: m[1], range: url.searchParams.get("range") ?? "7d",
        equity_curve: [{ ts: "2026-10-29T00:00:00Z", equity: "1000", hwm: "1000", drawdown_pct: "0" }, { ts: "2026-10-30T00:00:00Z", equity: "1010", hwm: "1020", drawdown_pct: "0.98" }],
        equity_curve_sampled: false,
        fills: [{ trade_id: "T1", client_order_id: "c1", instrument_id: "BTCUSDT-PERP.BINANCE", side: "BUY", price: "100000", quantity: "0.002", fee: "0.08", fee_currency: "USDT", executed_at: "2026-10-29T02:00:00Z" }],
        exit_outcomes: [{ decision_id: 7, instrument_id: "BTCUSDT-PERP.BINANCE", realized_pnl: "-4", r_multiple: null, timestamp: "2026-10-29T02:41:00Z", outcome: "STOP" }],
        stats: {
          audited_exit_count: 1, audited_wins: 0, audited_win_rate: "0", avg_r: null, r_known: 0, r_unknown: 1,
          outcome_coverage: { scope: "EXIT audit decisions only", complete: false, profile_fill_count: 1, audited_exit_count: 1, note: "some close fills have no EXIT audit row." },
          fees: { total_amount: "0.08", by_currency: { USDT: "0.08" } }, funding: "0", margin_interest_by_asset: {}, max_drawdown_pct: "0.98",
        },
      });
    if (method === "GET" && path === "/profiles/presets/stats") return json(route, PRESET_STATS);
    if (method === "GET" && path === "/profiles/estimate") {
      const coins = (url.searchParams.get("coins") ?? "").split(",").filter(Boolean);
      return json(route, {
        coins, master_switch: "OFF", style: url.searchParams.get("style"), direction: url.searchParams.get("direction"),
        feed: { ingest_max_symbols: 20, coins_in_feed: coins.filter((c) => c !== "DOGEUSDT"), not_in_feed: coins.filter((c) => c === "DOGEUSDT").map((coin) => ({ coin, code: "NOT_IN_FEED" })) },
        estimate: { debates_per_day: "3", llm_usd_per_day: "0.12", method: "Historical candidate runs per observed coin over the last 7 UTC days." },
      });
    }
    if (method === "POST" && path === "/mandates") {
      if (body.totp_code !== VALID_TOTP) return json(route, { detail: "TOTP_REQUIRED: this change gives the AI more room; enter your current TOTP code" }, 401);
      const created = profile({ id: "cccccccc-3333-4333-8333-333333333333", name: body.name, status: "ACTIVE", dry_run: body.dry_run, positions: [] });
      be.profiles.push(created);
      return json(route, { ...created, started: true, start_error: null }, 201);
    }
    if (method === "PATCH" && (m = path.match(/^\/mandates\/([^/]+)$/))) {
      if ("max_risk_per_trade_pct" in body && Number(body.max_risk_per_trade_pct) > 1 && body.totp_code !== VALID_TOTP)
        return json(route, { detail: "TOTP_REQUIRED: this change gives the AI more room; enter your current TOTP code" }, 401);
      const p = byId(m[1])!;
      Object.assign(p, Object.fromEntries(Object.entries(body).filter(([k]) => k !== "totp_code")));
      return json(route, p);
    }
    if (method === "POST" && (m = path.match(/^\/mandates\/([^/]+)\/(pause|stop|resume|start|archive)$/))) {
      const p = byId(m[1])!;
      if (m[2] === "resume" && body.totp_code !== VALID_TOTP) return json(route, { detail: "TOTP_REQUIRED: enter your current TOTP code" }, 401);
      p.status = { pause: "PAUSED", stop: "STOPPING", resume: "ACTIVE", start: "ACTIVE", archive: "ARCHIVED" }[m[2]]!;
      return json(route, p);
    }
    if (method === "POST" && path === "/profiles/stop-all") {
      const eligible = be.profiles.filter((p) => p.status === "ACTIVE" || p.status === "PAUSED");
      for (const p of eligible) p.status = "STOPPING";
      return json(route, { close: !!body.close, selected_count: eligible.length, requested_count: eligible.length, refused_count: 0, profiles: eligible.map((p) => ({ id: p.id, status: "STOPPING", outcome: "STOP_REQUESTED" })) });
    }
    if (method === "POST" && (m = path.match(/^\/mandates\/([^/]+)\/autonomy\/confirm$/))) {
      if (body.totp_code !== VALID_TOTP) return json(route, { detail: "TOTP_REQUIRED: enter your current TOTP code" }, 401);
      const p = byId(m[1])!;
      p.autonomy_level = body.level as number;
      p.execution_mode = ({ 1: "ASK", 2: "VETO", 3: "AUTO" } as Record<number, string>)[body.level as number];
      return json(route, { mandate_id: p.id, level: body.level });
    }
    if (method === "GET" && path.startsWith("/approvals")) return json(route, { now: new Date().toISOString(), approvals: [] });
    if (method === "GET" && path === "/status")
      return json(route, { kill_switch: { latched: false }, halted_mandates: [], account: null, exit_monitor_up: true, notifier_up: true, budget_alert: false, degraded: [] });
    return json(route, { detail: "not stubbed" }, 503);
  });
  return be;
}
