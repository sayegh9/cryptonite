import { test, expect } from "@playwright/test";

test.describe("Portfolio & Paper Execution Console E2E", () => {
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
  });

  test("navigates to portfolio console and displays core metrics cards", async ({ page }) => {
    // Mock paper trading endpoints
    await page.route("**/api/v1/paper/accounts", async (route) => {
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

    await page.route("**/api/v1/paper/positions", async (route) => {
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

    await page.route("**/api/v1/paper/orders", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([]),
      });
    });

    await page.route("**/api/v1/paper/strategies", async (route) => {
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

    await page.route("**/api/v1/paper/kill-switch", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ id: 1, is_latched: false }),
      });
    });

    await page.goto("/");
    await page.waitForLoadState("domcontentloaded");

    // Click Portfolio tab in header
    const portfolioTab = page.locator("button:has-text('Portfolio')");
    await expect(portfolioTab).toBeVisible();
    await portfolioTab.click();

    // Verify header title
    await expect(page.locator("text=PORTFOLIO & PAPER EXECUTION CONSOLE")).toBeVisible();
    await expect(page.locator("text=M4 SIMULATOR")).toBeVisible();

    // Verify Metric Cards
    await expect(page.locator("text=Total Net Equity")).toBeVisible();
    await expect(page.locator("text=$10,150.00")).toBeVisible();

    await expect(page.locator("text=Available Cash")).toBeVisible();
    await expect(page.locator("text=$8,500.00")).toBeVisible();

    await expect(page.locator("text=Daily Realized PnL")).toBeVisible();
    await expect(page.locator("div").filter({ hasText: /^\+\$150\.00$/ })).toBeVisible();

    await expect(page.locator("text=Open Positions")).toBeVisible();
    await expect(page.locator("text=1 Active")).toBeVisible();

    await expect(page.locator("text=Execution Guard")).toBeVisible();
    await expect(page.getByText("ACTIVE", { exact: true })).toBeVisible();
  });

  test("displays active positions and executes flatten action", async ({ page }) => {
    let positionClosed = false;

    await page.route("**/api/v1/paper/accounts", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([]),
      });
    });

    await page.route("**/api/v1/paper/positions", async (route) => {
      if (positionClosed) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify([]),
        });
      } else {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify([
            {
              position_id: "pos-btc-01",
              account_id: "PAPER-ACCOUNT-001",
              instance_id: "INST-EMA-01",
              instrument_id: "BTCUSDT.BINANCE",
              side: "LONG",
              entry_price: 50000.0,
              quantity: 0.03,
              current_price: 52000.0,
              unrealized_pnl: 60.0,
              status: "OPEN",
            },
          ]),
        });
      }
    });

    await page.route("**/api/v1/paper/orders", async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) });
    });

    await page.route("**/api/v1/paper/strategies", async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) });
    });

    await page.route("**/api/v1/paper/kill-switch", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ id: 1, is_latched: false }),
      });
    });

    // Handle close-position command
    let closePayload: any = null;
    await page.route("**/api/v1/paper/strategies/INST-EMA-01/close-position", async (route) => {
      closePayload = route.request().postDataJSON();
      positionClosed = true;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ command_id: "cmd-close-01", status: "PENDING" }),
      });
    });

    await page.goto("/");
    await page.locator("button:has-text('Portfolio')").click();

    // Verify position is rendered
    await expect(page.locator("td:has-text('BTCUSDT.BINANCE')")).toBeVisible();
    await expect(page.locator("td:has-text('LONG')")).toBeVisible();
    await expect(page.locator("td:has-text('$50000.00')")).toBeVisible();

    // Click Flatten button
    const flattenBtn = page.locator("button:has-text('Flatten')");
    await expect(flattenBtn).toBeVisible();
    await flattenBtn.click();

    // Verify close-position was dispatched
    expect(closePayload).toBeDefined();
    expect(closePayload.instrument_id).toBe("BTCUSDT.BINANCE");
  });

  test("manages strategy instances with pause, resume, and stop controls", async ({ page }) => {
    let stratStatus: "RUNNING" | "PAUSED" | "STOPPED" = "RUNNING";

    await page.route("**/api/v1/paper/accounts", async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) });
    });
    await page.route("**/api/v1/paper/positions", async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) });
    });
    await page.route("**/api/v1/paper/orders", async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) });
    });
    await page.route("**/api/v1/paper/kill-switch", async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ id: 1, is_latched: false }) });
    });

    await page.route("**/api/v1/paper/strategies", async (route) => {
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
            status: stratStatus,
            last_processed_bar_ns: 1725700000000000000,
          },
        ]),
      });
    });

    let pauseCalled = false;
    let resumeCalled = false;
    let stopCalled = false;

    await page.route("**/api/v1/paper/strategies/INST-EMA-01/pause", async (route) => {
      pauseCalled = true;
      stratStatus = "PAUSED";
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ command_id: "cmd-pause-01", status: "PENDING" }),
      });
    });

    await page.route("**/api/v1/paper/strategies/INST-EMA-01/resume", async (route) => {
      resumeCalled = true;
      stratStatus = "RUNNING";
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ command_id: "cmd-resume-01", status: "PENDING" }),
      });
    });

    await page.route("**/api/v1/paper/strategies/INST-EMA-01/stop", async (route) => {
      stopCalled = true;
      stratStatus = "STOPPED";
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ command_id: "cmd-stop-01", status: "PENDING" }),
      });
    });

    await page.goto("/");
    await page.locator("button:has-text('Portfolio')").click();

    await expect(page.locator("text=INST-EMA-01")).toBeVisible();
    await expect(page.locator("text=RUNNING")).toBeVisible();

    // Click Pause
    const pauseBtn = page.locator("button[title='Pause']");
    await expect(pauseBtn).toBeVisible();
    await pauseBtn.click();
    expect(pauseCalled).toBe(true);

    // After pause, button should become Resume
    const resumeBtn = page.locator("button[title='Resume']");
    await expect(resumeBtn).toBeVisible();
    await resumeBtn.click();
    expect(resumeCalled).toBe(true);

    // Click Stop
    const stopBtn = page.locator("button[title='Stop']");
    await expect(stopBtn).toBeVisible();
    await stopBtn.click();
    expect(stopCalled).toBe(true);
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

  test("opens deploy strategy modal and submits deployment command", async ({ page }) => {
    await page.route("**/api/v1/paper/accounts", async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) });
    });
    await page.route("**/api/v1/paper/positions", async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) });
    });
    await page.route("**/api/v1/paper/orders", async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) });
    });
    await page.route("**/api/v1/paper/strategies", async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) });
    });
    await page.route("**/api/v1/paper/kill-switch", async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ id: 1, is_latched: false }) });
    });

    let deployPayload: any = null;
    await page.route("**/api/v1/paper/strategies/deploy", async (route) => {
      deployPayload = route.request().postDataJSON();
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ command_id: "cmd-deploy-01", status: "PENDING" }),
      });
    });

    await page.goto("/");
    await page.locator("button:has-text('Portfolio')").click();

    // Click Deploy Strategy button in header
    const deployBtn = page.locator("button:has-text('Deploy Strategy')");
    await expect(deployBtn).toBeVisible();
    await deployBtn.click();

    // Verify modal appears
    await expect(page.locator("text=Deploy Strategy to Paper Engine")).toBeVisible();

    // Form inputs
    const submitBtn = page.locator("button[type='submit']:has-text('Deploy')");
    await expect(submitBtn).toBeVisible();
    await submitBtn.click();

    // Check payload submitted
    expect(deployPayload).toBeDefined();
    expect(deployPayload.strategy_name).toBe("EMACrossover");
    expect(deployPayload.instrument_id).toBe("BTCUSDT.BINANCE");
    expect(deployPayload.allocation_usd).toBe(5000);
  });

  test("handles emergency kill switch engagement and audited reset", async ({ page }) => {
    let latchedState = false;

    await page.route("**/api/v1/paper/accounts", async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) });
    });
    await page.route("**/api/v1/paper/positions", async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) });
    });
    await page.route("**/api/v1/paper/orders", async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) });
    });
    await page.route("**/api/v1/paper/strategies", async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) });
    });

    await page.route("**/api/v1/paper/kill-switch", async (route) => {
      if (route.request().method() === "POST") {
        latchedState = true;
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ command_id: "cmd-trip-01", status: "PENDING" }),
        });
      } else {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            id: 1,
            is_latched: latchedState,
            reason: latchedState ? "Test emergency halt" : undefined,
            latched_by: latchedState ? "web-ui" : undefined,
          }),
        });
      }
    });

    let resetCalled = false;
    await page.route("**/api/v1/paper/kill-switch/reset", async (route) => {
      resetCalled = true;
      latchedState = false;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ command_id: "cmd-reset-01", status: "PENDING" }),
      });
    });

    await page.goto("/");
    await page.locator("button:has-text('Portfolio')").click();

    // Verify initial active state
    await expect(page.locator("text=Trip Kill Switch")).toBeVisible();

    // Open Kill Switch modal
    await page.locator("button:has-text('Trip Kill Switch')").click();
    await expect(page.locator("text=CONFIRM EMERGENCY KILL SWITCH")).toBeVisible();

    // Enter reason and confirm
    const reasonInput = page.locator("input[value='Manual operator trigger']");
    await reasonInput.fill("Test emergency halt");
    await page.locator("button:has-text('ENGAGE KILL SWITCH')").click();

    // Verify emergency banner appears
    await expect(page.locator("text=EMERGENCY LATCH ENGAGED — EXECUTION HALTED")).toBeVisible();
    await expect(page.locator("text=Reset Switch")).toBeVisible();

    // Click Reset Switch
    await page.locator("button:has-text('Reset Switch')").click();
    expect(resetCalled).toBe(true);

    // Verify banner disappears after reset
    await expect(page.locator("text=EMERGENCY LATCH ENGAGED — EXECUTION HALTED")).not.toBeVisible();
  });
});
