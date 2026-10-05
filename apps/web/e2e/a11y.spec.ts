import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

// R6.6: no serious or critical axe (WCAG 2 A/AA) issues on the main views.
// Backend calls are not stubbed: views render their unknown/error states, which must be accessible too.
const VIEWS: { name: string; url: string; ready: string }[] = [
  { name: "Mission Control", url: "/?ws=mission&view=overview", ready: "[data-testid='mission-control']" },
  { name: "Terminal", url: "/?ws=terminal", ready: "#scanner-studio" },
  { name: "Markets", url: "/?ws=markets", ready: "nav[aria-label='Workspaces']" },
  { name: "Portfolio", url: "/?ws=portfolio", ready: "nav[aria-label='Workspaces']" },
  { name: "Research Lab", url: "/?ws=research", ready: "text=TARGET STRATEGY" },
  { name: "Brain Home", url: "/?ws=brain&view=home", ready: "[data-testid='brain-home']" },
  { name: "Debates", url: "/?ws=brain&view=debates", ready: "text=Action & Decision Inbox" },
  { name: "Alerts", url: "/?ws=alerts", ready: "nav[aria-label='Workspaces']" },
  { name: "Settings", url: "/?ws=settings", ready: "nav[aria-label='Workspaces']" },
];

test.describe("Accessibility (axe)", () => {
  test.beforeEach(async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem("cryptonite:ai-brain-tour-seen", "true"));
  });

  test("Copilot drawer traps focus and closes on Escape", async ({ page }) => {
    await page.goto("/?ws=terminal");
    const opener = page.getByRole("button", { name: "Brain", exact: true });
    await opener.click();
    const dialog = page.getByRole("dialog", { name: "Brain copilot" });
    await expect(dialog).toBeVisible();
    await expect(dialog).toHaveAttribute("aria-modal", "true");
    for (let i = 0; i < 12; i++) {
      await page.keyboard.press("Tab");
      expect(await dialog.evaluate((el) => el.contains(document.activeElement))).toBe(true);
    }
    await page.keyboard.press("Shift+Tab");
    expect(await dialog.evaluate((el) => el.contains(document.activeElement))).toBe(true);
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    await expect(opener).toBeFocused();
  });

  for (const v of VIEWS) {
    test(`${v.name}: no serious or critical issues`, async ({ page }) => {
      await page.goto(v.url);
      await page.locator(v.ready).first().waitFor();
      await page.waitForTimeout(500);
      const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
      const serious = results.violations.filter((x) => x.impact === "serious" || x.impact === "critical");
      const report = serious.map((x) => `${x.id} (${x.impact}): ${x.nodes.length} node(s), e.g. ${x.nodes.slice(0, 3).map((n) => n.target.join(" ")).join(" | ")}`);
      expect(report, report.join("\n")).toEqual([]);
    });
  }
});
