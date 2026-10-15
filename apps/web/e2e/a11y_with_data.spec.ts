import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";
import { test, expect } from "./fixtures/offline";

/**
 * WCAG 2 A/AA on the pages a QA run flagged (2026-10-14), with data on screen: the problems only showed once there
 * were movers and positions to draw (faded percentages, the flatten button, scroll regions, the live dot).
 */

const json = (body: unknown) => ({ status: 200, contentType: "application/json", body: JSON.stringify(body) });

async function seriousViolations(page: Page) {
  const r = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
  return r.violations
    .filter((v) => v.impact === "serious" || v.impact === "critical")
    .map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`);
}

test("Markets with gainers, losers and volume leaders has no serious violations", async ({ page }) => {
  const tickers = Array.from({ length: 14 }, (_, i) => ({
    symbol: `C${i}USDT`, last_price: 10 + i, change_pct: i % 2 ? 13.81 - i : -6.12 - i, volume_quote: 1e9 / (i + 1),
  }));
  await page.route("**/api/v1/market/overview*", (r) => r.fulfill(json(tickers)));
  await page.goto("/?ws=markets");
  await expect(page.getByRole("region", { name: "Volume leaders" })).toBeVisible();
  expect(await seriousViolations(page)).toEqual([]);
});

test("Portfolio with open positions has no serious violations", async ({ page }) => {
  await page.route("**/api/v1/paper/accounts", (r) => r.fulfill(json([])));
  await page.route("**/api/v1/paper/positions", (r) => r.fulfill(json([
    { position_id: "p1", account_id: "PAPER-ACCOUNT-001", instance_id: "MANUAL_TRADE", instrument_id: "BTCUSDT.BINANCE",
      side: "LONG", entry_price: 83599.29, quantity: 0.03, current_price: 83439.64, unrealized_pnl: -4.79, status: "OPEN" },
    { position_id: "p2", account_id: "PAPER-ACCOUNT-001", instance_id: "AI_MANDATE_x", instrument_id: "XRPUSDT.BINANCE",
      side: "LONG", entry_price: 1.3757, quantity: 363.45, current_price: 1.3727, unrealized_pnl: -1.09, status: "OPEN" },
  ])));
  for (const p of ["orders", "strategies"]) await page.route(`**/api/v1/paper/${p}`, (r) => r.fulfill(json([])));
  await page.goto("/?ws=portfolio");
  await expect(page.getByText("XRPUSDT.BINANCE").first()).toBeVisible();
  expect(await seriousViolations(page)).toEqual([]);
});

test("the live freshness dot is announced as an image named 'live'", async ({ page }) => {
  await page.goto("/?ws=terminal");
  const dots = page.locator('[aria-label="live"]');
  for (const dot of await dots.all()) await expect(dot).toHaveAttribute("role", "img");
});
