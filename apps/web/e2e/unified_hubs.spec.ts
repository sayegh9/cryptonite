import { test, expect } from "./fixtures/offline";

const MOCK_ACCOUNT = {
  account_id: "PAPER-ACCOUNT-001",
  venue: "BINANCE",
  currency: "USDT",
  starting_balance: 100000,
  cash_balance: 85000,
  total_equity: 104250,
  realized_pnl: 450,
  daily_realized_pnl: 450,
  daily_loss_latched: false,
};

const MOCK_POSITION_ENRICHED = {
  position_id: "pos-rsi-01",
  account_id: "PAPER-ACCOUNT-001",
  instance_id: "inst-rsi-01",
  instrument_id: "BTCUSDT.BINANCE",
  side: "LONG" as const,
  entry_price: 68000.0,
  quantity: 0.5,
  current_price: 69000.0,
  unrealized_pnl: 500.0,
  status: "OPEN" as const,
  opened_at: new Date(Date.now() - 3600 * 1000).toISOString(),
  strategy_name: "RSI Oversold Reversal",
  duration_s: 3600,
  stop_loss_price: 66000,
  take_profit_price: 72000,
  leverage: 5,
  margin_mode: "CROSS" as const,
  liquidation_price: 55000,
};

const MOCK_STRATEGY = {
  slug: "ema-crossover",
  name: "EMA Crossover Strategy",
  description: "Trend following momentum strategy",
  thesis: "Trend following strategy using exponential moving averages to capture momentum breakout regimes",
  category: "trend_following",
  market_regime: "Trending",
  timeframe_default: "15m",
  author: "Cryptonite Core",
  version_num: 1,
  active_version_id: "v1-active",
  code_hash: "a1b2c3d4e5f67890",
  default_parameters: { fast_period: 9, slow_period: 21 },
  parameter_schema: {},
};

const MOCK_MANDATE = {
  id: "11111111-1111-4111-8111-111111111111",
  name: "Institutional Momentum Alpha",
  capital_budget_usd: "50000",
  instruments: ["BTCUSDT.BINANCE", "ETHUSDT.BINANCE"],
  max_position_pct: "10",
  max_drawdown_pct: "15",
  allow_perps: true,
  max_leverage: 3,
  strategies: [],
  autonomy_level: 2,
  level_name: "L2 Supervised",
  status: "ACTIVE",
  active_hours_utc: null,
  veto_window_s: 300,
  halted_reason: null,
  halted_at: null,
  level_since: "2026-09-01T00:00:00Z",
  equity: "52400",
  hwm: "53000",
  drawdown_pct: "1.13",
  exposure_usd: "15000",
  budget_used_pct: "30",
  positions: [],
  pending_approvals: 0,
  pending_proposal: null,
};

test.describe("Unified Hubs & 4-Pillar Institutional Navigation", () => {
  test.beforeEach(async ({ page }) => {
    // Avoid onboarding modal interfering with interaction
    await page.addInitScript(() => {
      localStorage.setItem("cryptonite:ai-brain-tour-seen", "true");
    });

    // Default mock for operator authentication
    await page.route("**/api/v1/auth/me", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ authenticated: true, username: "operator", csrf_token: "csrf-test" }),
      })
    );

    // Default mock for kill switch status
    await page.route("**/api/v1/paper/kill-switch*", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ id: 1, is_latched: false }),
      })
    );

    await page.route("**/api/v1/control/kill-switch/status*", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          environment: "PAPER",
          account_id: "PAPER-ACCOUNT-001",
          is_latched: false,
          latch_generation: 1,
        }),
      })
    );

    // Default mock for paper orders and trades
    await page.route("**/api/v1/paper/orders*", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) })
    );
    await page.route("**/api/v1/paper/trades*", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) })
    );
    await page.route("**/api/v1/paper/strategies*", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) })
    );

    // Default instruments catalog
    await page.route("**/api/v1/instruments*", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          { id: "BTCUSDT.BINANCE", symbol: "BTCUSDT", exchange: "BINANCE" },
          { id: "ETHUSDT.BINANCE", symbol: "ETHUSDT", exchange: "BINANCE" },
        ]),
      })
    );
  });

  test("Pillar 1: Trades & Execution Central renders active positions, timers, gauges and zero-dummy state", async ({
    page,
  }) => {
    // 1. Stub paper accounts and enriched positions
    await page.route("**/api/v1/paper/accounts*", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([MOCK_ACCOUNT]),
      })
    );

    await page.route("**/api/v1/paper/positions*", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([MOCK_POSITION_ENRICHED]),
      })
    );

    // Navigate to Trades hub
    await page.goto("/?ws=trades");

    // Verify Header title
    await expect(page.getByRole("heading", { name: "Trades & Execution Central" })).toBeVisible();
    await expect(page.getByText("PAPER ENGINE")).toBeVisible();

    // Verify Active Position shows enriched strategy name
    await expect(page.getByRole("button", { name: /RSI Oversold Reversal/ })).toBeVisible();

    // Verify DurationTimer renders elapsed duration
    const durationTimer = page.locator("time[aria-label*='Position active duration']");
    await expect(durationTimer).toBeVisible();
    await expect(durationTimer).toContainText(/1h/);

    // Verify PositionLifecycleGauge renders bracket targets
    await expect(page.getByRole("button", { name: /Stop Loss: \$66,000/ })).toBeVisible();
    await expect(page.getByRole("button", { name: /Take Profit: \$72,000/ })).toBeVisible();
    await expect(page.getByText("To Stop Loss:")).toBeVisible();
    await expect(page.getByText("To Take Profit:")).toBeVisible();

    // Verify zero-dummy empty state when positions list is empty
    await page.route("**/api/v1/paper/positions*", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([]),
      })
    );

    // Trigger fresh page load with empty positions
    await page.goto("/?ws=trades");
    await expect(
      page.getByText("No active positions currently held on Paper Account.")
    ).toBeVisible();
    await expect(page.getByRole("button", { name: "Open Strategy Studio" })).toBeVisible();
  });

  test("Pillar 2: Strategy Lifecycle Studio renders cards, metadata, and opens StrategyInspectorDrawer", async ({
    page,
  }) => {
    // 1. Stub strategies API with rich metadata
    await page.route("**/api/v1/strategies*", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([MOCK_STRATEGY]),
      })
    );

    await page.route("**/api/v1/paper/accounts*", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([MOCK_ACCOUNT]),
      })
    );

    await page.route("**/api/v1/paper/positions*", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([]),
      })
    );

    // Navigate to Strategy Studio hub
    await page.goto("/?ws=strategies");

    // Verify Strategy Card renders name, regime pill, and thesis snippet
    await expect(page.getByText("EMA Crossover Strategy").first()).toBeVisible();
    await expect(page.getByText("Trending", { exact: true })).toBeVisible();
    await expect(page.getByText(/Trend following strategy using exponential moving averages/)).toBeVisible();

    // Click the strategy card to open StrategyInspectorDrawer
    await page.getByText("EMA Crossover Strategy").first().click();

    // Verify drawer dialog opens
    const drawer = page.getByRole("dialog", { name: /Strategy Inspector: EMA Crossover Strategy/ });
    await expect(drawer).toBeVisible();

    // Verify the drawer contains Overview, Backtest, and Live Instances tabs
    await expect(drawer.getByRole("button", { name: /Overview & Rationale/ })).toBeVisible();
    await expect(drawer.getByRole("button", { name: /Quantitative Backtest & Tearsheet/ })).toBeVisible();
    await expect(drawer.getByRole("button", { name: /Live Paper Instances/ })).toBeVisible();

    // Close the drawer
    await drawer.getByRole("button", { name: "Close strategy inspector" }).click();
    await expect(drawer).not.toBeVisible();
  });

  test("Pillar 4: Risk & Governance Flight Deck renders master emergency bar and subview tabs", async ({
    page,
  }) => {
    // 1. Stub mandates and kill switches
    await page.route("**/api/v1/mandates*", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([MOCK_MANDATE]),
      })
    );

    await page.route("**/api/v1/paper/accounts*", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([MOCK_ACCOUNT]),
      })
    );

    await page.route("**/api/v1/paper/positions*", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([]),
      })
    );

    // Navigate to Risk & Governance Flight Deck
    await page.goto("/?ws=governance");

    // Verify master emergency top bar
    const emergencyHeader = page.getByRole("banner", { name: "Master Emergency Top Bar" });
    await expect(emergencyHeader).toBeVisible();
    await expect(emergencyHeader.getByRole("heading", { name: "Governance Flight Deck" })).toBeVisible();
    await expect(page.getByText("ARMED · EXECUTION NOMINAL")).toBeVisible();

    // Verify Subview Navigation Tabs: Overview, Mandates, Approvals, Limits, Reconciliation, Autonomy
    const tablist = page.getByRole("tablist", { name: "Governance flight deck views" });
    await expect(tablist).toBeVisible();
    await expect(tablist.getByRole("tab", { name: "Overview" })).toBeVisible();
    await expect(tablist.getByRole("tab", { name: "Mandates" })).toBeVisible();
    await expect(tablist.getByRole("tab", { name: "Approvals" })).toBeVisible();
    await expect(tablist.getByRole("tab", { name: "Risk Limits" })).toBeVisible();
    await expect(tablist.getByRole("tab", { name: "Reconciliation" })).toBeVisible();
    await expect(tablist.getByRole("tab", { name: "Autonomy & Evals" })).toBeVisible();

    // Click Mandates tab and verify active selection
    await tablist.getByRole("tab", { name: "Mandates" }).click();
    await expect(tablist.getByRole("tab", { name: "Mandates" })).toHaveAttribute("aria-selected", "true");
    await expect(page).toHaveURL(/ws=governance&view=mandates/);
  });

  test("Option A 4-Pillar Header Navigation: jumps between all 4 hubs and highlights active pillar", async ({
    page,
  }) => {
    // Stub baseline APIs
    await page.route("**/api/v1/paper/accounts*", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([MOCK_ACCOUNT]) })
    );
    await page.route("**/api/v1/paper/positions*", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) })
    );
    await page.route("**/api/v1/strategies*", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([MOCK_STRATEGY]) })
    );
    await page.route("**/api/v1/mandates*", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([MOCK_MANDATE]) })
    );

    // 1. Start on Trades & Execution (Pillar 1)
    await page.goto("/?ws=trades");
    await expect(page).toHaveURL(/\?ws=trades$/);
    const tradesPillar = page.locator("span[data-pillar='Trades']");
    await expect(tradesPillar).toBeVisible();
    await expect(tradesPillar).toHaveAttribute("data-active", "true");

    // 2. Jump to Strategy Studio (Pillar 2)
    await page.locator("span[data-pillar='Strategies']").click();
    await expect(page).toHaveURL(/\?ws=strategies$/);
    const strategiesPillar = page.locator("span[data-pillar='Strategies']");
    await expect(strategiesPillar).toHaveAttribute("data-active", "true");
    await expect(tradesPillar).toHaveAttribute("data-active", "false");

    // 3. Jump to AI Brain (Pillar 3)
    await page.locator("span[data-pillar='AI Brain']").click();
    await expect(page).toHaveURL(/ws=brain/);
    const brainPillar = page.locator("span[data-pillar='AI Brain']");
    await expect(brainPillar).toHaveAttribute("data-active", "true");
    await expect(strategiesPillar).toHaveAttribute("data-active", "false");

    // 4. Jump to Risk & Governance (Pillar 4)
    await page.locator("span[data-pillar='Risk & Governance']").click();
    await expect(page).toHaveURL(/ws=governance/);
    const govPillar = page.locator("span[data-pillar='Risk & Governance']");
    await expect(govPillar).toHaveAttribute("data-active", "true");
    await expect(brainPillar).toHaveAttribute("data-active", "false");

    // 5. Jump back to Trades & Execution
    await tradesPillar.click();
    await expect(page).toHaveURL(/\?ws=trades$/);
    await expect(tradesPillar).toHaveAttribute("data-active", "true");
    await expect(govPillar).toHaveAttribute("data-active", "false");
  });
});
