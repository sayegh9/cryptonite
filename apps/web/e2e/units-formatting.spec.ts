import { test, expect, type Page } from "@playwright/test";

/**
 * G4 units and formatting in the UI, with the API stubbed: Brain rail quantities to step size and
 * FLAT rows hidden, Portfolio FLAT perp rows hidden, gate drawdown in %, Sortino "n/a (0 closed
 * trades)" instead of "inf", and a greyed-out calibration chart below 20 points.
 */

const M1 = "11111111-1111-4111-8111-111111111111";

const MANDATE = {
  id: M1, name: "Alt-momentum", capital_budget_usd: "10000", instruments: ["BTCUSDT.BINANCE"], max_position_pct: "10",
  max_drawdown_pct: "20", allow_perps: false, max_leverage: 1, strategies: [], autonomy_level: 1, level_name: "L1 Copilot",
  status: "ACTIVE", active_hours_utc: null, veto_window_s: 600, halted_reason: null, halted_at: null,
  level_since: "2026-10-01T00:00:00Z", equity: "10000", hwm: "10019", drawdown_pct: "0.1933", exposure_usd: "94",
  budget_used_pct: "0.94", positions: [], pending_approvals: 0, pending_proposal: null,
};

const POSITIONS = [
  { position_id: "p1", account_id: "PAPER-ACCOUNT-001", instance_id: "AI_MANDATE_1", instrument_id: "BTCUSDT.BINANCE", side: "LONG",
    entry_price: 60000, quantity: "0.009399821403393336", current_price: 60100, unrealized_pnl: "0.94", status: "OPEN" },
  { position_id: "p2", account_id: "PAPER-ACCOUNT-001", instance_id: "AI_MANDATE_1", instrument_id: "ETHUSDT-PERP.BINANCE", side: "FLAT",
    entry_price: 0, quantity: "0E-18", current_price: 3000, unrealized_pnl: "0", status: "OPEN", leverage: 2, margin_mode: "CROSS" },
  { position_id: "p3", account_id: "PAPER-ACCOUNT-001", instance_id: "AI_MANDATE_1", instrument_id: "SOLUSDT-PERP.BINANCE", side: "LONG",
    entry_price: 150, quantity: "0E-18", current_price: 151, unrealized_pnl: "0", status: "OPEN", leverage: 2, margin_mode: "CROSS" },
  // dust below the 0.0001 step: still held, shown as "<0.0001" in the rail, never hidden or "0"
  { position_id: "p5", account_id: "PAPER-ACCOUNT-001", instance_id: "AI_MANDATE_1", instrument_id: "ETHUSDT.BINANCE", side: "LONG",
    entry_price: 3000, quantity: "0.00004", current_price: 3000, unrealized_pnl: "0", status: "OPEN" },
  { position_id: "p4", account_id: "PAPER-ACCOUNT-001", instance_id: "AI_MANDATE_1", instrument_id: "XRPUSDT.BINANCE", side: "LONG",
    entry_price: 0.5, quantity: "120", current_price: 0.5, unrealized_pnl: "0", status: "CLOSED" },
];

const INSTRUMENTS = [
  { instrument_id: "BTCUSDT.BINANCE", symbol: "BTCUSDT", venue: "BINANCE", market_type: "SPOT", price_precision: 2, size_precision: 5,
    price_increment: "0.01", size_increment: "0.00001000", status: "TRADING", is_active: true },
  { instrument_id: "ETHUSDT.BINANCE", symbol: "ETHUSDT", venue: "BINANCE", market_type: "SPOT", price_precision: 2, size_precision: 4,
    price_increment: "0.01", size_increment: "0.0001", status: "TRADING", is_active: true },
];

async function stubCommon(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem("cryptonite:ai-brain-tour-seen", "true");
    localStorage.setItem("csrf_token", "csrf-test");
  });
  // Anything not stubbed below fails fast instead of hanging on a half-open :8000
  await page.route("**/api/v1/**", (route) => route.fulfill({ status: 503, json: { detail: "stubbed offline" } }));
  await page.route("**/agent-api/**", (route) => route.fulfill({ status: 503, json: { detail: "stubbed offline" } }));
  await page.route("**/api/v1/paper/positions*", (route) => route.fulfill({ json: POSITIONS }));
  await page.route("**/api/v1/instruments", (route) => route.fulfill({ json: INSTRUMENTS }));
  await page.route("**/api/v1/ai/mandates", (route) => route.fulfill({ json: { mandates: [MANDATE] } }));
  await page.route("**/api/v1/ai/status", (route) =>
    route.fulfill({ json: { kill_switch: { latched: false, reason: null, latched_by: null }, halted_mandates: [], account: null,
      exit_monitor_up: true, notifier_up: true, budget_alert: false, degraded: [] } }),
  );
  await page.route(/\/api\/v1\/ai\/approvals\?/, (route) => route.fulfill({ json: { now: new Date().toISOString(), approvals: [] } }));
}

test.describe("Units and formatting (G4)", () => {
  test("Brain rail: quantity to step size, FLAT and closed rows hidden", async ({ page }) => {
    await stubCommon(page);
    await page.route("**/agent-api/brain/spend", (route) => route.fulfill({ json: { spent_usd: "0.10", cap_usd: "25.00" } }));
    await page.route("**/agent-api/brain/sessions", (route) => route.fulfill({ json: { sessions: [] } }));
    await page.goto("/?ws=brain&view=home");
    const rail = page.getByTestId("brain-rail-positions");
    await expect(rail.getByRole("listitem")).toHaveCount(2);
    await expect(rail).toContainText("BTCUSDT.BINANCE");
    // truncated to the 0.00001 step (what the venue can close), never rounded up to 0.00940
    await expect(rail.getByRole("listitem").filter({ hasText: "BTCUSDT" }).getByTestId("brain-rail-qty")).toHaveText("0.00939");
    // dust below one step is still a position: shown as <step, not hidden and not "0"
    await expect(rail.getByRole("listitem").filter({ hasText: "ETHUSDT.BINANCE" }).getByTestId("brain-rail-qty")).toHaveText("<0.0001");
    await expect(rail).not.toContainText("0E-18");
    await expect(rail).not.toContainText("0.009399821403393336");
    await expect(rail).not.toContainText("ETHUSDT-PERP");
    await expect(rail).not.toContainText("XRPUSDT");
  });

  test("Portfolio: FLAT and zero-quantity perp rows are not active positions", async ({ page }) => {
    await stubCommon(page);
    await page.route("**/api/v1/paper/accounts*", (route) =>
      route.fulfill({ json: [{ account_id: "PAPER-ACCOUNT-001", venue: "BINANCE", currency: "USDT", starting_balance: 10000, cash_balance: 9900,
        total_equity: 10000, realized_pnl: 0, daily_realized_pnl: 0, daily_loss_latched: false }] }),
    );
    for (const p of ["orders", "trades", "strategies"]) {
      await page.route(`**/api/v1/paper/${p}*`, (route) => route.fulfill({ json: [] }));
    }
    await page.route("**/api/v1/paper/kill-switch*", (route) => route.fulfill({ json: { id: 1, is_latched: false } }));
    await page.goto("/?ws=portfolio");
    const table = page.getByRole("region", { name: "Active Positions" });
    await expect(table).toContainText("BTCUSDT.BINANCE");
    await expect(table).toContainText("0.009399"); // no step here: 4 significant digits, truncated
    await expect(table).toContainText("ETHUSDT.BINANCE"); // dust is still held, as in the Brain rail
    await expect(table).not.toContainText("ETHUSDT-PERP");
    await expect(table).not.toContainText("SOLUSDT-PERP");
    // the open-position count now lives in the Unrealized PnL metric card ("2L / 0S · 2 active")
    await expect(page.getByText(/· 2 active$/)).toBeVisible();
  });

  test("Autonomy gate: drawdown in %, Sortino n/a with 0 closed trades, calibration greyed below 20", async ({ page }) => {
    await stubCommon(page);
    await page.route("**/api/v1/ai/mandates/*/autonomy", (route) =>
      route.fulfill({
        json: {
          mandate_id: M1, name: "Alt-momentum", status: "ACTIVE", level: 1, level_name: "L1 Copilot", level_since: "2026-10-01T00:00:00Z",
          requested_level: null, proposal: null, events: [],
          gate: {
            level: 1, target_level: 2, passed: false,
            checks: [
              { name: "days_at_level", required: ">= 30 d", actual: "4.2 d", passed: false },
              { name: "closed_trades", required: ">= 30", actual: "0", passed: false },
              { name: "max_drawdown_pct", required: "<= 10%", actual: "0.1933", passed: true },
              { name: "mandate_breaches", required: "0", actual: "3", passed: false },
              { name: "sortino_vs_btc_hold", required: "> BTC hold (after fees)", actual: "inf vs not measured", passed: false },
            ],
            metrics: { closed_trades: 0, max_drawdown_pct: "0.1933", sortino: "inf", benchmark_sortino: null },
          },
        },
      }),
    );
    await page.route("**/api/v1/ai/outcomes/by-regime*", (route) => route.fulfill({ json: { horizon: "24h", mandate_id: M1, available: false, regimes: [] } }));
    const bins = [
      { lo: 0.4, hi: 0.6, n: 4, mean_confidence: 0.5, hit_rate: 0.25 },
      { lo: 0.6, hi: 0.8, n: 2, mean_confidence: 0.7, hit_rate: 1 },
    ];
    await page.route("**/api/v1/ai/evals", (route) =>
      route.fulfill({ json: { runs: [], calibration: { "debate.verdict": { n: 6, min_samples: 20, ece_pp: null, bins } }, calibration_note: "6 labelled decisions." } }),
    );
    await page.goto("/?ws=mission&view=autonomy");

    const gate = page.getByRole("table", { name: "Promotion gate checklist" });
    const row = (label: string) => gate.getByRole("row").filter({ hasText: label });
    await expect(row("Max drawdown")).toContainText("0.19%");
    await expect(row("Max drawdown")).not.toContainText("0.1933");
    await expect(row("Sortino vs BTC hold")).toContainText("n/a (0 closed trades) vs not measured");
    await expect(gate).not.toContainText("inf");

    const chart = page.getByTestId("reliability-debate.verdict");
    await expect(chart).toHaveAttribute("data-readable", "false");
    await expect(page.getByTestId("reliability-insufficient-debate.verdict")).toHaveText(
      "Too few labelled decisions to read: 6 of 20. Shown greyed out; the curve is noise until then.",
    );
    await expect(chart.getByRole("img")).toHaveAttribute("aria-label", /^Not enough data: Reliability of debate\.verdict/);
  });
});
