import { test, expect, type Page } from "@playwright/test";

/**
 * Phase 8 reporting features with stubbed APIs (all reporting only, no effect on trading):
 * F1 outcomes by regime (Mission Control → Autonomy & Evals) and the tearsheet's regime breakdown,
 * F3 the Research Lab's live-vs-backtest panel, F5 the isotonic step line and "calibrated ≈ p".
 */

const M1 = "11111111-1111-4111-8111-111111111111";

const MANDATE = {
  id: M1, name: "Alt-momentum", capital_budget_usd: "10000", instruments: ["BTCUSDT.BINANCE"], max_position_pct: "10",
  max_drawdown_pct: "20", allow_perps: false, max_leverage: 1, strategies: [], autonomy_level: 1, level_name: "L1 Copilot",
  status: "ACTIVE", active_hours_utc: null, veto_window_s: 600, halted_reason: null, halted_at: null,
  level_since: "2026-09-01T00:00:00Z", equity: "10420", hwm: "10600", drawdown_pct: "1.70", exposure_usd: "1200",
  budget_used_pct: "12", positions: [], pending_approvals: 1, pending_proposal: null,
};

const AUTONOMY = {
  mandate_id: M1, name: "Alt-momentum", status: "ACTIVE", level: 1, level_name: "L1 Copilot", level_since: "2026-08-25T00:00:00Z",
  requested_level: null, proposal: null,
  gate: { level: 1, target_level: 2, passed: false, checks: [{ name: "closed_trades", required: ">= 30", actual: "12", passed: false }], metrics: {} },
  events: [],
};

const BINS = [
  { lo: 0, hi: 0.2, n: 0, mean_confidence: null, hit_rate: null },
  { lo: 0.2, hi: 0.4, n: 60, mean_confidence: 0.31, hit_rate: 0.3 },
  { lo: 0.4, hi: 0.6, n: 90, mean_confidence: 0.52, hit_rate: 0.45 },
  { lo: 0.6, hi: 0.8, n: 70, mean_confidence: 0.7, hit_rate: 0.6 },
  { lo: 0.8, hi: 1, n: 20, mean_confidence: 0.85, hit_rate: 0.65 },
];

function approval(conviction: number | null) {
  return {
    id: "aaaaaaaa-0000-4000-8000-000000000001", mandate_id: M1, mandate: "Alt-momentum", action_class: "ADD_RISK",
    command_type: "SUBMIT_ORDER", summary: "BUY BTCUSDT 5% of mandate ($500)", source: "debate", level: 1, timeout_policy: "DENY",
    status: "PENDING", expires_at: new Date(Date.now() + 500_000).toISOString(), seconds_left: 500, decided_via: null,
    command_id: null, trace_id: null, created_at: new Date().toISOString(),
    payload: { instrument_id: "BTCUSDT.BINANCE", side: "BUY", conviction_score: conviction },
  };
}

async function mockMission(page: Page, opts: { byRegime?: unknown; isotonic?: unknown; approvals?: unknown[] } = {}) {
  await page.route("**/api/v1/ai/status", (route) =>
    route.fulfill({
      json: {
        kill_switch: { latched: false, reason: null, latched_by: null }, halted_mandates: [],
        account: { equity: "100420", hwm: "101000", drawdown_pct: "0.57", backstop_pct: "20" },
        exit_monitor_up: true, notifier_up: true, budget_alert: false, degraded: [],
      },
    }),
  );
  await page.route("**/api/v1/ai/mandates", (route) => route.fulfill({ json: { mandates: [MANDATE] } }));
  await page.route(/\/api\/v1\/ai\/approvals\?/, (route) =>
    route.fulfill({ json: { now: new Date().toISOString(), approvals: opts.approvals ?? [] } }),
  );
  await page.route("**/api/v1/ai/mandates/*/autonomy", (route) => route.fulfill({ json: AUTONOMY }));
  await page.route("**/api/v1/ai/evals", (route) =>
    route.fulfill({
      json: {
        runs: [],
        calibration: { "debate.verdict": { n: 240, min_samples: 20, ece_pp: 6.1, bins: BINS, ...(opts.isotonic ? { isotonic: opts.isotonic } : {}) } },
        calibration_note: "Verdict conviction vs 24h outcome over 240 labelled decisions: expected calibration error 6.1 pp.",
      },
    }),
  );
  await page.route(/\/api\/v1\/ai\/outcomes\/by-regime/, (route) =>
    opts.byRegime === undefined ? route.fulfill({ status: 500, json: { detail: "down" } }) : route.fulfill({ json: opts.byRegime }),
  );
  await page.route("**/api/v1/control/kill-switch/status*", (route) =>
    route.fulfill({ json: { environment: "PAPER", account_id: "PAPER-ACCOUNT-001", is_latched: false } }),
  );
}

async function openAutonomy(page: Page) {
  await page.addInitScript(() => localStorage.setItem("csrf_token", "csrf-test"));
  await page.goto("/");
  await page.getByRole("button", { name: "Mission Control", exact: true }).click();
  await expect(page.getByTestId("mission-control")).toBeVisible();
  await page.getByRole("tab", { name: "Autonomy & Evals" }).click();
}

test.describe("Phase 8 reporting: Mission Control", () => {
  test("outcomes by regime table, isotonic step line and calibrated conviction on the approval", async ({ page }) => {
    const regimeCalls: string[] = [];
    page.on("request", (r) => {
      if (r.url().includes("/ai/outcomes/by-regime")) regimeCalls.push(r.url());
    });
    await mockMission(page, {
      byRegime: {
        horizon: "24h", mandate_id: M1, available: true,
        regimes: [
          { regime: "CHOP", n: 12, wins: 4, losses: 6, neutral: 2, mean_signed_return: -0.0031 },
          { regime: "TREND_UP", n: 20, wins: 13, losses: 6, neutral: 1, mean_signed_return: 0.0124 },
        ],
      },
      isotonic: { fitted: true, n: 240, min_points: 200, points: [[0.2, 0.25], [0.5, 0.25], [0.55, 0.48], [0.75, 0.48], [0.8, 0.62], [0.95, 0.62]] },
      approvals: [approval(0.8)],
    });
    await openAutonomy(page);

    const table = page.getByRole("table", { name: "Outcomes by regime" });
    await expect(table.getByRole("row")).toHaveCount(3);
    await expect(table.getByRole("row").nth(1)).toContainText("Trend up"); // regime order, not server order
    await expect(page.getByTestId("regime-outcome-TREND_UP")).toContainText("+1.24%");
    await expect(page.getByTestId("regime-outcome-CHOP")).toContainText("-0.31%");
    expect(regimeCalls.some((u) => u.includes(`mandate_id=${M1}`) && u.includes("horizon=24h"))).toBe(true);

    await expect(page.getByTestId("isotonic-debate.verdict")).toContainText("shown dashed (informational)");
    await expect(page.getByTestId("isotonic-line-debate.verdict")).toBeAttached();

    // F5: with the fit loaded, the approval card shows the calibrated value next to the raw conviction
    await page.getByRole("tab", { name: "Overview" }).click();
    await expect(page.getByTestId("calibrated-conviction")).toHaveText("conviction 80% · calibrated ≈ 62% (informational)");
  });

  test("absent data shows a dash and no fitted line", async ({ page }) => {
    await mockMission(page, { isotonic: { fitted: false, n: 120, min_points: 200, points: [] }, approvals: [approval(0.8)] });
    await openAutonomy(page);
    await expect(page.getByTestId("outcomes-by-regime")).toContainText("—");
    await expect(page.getByRole("table", { name: "Outcomes by regime" })).toHaveCount(0);
    await expect(page.getByTestId("isotonic-debate.verdict")).toContainText("not fitted (needs 200)");
    await expect(page.getByTestId("isotonic-line-debate.verdict")).toHaveCount(0);
    await page.getByRole("tab", { name: "Overview" }).click();
    await expect(page.getByTestId("approval-row")).toHaveCount(1);
    await expect(page.getByTestId("calibrated-conviction")).toHaveCount(0);
  });
});

// --- Research Lab ------------------------------------------------------------------------------------

const RUN_ID = "run-p8-001";

function backtestRun(regimeBreakdown: unknown) {
  return {
    id: RUN_ID, strategy_slug: "ema-cross", strategy_name: "EMA Crossover Strategy", instrument_id: "BTCUSDT.BINANCE",
    bar_type: "BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL", timeframe: "1m", parameters: {}, execution_profile: "conservative",
    run_config_hash: "h".repeat(64), status: "COMPLETED", attempt_count: 1,
    results: {
      metrics: {
        total_return: 0.0452, cagr: null, sharpe_ratio: 2.14, sortino_ratio: 3.05, max_drawdown: 0.012, profit_factor: 2.45,
        win_rate: 0.667, total_trades: 6, winning_trades: 4, losing_trades: 2, avg_trade_pnl: 75.33, max_consecutive_losses: 1,
        metrics_version: 3, benchmark: null, fees_paid: 12.5, turnover: 3.4,
        ...(regimeBreakdown === undefined ? {} : { regime_breakdown: regimeBreakdown }),
      },
      downsampled_equity: [], monthly_returns: {}, catalog_manifest: [], artifact_manifest: {},
    },
  };
}

async function mockResearch(page: Page, opts: { regimeBreakdown?: unknown; liveVsBacktest?: unknown }) {
  await page.route("**/api/v1/bars*", (route) => route.fulfill({ json: { symbol: "BTCUSDT", timeframe: "1m", bars: [], count: 0 } }));
  await page.route("**/api/v1/scanners*", (route) => route.fulfill({ json: [] }));
  await page.route("**/api/v1/signals*", (route) => route.fulfill({ json: { signals: [], total: 0 } }));
  await page.route("**/api/v1/strategies", (route) =>
    route.fulfill({
      json: [{
        slug: "ema-cross", name: "EMA Crossover Strategy", description: "EMA cross", author: "Cryptonite Core", version_num: 1,
        default_parameters: { fast_period: 9 }, parameter_schema: { fast_period: { type: "integer", default: 9, title: "Fast EMA Period" } },
        code_hash: "c".repeat(64),
      }],
    }),
  );
  const run = backtestRun(opts.regimeBreakdown);
  await page.route(/\/api\/v1\/backtests/, (route) => {
    const url = route.request().url();
    if (url.includes(`/${RUN_ID}`)) return route.fulfill({ json: run });
    if (route.request().method() === "POST") return route.fulfill({ json: { id: RUN_ID, status: "PENDING" } });
    return route.fulfill({ json: [run] });
  });
  await page.route("**/api/v1/research/live-vs-backtest", (route) => route.fulfill({ json: opts.liveVsBacktest ?? { available: true, instances: [], without_backtest: [] } }));
}

async function runBacktest(page: Page) {
  await page.goto("/");
  await page.getByRole("button", { name: "Research Lab" }).click();
  await page.getByRole("button", { name: "Run Quantitative Backtest" }).click();
  await expect(page.getByText("+4.52%")).toBeVisible({ timeout: 10000 });
}

test.describe("Phase 8 reporting: Research Lab", () => {
  test("tearsheet shows trades by regime at entry", async ({ page }) => {
    await mockResearch(page, {
      regimeBreakdown: {
        UNKNOWN: { trades: 1, win_rate: 1, total_pnl: 4.2 },
        TREND_UP: { trades: 4, win_rate: 0.75, total_pnl: 310.5 },
        CHOP: { trades: 1, win_rate: 0, total_pnl: -12 },
      },
    });
    await runBacktest(page);
    const table = page.getByRole("table", { name: "Trades by market regime" });
    await expect(table.getByRole("row")).toHaveCount(4);
    await expect(table.getByRole("row").nth(1)).toContainText("Trend up");
    await expect(page.getByTestId("regime-row-TREND_UP")).toContainText("75.0%");
    await expect(page.getByTestId("regime-row-TREND_UP")).toContainText("+$310.50");
    await expect(page.getByTestId("regime-row-CHOP")).toContainText("-$12.00");
    await expect(page.getByTestId("regime-row-UNKNOWN")).toContainText("Unknown");
  });

  test("tearsheet shows a dash for runs without a regime breakdown", async ({ page }) => {
    await mockResearch(page, {});
    await runBacktest(page);
    await expect(page.getByTestId("regime-breakdown")).toContainText("—");
    await expect(page.getByRole("table", { name: "Trades by market regime" })).toHaveCount(0);
  });

  test("live vs backtest panel compares a running instance with its backtest", async ({ page }) => {
    await mockResearch(page, {
      liveVsBacktest: {
        available: true,
        without_backtest: [],
        instances: [{
          instance_id: "EMA-BTC-1", strategy_name: "EmaCross", instrument_id: "BTCUSDT.BINANCE", deployed_at: "2026-09-01T00:00:00+00:00",
          live: { trades: 7, win_rate: 0.428571, mean_return_per_trade: 0.0012, fees: 3.5, slippage_bps: null },
          backtest: { run_id: "0b1c2d3e-0000-4000-8000-000000000000", execution_profile: "conservative", trades: 40, win_rate: 0.55,
                      mean_return_per_trade: 0.0031, fees: 12.5, slippage_bps: 1.25 },
        }],
      },
    });
    await page.goto("/");
    await page.getByRole("button", { name: "Research Lab" }).click();
    await page.getByRole("button", { name: "Live vs Backtest" }).click();
    const table = page.getByRole("table", { name: "Live vs backtest for EMA-BTC-1" });
    await expect(table).toBeVisible();
    await expect(table.getByRole("row", { name: /Win rate/ })).toContainText("42.9%");
    await expect(table.getByRole("row", { name: /Win rate/ })).toContainText("55.0%");
    await expect(table.getByRole("row", { name: /Trades/ })).toContainText("40");
    await expect(table.getByRole("row", { name: /Slippage/ })).toContainText("—");
    await expect(table.getByRole("row", { name: /Slippage/ })).toContainText("1.25");
    await expect(page.getByTestId("lvb-EMA-BTC-1")).toContainText("deployed 2026-09-01");
  });

  test("live vs backtest panel without instances", async ({ page }) => {
    await mockResearch(page, {});
    await page.goto("/");
    await page.getByRole("button", { name: "Research Lab" }).click();
    await page.getByRole("button", { name: "Live vs Backtest" }).click();
    await expect(page.getByTestId("live-vs-backtest-empty")).toHaveText("No running strategy instances.");
  });
});
