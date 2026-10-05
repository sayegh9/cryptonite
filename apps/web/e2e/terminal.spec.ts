import { test, expect } from "@playwright/test";

test.describe("Cryptonite Market Terminal & Polars Scanner E2E", () => {
  test.beforeEach(async ({ page }) => {
    // 1. Mock bars API
    await page.route("**/api/v1/bars*", async (route) => {
      const nowSec = Math.floor(Date.now() / 1000);
      const bars = [];
      for (let i = 60; i >= 0; i--) {
        const t = nowSec - i * 60;
        bars.push({
          time: t,
          open: "54000.00",
          high: "54200.00",
          low: "53950.00",
          close: "54150.00",
          volume: "12.5000",
          bar_open_ns: t * 1_000_000_000,
          bar_close_inclusive_ns: (t + 60) * 1_000_000_000 - 1_000_000,
          interval_end_exclusive_ns: (t + 60) * 1_000_000_000,
        });
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          symbol: "BTCUSDT",
          timeframe: "1m",
          bars,
          count: bars.length,
        }),
      });
    });

    // 2. Mock scanners specs API
    await page.route("**/api/v1/scanners", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          {
            id: "spec-rsi-01",
            slug: "rsi-oversold",
            name: "RSI Oversold Bounce",
            description: "Detects RSI < 30 on oversold dips with volume expansion",
            active_version_id: "ver-rsi-01",
            active_version: {
              id: "ver-rsi-01",
              spec_id: "spec-rsi-01",
              version_num: 1,
              spec_hash: "a1b2c3d4e5f6071829",
              universe: {
                instrument_ids: ["BTCUSDT.BINANCE", "ETHUSDT.BINANCE"],
                timeframes: ["1m", "5m"],
              },
              rules: [
                {
                  category: "trigger",
                  logic: "AND",
                  conditions: [{ field: "rsi", operator: "<", value: 30 }],
                },
              ],
            },
            is_archived: false,
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
          },
          {
            id: "spec-trend-02",
            slug: "trend-pullback",
            name: "Trend Pullback",
            description: "Trend following pullback to 50 EMA",
            active_version_id: "ver-trend-02",
            active_version: {
              id: "ver-trend-02",
              spec_id: "spec-trend-02",
              version_num: 1,
              spec_hash: "b2c3d4e5f607182930",
              universe: {
                instrument_ids: ["BTCUSDT.BINANCE"],
                timeframes: ["1h"],
              },
              rules: [],
            },
            is_archived: false,
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
          },
        ]),
      });
    });

    // 3. Mock scan trigger and job polling
    await page.route("**/api/v1/scanners/**run", async (route) => {
      await route.fulfill({
        status: 202,
        contentType: "application/json",
        body: JSON.stringify({
          id: "job-test-999",
          request_id: "req-test-999",
          spec_version_id: "ver-rsi-01",
          status: "PENDING",
          created_at: new Date().toISOString(),
        }),
      });
    });

    await page.route("**/api/v1/scanners/jobs/job-test-999", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: "job-test-999",
          request_id: "req-test-999",
          spec_version_id: "ver-rsi-01",
          status: "COMPLETED",
          candidate_count: 1,
          created_at: new Date().toISOString(),
          completed_at: new Date().toISOString(),
        }),
      });
    });

    // 4. Mock signals API
    await page.route("**/api/v1/signals*", async (route) => {
      const nowSec = Math.floor(Date.now() / 1000);
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          signals: [
            {
              id: "sig-candidate-001",
              scan_run_id: "run-001",
              spec_version_id: "ver-rsi-01",
              instrument_id: "BTCUSDT.BINANCE",
              timeframe: "1m",
              bar_open_ns: (nowSec - 120) * 1_000_000_000,
              bar_close_inclusive_ns: (nowSec - 60) * 1_000_000_000 - 1_000_000,
              interval_end_exclusive_ns: (nowSec - 60) * 1_000_000_000,
              matched_rules: ["rsi < 30", "volume_ratio > 1.2"],
              metrics: { rsi: 26.4, volume_ratio: 1.8 },
              suggested_entry: 54100.0,
              suggested_stop: 53500.0,
              suggested_target: 55300.0,
              created_at: new Date().toISOString(),
            },
          ],
        }),
      });
    });
  });

  test("loads terminal dashboard with visible TradingView attribution", async ({ page }) => {
    await page.goto("/?ws=terminal");

    // Verify Title and Header Branding
    await expect(page.getByText("CRYPTONITE", { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "Terminal" })).toBeVisible();
    await expect(page.locator("text=ZERO TRADING AUTHORITY")).toBeVisible();

    // Verify Mandatory TradingView Attribution
    const attributionLinks = page.locator("a[href='https://www.tradingview.com/lightweight-charts/']");
    const count = await attributionLinks.count();
    expect(count).toBeGreaterThan(0);
    await expect(attributionLinks.first()).toContainText(
      "Market charts powered by TradingView Lightweight Charts™ (Apache-2.0)"
    );

    // Verify Watchlist rendering
    await expect(page.locator("text=BTCUSDT").first()).toBeVisible();
    await expect(page.locator("text=ETHUSDT").first()).toBeVisible();
  });

  test("switches multi-chart grid layouts between 1x1, 1x2, and 2x2", async ({ page }) => {
    await page.goto("/?ws=terminal");

    // Click 1x1 layout button
    await page.click("button:has-text('1x1')");
    // In 1x1, only 1 chart header with timeframe tabs is visible in the grid
    await expect(page.locator("text=BTCUSDT").first()).toBeVisible();

    // Click 2x2 layout button
    await page.click("button:has-text('2x2')");
    // Verify quad grid loaded
    await expect(page.locator("text=Active:")).toBeVisible();
  });

  test("runs scanner and triggers View on Chart navigation", async ({ page }) => {
    await page.goto("/?ws=terminal");

    // Verify Scanner Studio is visible
    await expect(page.locator("text=Polars Scanner Studio")).toBeVisible();
    await expect(page.locator("text=Active Spec Rules")).toBeVisible();
    await expect(page.getByLabel("Select scanner preset")).toHaveValue("spec-rsi-01");

    // Click "Run Scanner"
    const runBtn = page.locator("button:has-text('Run Scanner')");
    await expect(runBtn).toBeEnabled();
    await runBtn.click();

    // Wait for completed status message
    await expect(page.locator("text=Scan completed successfully!")).toBeVisible({ timeout: 10000 });

    // Verify candidate card rendered
    await expect(page.locator("text=rsi < 30").first()).toBeVisible();
    await expect(page.locator("text=$54,100.00").first()).toBeVisible();
    await expect(page.locator("text=$53,500.00").first()).toBeVisible();
    await expect(page.locator("text=$55,300.00").first()).toBeVisible();

    // Click "Chart" (View on Chart) button
    const chartNavBtn = page.locator("button:has-text('Chart')").first();
    await chartNavBtn.click();

    // Verify Active chart target reflects BTCUSDT 1m
    await expect(page.locator("text=Active: BTCUSDT (1m)")).toBeVisible();
  });
});
