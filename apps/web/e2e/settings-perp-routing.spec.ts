import type { Page } from "@playwright/test";
import { test, expect } from "./fixtures/offline";

// ADR 0011 D-P1-1: retired settings must remain hidden even when stale backend rows are returned.

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

test("retired routing settings are hidden even if an old backend supplies them", async ({ page }) => {
  const card = await openRiskSection(page, "1");
  await expect(card).toHaveCount(0);
  await expect(page.getByRole("radio", { name: "PERP_FIRST" })).toHaveCount(0);
});
