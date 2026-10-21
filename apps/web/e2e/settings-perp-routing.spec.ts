import type { Page } from "@playwright/test";
import { test, expect } from "./fixtures/offline";

// ADR 0010 P-10: debate.perp_routing is an enum stored as 0 (SPOT_FIRST) / 1 (PERP_FIRST). PERP_FIRST sends more
// trades to leveraged perps, so choosing it asks for the TOTP; going back to SPOT_FIRST is free.

const ROUTING = (value: string | null) => ({
  key: "debate.perp_routing",
  label: "Spot candidates: spot or perps first",
  default: "0",
  minimum: "0",
  maximum: "1",
  decimals: 0,
  apply: "hot",
  tier: "T/L",
  writable: true,
  adr: "0010",
  store: "system_settings",
  note: "PERP_FIRST sends more trades to leveraged perps, so it needs TOTP; SPOT_FIRST is free",
  value,
  value_source: value === null ? "default" : "api",
  effective: null,
  tighter: "lower",
  choices: [
    { value: "0", label: "SPOT_FIRST" },
    { value: "1", label: "PERP_FIRST" },
  ],
});

async function openRiskSection(page: Page, stored: string | null) {
  await page.addInitScript(() => localStorage.setItem("cryptonite:ai-brain-tour-seen", "true"));
  const json = (body: unknown) => ({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  await page.route("**/api/v1/auth/me", (route) =>
    route.fulfill(json({ authenticated: true, username: "lead_operator", role: "lead_operator", csrf_token: "test-csrf-token" }))
  );
  await page.route("**/api/v1/settings/registry", (route) => route.fulfill(json({ settings: [ROUTING(stored)] })));
  await page.route("**/api/v1/settings/environment", (route) =>
    route.fulfill(json({
      gemini_key_set: true, binance_key_set: true, environment_mode: "paper", kill_switch_active: false,
      dev_totp_login: false, dev_totp_approvals: false, secure_cookie: true, shadow_allow_paid: false, warnings: [],
    }))
  );
  await page.route("**/api/v1/settings/routes", (route) => route.fulfill(json({ routes: [] })));
  await page.route("**/api/v1/settings/agents", (route) => route.fulfill(json({})));
  await page.route("**/api/v1/settings/audit", (route) => route.fulfill(json({ events: [] })));
  await page.route("**/api/v1/settings/shadow-paid", (route) => route.fulfill(json({ allow_paid: false })));
  await page.goto("/?ws=settings");
  await page.locator("aside").getByRole("button", { name: /Risk & Guardrails/i }).click();
  return page.getByTestId("setting-card-debate.perp_routing");
}

test.describe("Settings: debate.perp_routing", () => {
  test("choosing PERP_FIRST asks for the TOTP and saves the stored number", async ({ page }) => {
    const card = await openRiskSection(page, null);
    await expect(card.getByText("SPOT_FIRST").first()).toBeVisible(); // the default, shown by label
    await expect(card.getByPlaceholder("6-digit code")).toHaveCount(0);

    await card.getByRole("button", { name: /^Edit / }).click();
    await card.getByRole("radio", { name: "PERP_FIRST" }).click();
    await expect(card.getByRole("radio", { name: "PERP_FIRST" })).toHaveAttribute("aria-checked", "true");
    await expect(card.getByPlaceholder("6-digit code")).toBeVisible();
    await expect(card.getByText("Loosening change detected")).toBeVisible();
    const save = card.getByRole("button", { name: /Save with TOTP/i });
    await expect(save).toBeDisabled();

    let sent: Record<string, unknown> | null = null;
    await page.route("**/api/v1/settings/registry/debate.perp_routing", (route) => {
      sent = route.request().postDataJSON();
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ success: true, key: "debate.perp_routing", changed: true, previous: "0", value: "1", totp_verified: true }),
      });
    });
    await card.getByLabel(/Reason/i).fill("Let BTC debates reach the perps mandate");
    await card.getByPlaceholder("6-digit code").fill("123456");
    await expect(save).toBeEnabled();
    await save.click();
    await expect.poll(() => sent).toEqual({ value: "1", reason: "Let BTC debates reach the perps mandate", totp_code: "123456" });
  });

  test("going back to SPOT_FIRST is free", async ({ page }) => {
    const card = await openRiskSection(page, "1");
    await expect(card.getByText("PERP_FIRST").first()).toBeVisible();
    await card.getByRole("button", { name: /^Edit / }).click();
    await card.getByRole("radio", { name: "SPOT_FIRST" }).click();
    await expect(card.getByPlaceholder("6-digit code")).toHaveCount(0);
    await expect(card.getByText(/Your TOTP code/i)).toHaveCount(0);
    await card.getByLabel(/Reason/i).fill("Back to spot first");
    await expect(card.getByRole("button", { name: /^Save$/ })).toBeEnabled();
  });
});
