import { test, expect } from "@playwright/test";

test.describe("Research Lab & Nautilus Backtesting Engine E2E", () => {
  test.beforeEach(async ({ page }) => {
    // 1. Mock market bars API for terminal
    await page.route("**/api/v1/bars*", async (route) => {
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
    await page.route("**/api/v1/scanners*", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([]),
      });
    });

    await page.route("**/api/v1/signals*", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ signals: [], total: 0 }),
      });
    });

    // 3. Mock registered strategies API
    await page.route("**/api/v1/strategies", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          {
            slug: "ema-cross",
            name: "EMA Crossover Strategy",
            description: "Trend-following fast EMA vs slow EMA cross.",
            author: "Cryptonite Core",
            version_num: 1,
            default_parameters: { fast_period: 9, slow_period: 21, trade_size: 0.01 },
            parameter_schema: {
              fast_period: { type: "integer", default: 9, minimum: 2, maximum: 200, title: "Fast EMA Period" },
              slow_period: { type: "integer", default: 21, minimum: 3, maximum: 500, title: "Slow EMA Period" },
              trade_size: { type: "number", default: 0.01, minimum: 0.0001, title: "Trade Size (BTC)" },
            },
            code_hash: "4e5af0a0a9b52c8fbcf5ed5a312ff761d7b5a80cb22d9318ab2f05cf414cf6c0",
          },
          {
            slug: "rsi-reversal",
            name: "RSI Oversold Reversal",
            description: "Mean-reversion strategy entering on oversold bounce.",
            author: "Cryptonite Core",
            version_num: 1,
            default_parameters: { rsi_period: 14, oversold_threshold: 30.0, exit_threshold: 50.0, trade_size: 0.01 },
            parameter_schema: {
              rsi_period: { type: "integer", default: 14, minimum: 2, maximum: 100, title: "RSI Period" },
              oversold_threshold: { type: "number", default: 30.0, minimum: 1.0, maximum: 50.0, title: "Oversold Threshold" },
              exit_threshold: { type: "number", default: 50.0, minimum: 30.0, maximum: 90.0, title: "Exit Threshold" },
              trade_size: { type: "number", default: 0.01, minimum: 0.0001, title: "Trade Size (BTC)" },
            },
            code_hash: "d0a701afb2e4fb6df6fdade56d09467723ae6872600267d9ca153421972bf9b1",
          },
        ]),
      });
    });

    // 4. Mock backtest runs submission & polling
    const mockRunId = "run-e2e-001";
    const mockRunData = {
      id: mockRunId,
      strategy_version_id: "00000000-0000-0000-0000-000000000001",
      instrument_id: "BTCUSDT.BINANCE",
      bar_type: "BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL",
      timeframe: "1m",
      parameters: { fast_period: 9, slow_period: 21, trade_size: 0.01 },
      warmup_start_ns: 1725700000000000000,
      evaluation_start_ns: 1725700600000000000,
      evaluation_end_ns: 1725701200000000000,
      execution_profile: "optimistic",
      run_config_hash: "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
      status: "COMPLETED",
      attempt_count: 1,
      results: {
        backtest_run_id: mockRunId,
        metrics: {
          total_return: 0.0452,
          cagr: null,
          sharpe_ratio: 2.14,
          sortino_ratio: 3.05,
          max_drawdown: 0.012,
          profit_factor: 2.45,
          win_rate: 0.667,
          total_trades: 6,
          winning_trades: 4,
          losing_trades: 2,
          avg_trade_pnl: 75.33,
          max_consecutive_losses: 1,
        },
        downsampled_equity: [
          { time: 1725700600, value: 10000.0, drawdown: 0.0 },
          { time: 1725700800, value: 10250.0, drawdown: 0.0 },
          { time: 1725701000, value: 10180.0, drawdown: 0.0068 },
          { time: 1725701200, value: 10452.0, drawdown: 0.0 },
        ],
        monthly_returns: {
          "2024": {
            "8": 0.0452,
            YTD: 0.0452,
          },
        },
        catalog_manifest: [
          {
            path: "data/bars/BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL/sample.parquet",
            size_bytes: 12450,
            sha256: "c1d2e3f4a5b6c1d2e3f4a5b6c1d2e3f4a5b6c1d2e3f4a5b6c1d2e3f4a5b6c1d2",
          },
        ],
        artifact_manifest: {
          run_id: mockRunId,
          files: {
            "equity_curve.parquet": { size_bytes: 4096, sha256: "e1e2e3e4e5e6e1e2e3e4e5e6e1e2e3e4e5e6e1e2e3e4e5e6e1e2e3e4e5e6e1e2" },
            "trades.parquet": { size_bytes: 2048, sha256: "t1t2t3t4t5t6t1t2t3t4t5t6t1t2t3t4t5t6t1t2t3t4t5t6t1t2t3t4t5t6t1t2" },
            "orders.parquet": { size_bytes: 3072, sha256: "o1o2o3o4o5o6o1o2o3o4o5o6o1o2o3o4o5o6o1o2o3o4o5o6o1o2o3o4o5o6o1o2" },
          },
        },
      },
    };

    // 4. Centralized Mock for all /api/v1/backtests* endpoints using regex
    await page.route(/\/api\/v1\/backtests/, async (route) => {
      const url = route.request().url();
      if (url.includes("/compare")) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            runs: [
              {
                id: mockRunId,
                strategy_slug: "ema-cross",
                strategy_name: "EMA Crossover Strategy",
                execution_profile: "optimistic",
                timeframe: "1m",
                status: "COMPLETED",
                parameters: { fast_period: 9, slow_period: 21 },
                metrics: {
                  total_return: 0.0452,
                  cagr: null,
                  sharpe_ratio: 2.14,
                  sortino_ratio: 3.05,
                  max_drawdown: 0.012,
                  profit_factor: 2.45,
                  win_rate: 0.667,
                  total_trades: 6,
                  winning_trades: 4,
                  losing_trades: 2,
                  avg_trade_pnl: 75.33,
                  max_consecutive_losses: 1,
                },
              },
            ],
          }),
        });
      } else if (url.includes("/trades")) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            page: 1,
            limit: 20,
            total: 1,
            pages: 1,
            trades: [
              {
                position_id: "POS-BTC-001",
                instrument_id: "BTCUSDT.BINANCE",
                side: "BUY",
                ts_opened_ns: 1725700600000000000,
                ts_closed_ns: 1725700800000000000,
                entry_price: 54000.0,
                exit_price: 54500.0,
                quantity: 0.5,
                realized_pnl: 250.0,
                realized_return: 0.0092,
              },
            ],
          }),
        });
      } else if (url.includes(`/${mockRunId}`)) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(mockRunData),
        });
      } else if (route.request().method() === "POST") {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ id: mockRunId, status: "PENDING" }),
        });
      } else {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify([mockRunData]),
        });
      }
    });
  });

  test("navigate to Research Lab, configure strategy, run backtest, and inspect tearsheet", async ({ page }) => {
    await page.goto("/");

    // 1. Switch to Research Lab mode
    const researchLabBtn = page.getByRole("button", { name: "Research Lab" });
    await expect(researchLabBtn).toBeVisible();
    await researchLabBtn.click();

    // Verify Research Lab sidebar header is visible
    await expect(page.getByText("Nautilus rc4")).toBeVisible();
    await expect(page.getByText("TARGET STRATEGY")).toBeVisible();

    // 2. Select strategy from dropdown
    const strategySelect = page.locator("select").first();
    await expect(strategySelect).toBeVisible();
    await strategySelect.selectOption("rsi-reversal");

    // Verify dynamic parameters updated to RSI parameters
    await expect(page.getByText("RSI Period:")).toBeVisible();
    await expect(page.getByText("Oversold Threshold:")).toBeVisible();

    // 3. Switch execution profiles
    const adverseBtn = page.getByText("Adverse Slippage (Stress)");
    await adverseBtn.click();
    await expect(page.getByText("Forces 1 adverse tick per fill")).toBeVisible();

    const conservativeBtn = page.getByText("Conservative Institutional");
    await conservativeBtn.click();
    await expect(page.getByText("80% limit fills, 50% slip, 5/10 bps fees")).toBeVisible();

    // 4. Click Run Quantitative Backtest
    const runBtn = page.getByRole("button", { name: "Run Quantitative Backtest" });
    await expect(runBtn).toBeVisible();
    await runBtn.click();

    // 5. Verify Tearsheet metric cards are rendered
    await expect(page.getByText("+4.52%")).toBeVisible(); // Total Return
    await expect(page.getByText("2.14")).toBeVisible(); // Sharpe Ratio
    await expect(page.getByText("1.20%")).toBeVisible(); // Max Drawdown
    await expect(page.getByText("66.7%")).toBeVisible(); // Win Rate

    // Verify Timing Contract Disclosure banner is present
    await expect(page.getByText("Execution Timing Contract:")).toBeVisible();
    await expect(page.getByText("execute against the simulated L1 book state at bar close")).toBeVisible();

    // 6. Switch to Trades Log tab
    await page.getByRole("button", { name: "Trades Log" }).click();
    await expect(page.getByText("POS-BTC-001")).toBeVisible();
    await expect(page.getByText("+$250.00")).toBeVisible();

    // 7. Switch to Monthly Returns Heatmap tab
    await page.getByRole("button", { name: "Monthly Returns" }).click();
    await expect(page.getByText("2024")).toBeVisible();
    await expect(page.getByText("YTD")).toBeVisible();

    // 8. Switch to Multi-Run Comparator tab
    await page.getByRole("button", { name: "Multi-Run Comparator" }).click();
    await expect(page.getByText("SCENARIO & RUN COMPARATOR")).toBeVisible();
    await expect(page.getByText("Select Runs:")).toBeVisible();
    await expect(page.getByText("Sharpe Ratio (0% RFR)")).toBeVisible();

    // 9. Inspect Reproducibility Manifest modal
    const manifestBtn = page.getByRole("button", { name: "Manifest" });
    await expect(manifestBtn).toBeVisible();
    await manifestBtn.click();

    // Verify modal elements
    await expect(page.getByText("REPRODUCIBILITY & DATA INTEGRITY MANIFEST")).toBeVisible();
    await expect(page.getByText("a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2")).toBeVisible();
    await expect(page.getByText("Input Data Catalog Files (Nautilus ParquetDataCatalog)")).toBeVisible();
    await expect(page.getByText("equity_curve.parquet")).toBeVisible();

    // Close manifest modal
    await page.getByRole("button", { name: "Close" }).click();
    await expect(page.getByText("REPRODUCIBILITY & DATA INTEGRITY MANIFEST")).not.toBeVisible();
  });
});
