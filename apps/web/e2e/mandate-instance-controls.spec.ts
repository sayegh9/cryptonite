import { test, expect, type Page } from "@playwright/test";

// AI mandates are paused, resumed and stopped in Mission Control, so their instance rows and drawers offer only a link
// there, plus FLATTEN (close-position) while running, since flattening reduces risk. Other instances keep their controls.
const MANDATE = {
  instance_id: "AI_MANDATE_587a1f2c",
  strategy_name: "AI Mandate",
  instrument_id: "BTCUSDT.BINANCE",
  timeframe: "1m",
  allocation_usd: 2000,
  status: "RUNNING",
  last_processed_bar_ns: 1725700000000000000,
};
const RUNNING = {
  instance_id: "INST-EMA-01",
  strategy_name: "EMACrossover",
  instrument_id: "BTCUSDT.BINANCE",
  timeframe: "1m",
  allocation_usd: 5000,
  status: "RUNNING",
  last_processed_bar_ns: 1725700000000000000,
};
const PAUSED = {
  instance_id: "INST-RSI-02",
  strategy_name: "RSIReversion",
  instrument_id: "ETHUSDT.BINANCE",
  timeframe: "5m",
  allocation_usd: 3000,
  status: "PAUSED",
  last_processed_bar_ns: 1725700000000000000,
};
const STRATEGIES = [MANDATE, RUNNING, PAUSED];

async function stubStrategies(page: Page) {
  await page.route("**/api/v1/paper/strategies*", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(STRATEGIES) }));
}

test.describe("AI mandate instances: no instance commands, link to Mission Control", () => {
  test("operator panel shows only FLATTEN and the link on an AI mandate row and keeps controls for other instances", async ({ page }) => {
    await page.route("**/api/v1/auth/me", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ authenticated: true, username: "op", csrf_token: "t" }) }));
    await stubStrategies(page);

    // Direct link to the operator workspace, as in enforced_limits.spec.ts
    await page.goto("/?ws=operator");

    const mandateRow = page.locator("tr", { hasText: "AI_MANDATE_587a" });
    await expect(mandateRow).toBeVisible();
    await expect(mandateRow.getByRole("link", { name: "Mission Control" })).toBeVisible();
    await expect(mandateRow.getByTitle("Flatten Position")).toBeVisible();
    await expect(mandateRow.getByTitle("Resume")).toHaveCount(0);
    await expect(mandateRow.getByTitle("Pause")).toHaveCount(0);
    await expect(mandateRow.getByTitle("Stop")).toHaveCount(0);

    // Other instances keep their controls
    await expect(page.locator("tr", { hasText: "INST-RSI-02" }).getByTitle("Resume")).toBeVisible();
    const running = page.locator("tr", { hasText: "INST-EMA-01" });
    await expect(running.getByTitle("Pause")).toBeVisible();
    await expect(running.getByTitle("Flatten Position")).toBeVisible();
    await expect(running.getByTitle("Stop")).toBeVisible();
    await expect(running.getByRole("link", { name: "Mission Control" })).toHaveCount(0);
  });

  test("the instance drawer for an AI mandate offers the link and Flatten instead of Pause, Resume and Stop", async ({ page }) => {
    await page.route("**/api/v1/auth/me", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ authenticated: true, username: "op", csrf_token: "t" }) }));
    await stubStrategies(page);
    await page.goto("/?ws=operator");

    await page.locator("tr", { hasText: "AI_MANDATE_587a" }).click();
    const drawer = page.locator("div.fixed", { hasText: "INSTANCE DETAILS" });
    await expect(drawer).toBeVisible();
    await expect(drawer.getByRole("link", { name: "Mission Control" })).toBeVisible();
    await expect(drawer.getByRole("button", { name: "Flatten Position" })).toBeVisible();
    await expect(drawer.getByText("Pause Strategy")).toHaveCount(0);
    await expect(drawer.getByText("Resume Strategy")).toHaveCount(0);
    await expect(drawer.getByRole("button", { name: /Stop/ })).toHaveCount(0);
  });

  test("FLATTEN on an AI mandate row sends close-position for that instance", async ({ page }) => {
    await page.route("**/api/v1/auth/me", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ authenticated: true, username: "op", csrf_token: "t" }) }));
    await stubStrategies(page);
    // Registered after the strategies stub so it takes precedence for the command URL
    const commands: string[] = [];
    await page.route("**/api/v1/paper/strategies/AI_MANDATE_587a1f2c/close-position", (route) => {
      commands.push(route.request().method());
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ok: true }) });
    });
    await page.goto("/?ws=operator");

    await page.locator("tr", { hasText: "AI_MANDATE_587a" }).getByTitle("Flatten Position").click();
    await expect.poll(() => commands).toEqual(["POST"]);
  });

  test("the link switches to Mission Control", async ({ page }) => {
    await page.route("**/api/v1/auth/me", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ authenticated: true, username: "op", csrf_token: "t" }) }));
    await stubStrategies(page);
    await page.goto("/?ws=operator");

    await page.locator("tr", { hasText: "AI_MANDATE_587a" }).getByRole("link", { name: "Mission Control" }).click();
    await expect(page).toHaveURL(/ws=mission/);
  });

  test("portfolio strategy dashboard hides command buttons for an AI mandate and keeps them for other instances", async ({ page }) => {
    await page.route("**/api/v1/paper/accounts*", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([{ account_id: "PAPER-ACCOUNT-001", venue: "BINANCE", currency: "USDT", starting_balance: 10000, cash_balance: 8500, total_equity: 10150, realized_pnl: 150, daily_realized_pnl: 150, daily_loss_latched: false }]),
      }));
    await page.route("**/api/v1/paper/positions*", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) }));
    await page.route("**/api/v1/paper/orders*", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) }));
    await page.route("**/api/v1/paper/trades*", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) }));
    await page.route("**/api/v1/paper/kill-switch*", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ id: 1, is_latched: false }) }));
    await stubStrategies(page);

    await page.goto("/?ws=portfolio");

    const section = page.getByRole("region", { name: "Strategy Instances" });
    await expect(section).toBeVisible();

    const mandateRow = section.getByRole("listitem").filter({ hasText: "AI_MANDATE_587a1f2c" });
    await expect(mandateRow).toBeVisible();
    await expect(mandateRow.getByRole("link", { name: "Mission Control" })).toBeVisible();
    await expect(mandateRow.getByRole("button", { name: /strategy/i })).toHaveCount(0);

    // Other instances keep their controls
    await expect(section.getByRole("button", { name: "Resume strategy RSIReversion" })).toBeVisible();
    const runningRow = section.getByRole("listitem").filter({ hasText: "INST-EMA-01" });
    await expect(runningRow.getByRole("button", { name: "Pause strategy EMACrossover" })).toBeVisible();
    await expect(runningRow.getByRole("button", { name: "Stop strategy EMACrossover" })).toBeVisible();
  });
});
