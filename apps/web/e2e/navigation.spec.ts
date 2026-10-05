import { test, expect } from "./fixtures/offline";

// R6.1-R6.3: every view has a URL, back/forward work, Mission Control is the landing view,
// navigation is grouped and the command palette lists every view.
test.describe("Workstation navigation", () => {
  test.beforeEach(async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem("cryptonite:ai-brain-tour-seen", "true"));
  });

  test("lands on Mission Control and groups the workspace tabs", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByTestId("mission-control")).toBeVisible();
    const nav = page.getByRole("navigation", { name: "Workspaces" });
    for (const group of ["Trade", "Research", "AI", "System"]) {
      await expect(nav.getByRole("group", { name: group, exact: true })).toBeVisible();
    }
    await expect(nav.getByRole("group", { name: "AI", exact: true }).getByRole("button")).toHaveText(["AI Brain", "Mission Control"]);
    await expect(nav.getByRole("button", { name: "Mission Control", exact: true })).toHaveAttribute("aria-current", "page");
  });

  test("tabs and sub-views update the URL; back and forward restore them", async ({ page }) => {
    await page.goto("/?ws=terminal");
    await page.getByRole("button", { name: "Research Lab", exact: true }).click();
    await expect(page).toHaveURL(/\?ws=research$/);
    await expect(page.getByText("TARGET STRATEGY")).toBeVisible();

    await page.getByRole("button", { name: "AI Brain", exact: true }).click();
    await expect(page).toHaveURL(/ws=brain&view=home/);
    await expect(page.getByTestId("brain-home")).toBeVisible();

    await page.getByRole("group", { name: "AI Brain view" }).getByRole("button", { name: "Debates" }).click();
    await expect(page).toHaveURL(/ws=brain&view=debates/);
    await expect(page.getByText("Action & Decision Inbox")).toBeVisible();

    await page.goBack();
    await expect(page).toHaveURL(/ws=brain&view=home/);
    await expect(page.getByTestId("brain-home")).toBeVisible();

    await page.goBack();
    await expect(page).toHaveURL(/\?ws=research$/);
    await expect(page.getByText("TARGET STRATEGY")).toBeVisible();

    await page.goForward();
    await expect(page.getByTestId("brain-home")).toBeVisible();
  });

  test("a deep link opens the named Mission Control view", async ({ page }) => {
    await page.goto("/?ws=mission&view=autonomy");
    await expect(page.getByRole("tab", { name: "Autonomy & Evals" })).toHaveAttribute("aria-selected", "true");
    await page.getByRole("tab", { name: "Mandate editor" }).click();
    await expect(page).toHaveURL(/ws=mission&view=mandates/);
  });

  test("the command palette lists every view and navigates to it", async ({ page }) => {
    await page.goto("/?ws=terminal");
    await expect(page.getByText("Polars Scanner Studio")).toBeVisible();
    await page.keyboard.press("Control+k");
    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();
    for (const label of ["Terminal", "Markets", "Portfolio", "Research Lab", "Scanner", "AI Brain → Debates", "Mission Control → Autonomy & Evals", "Operator", "Alerts", "Settings"]) {
      await expect(dialog.getByRole("option", { name: label, exact: true })).toHaveCount(1);
    }
    await dialog.getByRole("option", { name: "Mission Control → Autonomy & Evals", exact: true }).click();
    await expect(page).toHaveURL(/ws=mission&view=autonomy/);
    await expect(page.getByRole("tab", { name: "Autonomy & Evals" })).toHaveAttribute("aria-selected", "true");
  });
});
