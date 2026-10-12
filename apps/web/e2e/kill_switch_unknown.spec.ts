import { test, expect, type Page } from "@playwright/test";

/**
 * The PAPER kill-switch panel never renders ARMED for a status it could not read.
 * A missing row answers 503 from the control status endpoint, so the panel must show UNKNOWN.
 */

async function openLanding(page: Page) {
  await page.addInitScript(() => localStorage.setItem("csrf_token", "csrf-test"));
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Execution Kill-Switch · PAPER" })).toBeVisible();
}

test("an unreadable PAPER kill switch shows UNKNOWN, never ARMED", async ({ page }) => {
  // Everything else the page asks for fails fast, so only the kill-switch status matters here
  await page.route("**/api/v1/**", (route) => route.fulfill({ status: 503, json: { detail: "offline" } }));
  await openLanding(page);
  await expect(page.getByText("UNKNOWN", { exact: true })).toBeVisible();
  await expect(page.getByText("ARMED", { exact: true })).toHaveCount(0);
});

test("a status that fails after an armed read drops back to UNKNOWN", async ({ page }) => {
  let fail = false;
  await page.route("**/api/v1/**", (route) => route.fulfill({ status: 503, json: { detail: "offline" } }));
  await page.route("**/api/v1/control/kill-switch/status*", (route) =>
    fail
      ? route.fulfill({ status: 503, json: { detail: "No kill-switch row for PAPER/PAPER-ACCOUNT-001" } })
      : route.fulfill({
          json: { environment: "PAPER", account_id: "PAPER-ACCOUNT-001", is_latched: false, latch_generation: 1, updated_at: null },
        }),
  );
  await openLanding(page);
  await expect(page.getByText("ARMED", { exact: true })).toBeVisible();
  fail = true;
  await expect(page.getByText("UNKNOWN", { exact: true })).toBeVisible({ timeout: 15000 });
  await expect(page.getByText("ARMED", { exact: true })).toHaveCount(0);
});
