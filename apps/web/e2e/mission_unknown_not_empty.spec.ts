import { test, expect } from "@playwright/test";

/**
 * A failed load is unknown, not empty: Mission Control never says "No mandates" or "Nothing waiting for you"
 * when it could not read them (ui-ux-critique 2026-10-12, priority 2).
 */
test("mandates and approvals that fail to load read as unknown, not empty", async ({ page }) => {
  await page.route("**/api/v1/**", (route) => route.fulfill({ status: 503, json: { detail: "offline" } }));
  await page.goto("/");
  await page.getByRole("button", { name: "Mission Control", exact: true }).click();
  await expect(page.getByTestId("mission-control")).toBeVisible();

  await expect(page.getByText("Mandates could not be loaded", { exact: false })).toBeVisible();
  await expect(page.getByText("Approvals could not be loaded", { exact: false })).toBeVisible();
  await expect(page.getByText("Nothing waiting for you.")).toHaveCount(0);
  await expect(page.getByText(/^No mandates\./)).toHaveCount(0);
});

test("an empty answer still says there is nothing", async ({ page }) => {
  await page.route("**/api/v1/**", (route) => route.fulfill({ status: 503, json: { detail: "offline" } }));
  await page.route("**/api/v1/ai/mandates*", (route) => route.fulfill({ json: [] }));
  await page.route("**/api/v1/ai/approvals*", (route) =>
    route.fulfill({ json: { approvals: [], now: new Date().toISOString() } }),
  );
  await page.goto("/");
  await page.getByRole("button", { name: "Mission Control", exact: true }).click();
  await expect(page.getByTestId("mission-control")).toBeVisible();

  await expect(page.getByText("Nothing waiting for you.")).toBeVisible();
  await expect(page.getByText(/^No mandates\./)).toBeVisible();
  await expect(page.getByText("Mandates could not be loaded", { exact: false })).toHaveCount(0);
  await expect(page.getByText("Approvals could not be loaded", { exact: false })).toHaveCount(0);
});
