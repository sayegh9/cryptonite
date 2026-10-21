import { test, expect } from "./fixtures/offline";
import { openRiskSettings } from "./fixtures/settings-risk";

// Settings are full-page rows: the editor opens inline, one row at a time, with focus kept honest.

test.describe("Settings rows: layout and editor", () => {
  for (const size of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) {
    test(`no horizontal overflow at ${size.width}px`, async ({ page }) => {
      await page.setViewportSize(size);
      await openRiskSettings(page);
      const overflow = await page.evaluate(() => {
        const main = document.querySelector("main:has([data-testid='leverage-settings'])") as HTMLElement | null;
        return {
          doc: document.documentElement.scrollWidth - document.documentElement.clientWidth,
          main: main ? main.scrollWidth - main.clientWidth : -1,
        };
      });
      expect(overflow.doc).toBeLessThanOrEqual(0);
      expect(overflow.main).toBeLessThanOrEqual(0);
    });
  }

  test("Edit opens one row at a time, moves focus in, and Escape returns it to Edit", async ({ page }) => {
    await openRiskSettings(page);
    const a = page.getByTestId("setting-card-leverage.grade_ceiling_a");
    const b = page.getByTestId("setting-card-leverage.grade_ceiling_b");
    const editA = a.getByRole("button", { name: "Edit Leverage ceiling, grade A setups (x)" });
    const editB = b.getByRole("button", { name: /^Edit Leverage ceiling, grade B/ });

    await expect(editA).toHaveAttribute("aria-expanded", "false");
    await expect(a.locator('input[type="number"]')).toHaveCount(0);

    await editA.click();
    await expect(editA).toHaveAttribute("aria-expanded", "true");
    await expect(a.locator('input[type="number"]')).toBeFocused();
    expect(await editA.getAttribute("aria-controls")).toBeTruthy();

    await editB.click();
    await expect(editB).toHaveAttribute("aria-expanded", "true");
    await expect(editA).toHaveAttribute("aria-expanded", "false");

    await b.locator('input[type="number"]').press("Escape");
    await expect(editB).toHaveAttribute("aria-expanded", "false");
    await expect(editB).toBeFocused();
  });

  test("key chips never overflow their row and read-only guardrails are a table", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await openRiskSettings(page);
    const row = page.getByTestId("setting-card-leverage.slippage_allowance_pct");
    const box = await row.boundingBox();
    const chip = row.locator("span.font-mono").first();
    const chipBox = await chip.boundingBox();
    expect(chipBox!.x + chipBox!.width).toBeLessThanOrEqual(box!.x + box!.width + 1);
    await expect(page.getByRole("table", { name: "Fixed leverage guardrails" })).toBeVisible();
    await expect(page.getByTestId("setting-card-guard.perp_notional_pct")).toBeVisible();
  });
});
