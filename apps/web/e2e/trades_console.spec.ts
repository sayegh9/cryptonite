import { test, expect } from "@playwright/test";

test.describe("Trades console E2E", () => {
  test.beforeEach(async ({ page }) => {
    // 1. Mock market bars API for terminal
    await page.route("**/api/bars*", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          symbol: "BTCUSDT",
          timeframe: "1m",
          bars: [],
          count: 0,
        }),
      });
    });

    // 2. Mock scanners and signals API
    await page.route("**/api/scanners*", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([]),
      });
    });

    await page.route("**/api/signals*", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ signals: [], total: 0 }),
      });
    });

    // 3. Mock strategies registry API
    await page.route("**/api/v1/strategies*", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([]),
      });
    });

    // 4. Default paper mocks
    await page.route("**/api/v1/paper/accounts*", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          {
            account_id: "PAPER-ACCOUNT-001",
            venue: "BINANCE",
            currency: "USDT",
            starting_balance: 10000,
            cash_balance: 8500,
            total_equity: 10150,
            realized_pnl: 150,
            daily_realized_pnl: 150,
            daily_loss_latched: false,
          },
        ]),
      });
    });

    await page.route("**/api/v1/paper/positions*", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          {
            position_id: "pos-001",
            account_id: "PAPER-ACCOUNT-001",
            instance_id: "INST-EMA-01",
            instrument_id: "BTCUSDT.BINANCE",
            side: "LONG",
            entry_price: 50000.0,
            quantity: 0.03,
            current_price: 55000.0,
            unrealized_pnl: 150.0,
            status: "OPEN",
          },
        ]),
      });
    });

    await page.route("**/api/v1/paper/orders*", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([]),
      });
    });

    await page.route("**/api/v1/paper/strategies*", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          {
            instance_id: "INST-EMA-01",
            strategy_name: "EMACrossover",
            instrument_id: "BTCUSDT.BINANCE",
            timeframe: "1m",
            allocation_usd: 5000,
            status: "RUNNING",
            last_processed_bar_ns: 1725700000000000000,
          },
        ]),
      });
    });

    await page.route("**/api/v1/paper/kill-switch*", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ id: 1, is_latched: false }),
      });
    });

    await page.route("**/api/v1/paper/trades*", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([]),
      });
    });
  });

  test("keeps positions on screen when a poll fails (no flicker)", async ({ page }) => {
    let calls = 0;
    await page.route("**/api/v1/paper/positions", async (route) => {
      calls += 1;
      // Every poll after the first fails, as with a keep-alive ECONNRESET through the proxy
      if (calls > 1) return route.abort("connectionreset");
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          {
            position_id: "pos-eth-01",
            account_id: "PAPER-ACCOUNT-001",
            instance_id: "INST-EMA-01",
            instrument_id: "ETHUSDT.BINANCE",
            side: "LONG",
            entry_price: 3000.0,
            quantity: 1,
            current_price: 3100.0,
            unrealized_pnl: 100.0,
            status: "OPEN",
          },
        ]),
      });
    });

    await page.goto("/");
    await page.getByRole("button", { name: "Portfolio", exact: true }).click();
    const row = page.locator("td:has-text('ETHUSDT.BINANCE')");
    await expect(row).toBeVisible();

    // Sample across two failed polls: the row must never disappear
    await expect.poll(() => calls, { timeout: 10_000 }).toBeGreaterThanOrEqual(3);
    for (let i = 0; i < 10; i++) {
      await expect(row).toBeVisible({ timeout: 50 });
      await page.waitForTimeout(100);
    }
    await expect(page.locator("text=No open positions.")).toHaveCount(0);
  });

  test("cancels resting orders from the execution orders journal", async ({ page }) => {
    let orderCancelled = false;

    await page.route("**/api/v1/paper/accounts", async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) });
    });
    await page.route("**/api/v1/paper/positions", async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) });
    });
    await page.route("**/api/v1/paper/strategies", async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) });
    });
    await page.route("**/api/v1/paper/kill-switch", async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ id: 1, is_latched: false }) });
    });

    await page.route("**/api/v1/paper/orders", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          {
            client_order_id: "ORD-TEST-001",
            instance_id: "INST-EMA-01",
            instrument_id: "BTCUSDT.BINANCE",
            side: "BUY",
            order_type: "LIMIT",
            quantity: 0.05,
            price: 52000.0,
            filled_qty: 0.0,
            avg_fill_price: null,
            status: orderCancelled ? "CANCELED" : "SUBMITTED",
            created_at: new Date().toISOString(),
          },
        ]),
      });
    });

    let cancelEndpointCalled = false;
    await page.route("**/api/v1/paper/orders/ORD-TEST-001/cancel", async (route) => {
      cancelEndpointCalled = true;
      orderCancelled = true;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ command_id: "cmd-cancel-01", status: "PENDING" }),
      });
    });

    await page.goto("/");
    await page.locator("button:has-text('Portfolio')").click();

    await expect(page.locator("text=ORD-TEST-001")).toBeVisible();
    await expect(page.locator("text=SUBMITTED")).toBeVisible();

    // Click Cancel
    const cancelBtn = page.locator("button:has-text('Cancel')");
    await expect(cancelBtn).toBeVisible();
    await cancelBtn.click();
    expect(cancelEndpointCalled).toBe(true);
  });

});
