import { test, expect, type Page } from "@playwright/test";

// R1.8: the Operator panel shows the limits the running system enforces, read-only.
async function openOperator(page: Page, limits: { status: number; body: unknown }) {
  await page.route("**/api/v1/auth/me", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ authenticated: true, username: "op", csrf_token: "t" }) }));
  await page.route("**/api/v1/control/enforced-limits", (route) =>
    route.fulfill({ status: limits.status, contentType: "application/json", body: JSON.stringify(limits.body) }));
  await page.route("**/api/v1/ai/mandates", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        mandates: [{ id: "m1", name: "BTC-ETH spot L1", status: "ACTIVE", capital_budget_usd: "2000", max_position_pct: "50", max_drawdown_pct: "10", allow_perps: false, max_leverage: 1 }],
      }),
    }));
  // Direct link: the stubbed mandates are partial, so do not land on Mission Control first
  await page.goto("/?ws=operator");
  return page.getByTestId("enforced-limits");
}

test.describe("Operator → Enforced limits", () => {
  test("shows published paper limits and mandate limits, with no edit form", async ({ page }) => {
    const card = await openOperator(page, {
      status: 200,
      body: {
        as_of: new Date().toISOString(),
        paper: { max_daily_loss_pct: "5", min_order_equity_pct: "1", max_order_equity_pct: "25", taker_fee_bps: "10", account_backstop_drawdown_pct: "20" },
      },
    });
    await expect(card).toBeVisible();
    await expect(card).toContainText("Max daily loss");
    await expect(card).toContainText("5 % of equity");
    await expect(card).toContainText("25 % of equity");
    await expect(card).toContainText("10 bps");
    await expect(card).toContainText("BTC-ETH spot L1");
    await expect(card).toContainText("Limits are not configurable here; edit the mandate");
    await expect(card.locator("input, form")).toHaveCount(0);
    await expect(page.getByText("UPDATE RISK LIMITS")).toHaveCount(0);
  });

  test("paper: null is shown as unknown", async ({ page }) => {
    const card = await openOperator(page, { status: 200, body: { as_of: null, paper: null } });
    await expect(card.getByTestId("enforced-limits-unknown")).toContainText("unknown");
    await expect(card).not.toContainText("% of equity");
  });

  test("a failed call is shown as unknown, never as default numbers", async ({ page }) => {
    const card = await openOperator(page, { status: 500, body: {} });
    await expect(card.getByTestId("enforced-limits-unknown")).toContainText("unknown (enforced-limits unavailable: HTTP 500)");
    await expect(card).not.toContainText("5 % of equity");
  });
});
