import { test, expect, type Page } from "@playwright/test";

// R1.8: the Operator panel shows the limits the running system enforces. F7: the paper limits can be
// tightened below the code defaults; loosening asks for TOTP; the server's validation errors are shown.
const CONFIG = {
  environment: "PAPER",
  account_id: "PAPER-ACCOUNT-001",
  defaults: { max_daily_loss_pct: "5", max_order_equity_pct: "25", cross_mandate_exposure_pct: "25" },
  overrides: { max_daily_loss_pct: "3", max_order_equity_pct: null, cross_mandate_exposure_pct: null },
  effective: { max_daily_loss_pct: "3", max_order_equity_pct: "25", cross_mandate_exposure_pct: "25" },
  min_order_equity_pct: "1",
  not_configurable: { account_backstop_drawdown_pct: "20" },
  updated_by: "op",
  updated_at: "2026-10-05T10:00:00Z",
  reason: "tightened for review",
};

async function openOperator(page: Page, limits: { status: number; body: unknown }, post?: (body: Record<string, unknown>) => { status: number; body: unknown }) {
  await page.route("**/api/v1/control/risk-limits**", (route) => {
    if (route.request().method() === "POST") {
      const res = post ? post(route.request().postDataJSON()) : { status: 200, body: { ...CONFIG, success: true, applies_within_s: 60 } };
      return route.fulfill({ status: res.status, contentType: "application/json", body: JSON.stringify(res.body) });
    }
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(CONFIG) });
  });
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
  test("shows published paper limits and mandate limits", async ({ page }) => {
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
    await expect(card).toContainText("Per-mandate limits are edited on the mandate");
    await expect(page.getByText("UPDATE RISK LIMITS")).toHaveCount(0);
  });

  test("tightening is sent without TOTP; loosening asks for it", async ({ page }) => {
    const sent: Record<string, unknown>[] = [];
    const card = await openOperator(page, { status: 200, body: { as_of: new Date().toISOString(), paper: null } }, (body) => {
      sent.push(body);
      return { status: 200, body: { ...CONFIG, success: true, applies_within_s: 60 } };
    });
    const form = card.getByTestId("risk-limits-form");
    await expect(form.getByTestId("risk-limit-current-max_daily_loss_pct")).toHaveText("3%");
    await form.getByLabel("Max order size").fill("10");
    await form.getByLabel("Reason").fill("tighten the order cap");
    await expect(form.getByTestId("risk-limits-totp")).toHaveCount(0);
    await form.getByRole("button", { name: "Save" }).click();
    await expect(form.getByTestId("risk-limits-saved")).toContainText("Not yet enforced");
    expect(sent[0]).toMatchObject({ max_daily_loss_pct: "3", max_order_equity_pct: "10", cross_mandate_exposure_pct: null, reason: "tighten the order cap" });
    expect(sent[0]).not.toHaveProperty("totp_code");

    // Clearing the 3% override goes back toward the 5% default: TOTP required before the button enables
    await form.getByLabel("Max daily loss").fill("");
    await form.getByLabel("Reason").fill("back to the default");
    await expect(form.getByTestId("risk-limits-totp")).toContainText("Max daily loss");
    const loosen = form.getByRole("button", { name: "Loosen with TOTP" });
    await expect(loosen).toBeDisabled();
    await form.getByLabel("TOTP code").fill("123456");
    await loosen.click();
    await expect.poll(() => sent.length).toBe(2);
    expect(sent[1]).toMatchObject({ max_daily_loss_pct: null, totp_code: "123456" });
  });

  test("the server's validation errors are shown per field", async ({ page }) => {
    const card = await openOperator(page, { status: 200, body: { as_of: null, paper: null } }, () => ({
      status: 422,
      body: { detail: [{ loc: ["body", "max_order_equity_pct"], msg: "max_order_equity_pct: 0.5% is below the 1% minimum order size; nothing could trade", type: "risk_limit" }] },
    }));
    const form = card.getByTestId("risk-limits-form");
    await form.getByLabel("Max order size").fill("0.5");
    await form.getByLabel("Reason").fill("too tight on purpose");
    await form.getByRole("button", { name: "Save" }).click();
    await expect(form.getByTestId("risk-limit-error-max_order_equity_pct")).toContainText("below the 1% minimum order size");
    await expect(form.getByTestId("risk-limits-saved")).toHaveCount(0);
  });

  test("a refused TOTP is shown, not a success", async ({ page }) => {
    const card = await openOperator(page, { status: 200, body: { as_of: null, paper: null } }, () => ({
      status: 401,
      body: { detail: "TOTP_REQUIRED: loosening max_daily_loss_pct needs your current TOTP code" },
    }));
    const form = card.getByTestId("risk-limits-form");
    await form.getByLabel("Max daily loss").fill("4");
    await form.getByLabel("Reason").fill("loosen a little");
    await form.getByLabel("TOTP code").fill("000001");
    await form.getByRole("button", { name: "Loosen with TOTP" }).click();
    await expect(form.getByTestId("risk-limits-error")).toContainText("TOTP_REQUIRED");
    await expect(form.getByTestId("risk-limits-saved")).toHaveCount(0);
  });

  test("paper: null is shown as unknown", async ({ page }) => {
    const card = await openOperator(page, { status: 200, body: { as_of: null, paper: null } });
    await expect(card.getByTestId("enforced-limits-unknown")).toContainText("unknown");
    await expect(card.locator("dl")).toHaveCount(0); // no published dispatcher values
  });

  test("a failed call is shown as unknown, never as default numbers", async ({ page }) => {
    const card = await openOperator(page, { status: 500, body: {} });
    await expect(card.getByTestId("enforced-limits-unknown")).toContainText("unknown (enforced-limits unavailable: HTTP 500)");
    await expect(card).not.toContainText("5 % of equity");
  });

  test("transitions to enforced after the orchestrator applies the override", async ({ page }) => {
    const card = await openOperator(page, { status: 200, body: { as_of: "2026-10-07T00:00:00Z", paper: null } }, () => ({
      status: 200,
      body: { ...CONFIG, success: true, applies_within_s: 60, override_version: 7 },
    }));
    const form = card.getByTestId("risk-limits-form");
    await form.getByLabel("Max order size").fill("10");
    await form.getByLabel("Reason").fill("tighten the order cap");

    // Orchestrator has not applied v7 yet: effective_version is still stale (0).
    await page.unroute("**/api/v1/control/enforced-limits");
    await page.route("**/api/v1/control/enforced-limits", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ as_of: null, paper: null, effective_version: 0, enforced_at: null }),
      }));

    await form.getByRole("button", { name: "Save" }).click();
    await expect(form.getByTestId("risk-limits-saved")).toContainText("Not yet enforced");
    await expect(form.getByTestId("risk-limits-saved")).toContainText("override v7");

    // The orchestrator applies v7 a moment later; the form must poll and flip over.
    await page.unroute("**/api/v1/control/enforced-limits");
    await page.route("**/api/v1/control/enforced-limits", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          as_of: "2026-10-07T00:05:00Z",
          paper: { max_daily_loss_pct: "3", min_order_equity_pct: "1", max_order_equity_pct: "10", taker_fee_bps: "10", account_backstop_drawdown_pct: "20" },
          effective_version: 7,
          enforced_at: "2026-10-07T00:05:00Z",
        }),
      }));
    await expect(form.getByTestId("risk-limits-saved")).toContainText("Enforced since");
  });

  test("stale or missing enforced telemetry keeps the pending message", async ({ page }) => {
    const card = await openOperator(page, { status: 200, body: { as_of: null, paper: null } }, () => ({
      status: 200,
      body: { ...CONFIG, success: true, applies_within_s: 60, override_version: 3 },
    }));
    const form = card.getByTestId("risk-limits-form");
    await form.getByLabel("Max order size").fill("10");
    await form.getByLabel("Reason").fill("tighten the order cap");

    // effective_version never reaches 3 (and there is no enforced_at): pending stays.
    await page.unroute("**/api/v1/control/enforced-limits");
    await page.route("**/api/v1/control/enforced-limits", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ as_of: "2026-10-07T00:00:00Z", paper: null, effective_version: 2, enforced_at: null }),
      }));

    await form.getByRole("button", { name: "Save" }).click();
    await expect(form.getByTestId("risk-limits-saved")).toContainText("Not yet enforced");
    await expect(form.getByTestId("risk-limits-saved")).toContainText("override v3");

    // Missing telemetry (paper: null, no version fields) also fails closed to pending.
    await page.unroute("**/api/v1/control/enforced-limits");
    await page.route("**/api/v1/control/enforced-limits", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ as_of: null, paper: null }) }));
    await form.getByLabel("Max order size").fill("9");
    await form.getByLabel("Reason").fill("tighter still");
    await form.getByRole("button", { name: "Save" }).click();
    await expect(form.getByTestId("risk-limits-saved")).toContainText("Not yet enforced");
  });
});
