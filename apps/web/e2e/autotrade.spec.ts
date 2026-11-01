import AxeBuilder from "@axe-core/playwright";
import { test, expect } from "./fixtures/offline";
import { mockAutoTrade, P1, P2, profile, VALID_TOTP } from "./fixtures/autotrade";

/** P2 Auto-Trade workspace: list, Stop all, wizard (TOTP, validation, coin lock), detail tabs and editor, a11y, layout. */

test.describe("Auto-Trade", () => {
  test("list shows each profile's status, mode line, P&L scale and health", async ({ page }) => {
    await mockAutoTrade(page);
    await page.goto("/?ws=autotrade");
    const cards = page.getByTestId("profile-card");
    await expect(cards).toHaveCount(2);
    const btc = cards.filter({ hasText: "BTC swing paper" });
    await expect(btc.getByTestId("profile-status")).toHaveText("Running");
    await expect(btc.getByTestId("profile-mode-line")).toHaveText("Mode: Ask me");
    await expect(btc.getByRole("meter", { name: /Today's P&L against the daily loss limit/ })).toBeVisible();
    await expect(btc.getByTestId("profile-health")).toContainText("100/100");
    const xrp = cards.filter({ hasText: "XRP short hedge" });
    await expect(xrp.getByTestId("profile-mode-line")).toHaveText("Paused: BREAKER_LOSS_STREAK");
    await expect(xrp.getByRole("button", { name: "Resume XRP short hedge" })).toBeVisible();
    await expect(btc.getByRole("button", { name: "Pause BTC swing paper" })).toBeVisible();
  });

  test("Stop all stops running and paused profiles, optionally closing positions, without TOTP", async ({ page }) => {
    const be = await mockAutoTrade(page);
    await page.goto("/?ws=autotrade");
    await page.getByRole("button", { name: "Stop all" }).click();
    const dialog = page.getByRole("dialog", { name: "Stop all profiles?" });
    await expect(dialog).toContainText("close on their own stops and targets");
    await expect(dialog).toContainText("The master switch is unchanged.");
    await dialog.getByLabel("Also close open positions now").check();
    await dialog.getByRole("button", { name: "Stop 2 profiles" }).click();
    await expect(dialog).toHaveCount(0);
    expect(be.writes).toEqual([{ method: "POST", path: "/profiles/stop-all", body: { close: true } }]);
    // Nothing is left to stop, so the button is disabled; the outcome is announced instead
    await expect(page.getByRole("status").filter({ hasText: "Stop requested for 2 profiles. Closing open positions now." })).toHaveCount(1);
    await expect(page.getByRole("button", { name: "Stop all" })).toBeDisabled();
  });

  test("pausing needs no code; resuming always asks for TOTP", async ({ page }) => {
    const be = await mockAutoTrade(page);
    await page.goto("/?ws=autotrade");
    await page.getByRole("button", { name: "Resume XRP short hedge" }).click();
    await page.getByRole("dialog", { name: "Resume XRP short hedge?" }).getByRole("button", { name: "Resume profile" }).click();
    const totp = page.getByRole("dialog", { name: "Confirm this risk change" });
    await expect(totp.getByLabel("TOTP code")).toBeFocused();
    await expect(totp.getByRole("button", { name: "Confirm and resume" })).toBeDisabled();
    await totp.getByLabel("TOTP code").fill("000000");
    await totp.getByRole("button", { name: "Confirm and resume" }).click();
    await expect(totp.getByRole("alert")).toContainText("That code wasn't accepted");
    await totp.getByLabel("TOTP code").fill(VALID_TOTP);
    await totp.getByRole("button", { name: "Confirm and resume" }).click();
    await expect(totp).toHaveCount(0);
    expect(be.writes.map((w) => w.path)).toEqual([`/mandates/${P2}/resume`, `/mandates/${P2}/resume`]);
    // the code is never kept in browser storage
    expect(await page.evaluate(() => JSON.stringify(localStorage))).not.toContain(VALID_TOTP);
  });

  test("wizard: keyboard-only creation with a preset, TOTP and dry run", async ({ page }) => {
    const be = await mockAutoTrade(page, { profiles: [] });
    await page.goto("/?ws=autotrade");
    await expect(page.getByText("No profiles yet")).toBeVisible();
    await page.getByRole("button", { name: "Create profile" }).first().click();
    await expect(page.getByRole("heading", { name: /Step 1 of 4/ })).toBeFocused();

    await page.keyboard.press("Tab"); // Cancel
    await page.keyboard.press("Tab");
    await page.keyboard.type("ETH hedge");
    await page.keyboard.press("Tab");
    await page.keyboard.type("ETHUSDT, BTCUSDT");
    await expect(page.getByText("The live feed supports 20 symbols.")).toBeVisible();
    await page.getByRole("button", { name: "Continue" }).focus();
    await page.keyboard.press("Enter");

    await expect(page.getByRole("heading", { name: /Step 2 of 4/ })).toBeFocused();
    await page.getByRole("radio", { name: /Short-only hedge/ }).check();
    const replay = page.getByTestId("replay-stats");
    await expect(replay).toContainText("Negative replay: -0.63R average R over 49 trades.");
    await expect(replay).toContainText("Thin replay: 3 trades.");
    await expect(replay).toContainText("No replay data for BTCUSDT.");
    const picker = page.getByTestId("strategy-picker");
    await expect(picker.getByRole("checkbox", { name: /^trend-pullback-short / })).not.toBeChecked();  // offered, not a default
    await expect(picker.getByRole("checkbox", { name: /^swing-trend-pullback-1h/ })).toHaveCount(0);  // a long preset
    await picker.getByRole("checkbox", { name: /swing-sniper-pullback-short-15m/ }).uncheck();
    await picker.getByRole("checkbox", { name: "15m", exact: true }).check();
    await page.getByRole("button", { name: "Continue" }).press("Enter");

    await expect(page.getByRole("heading", { name: /Step 3 of 4/ })).toBeFocused();
    await expect(page.getByLabel("Risk per trade (%)")).toHaveValue("0.5");
    await expect(page.getByLabel("Maximum leverage")).toHaveValue("2");
    await expect(page.getByText(/leverage changes how close liquidation is/)).toBeVisible();
    await page.getByRole("button", { name: "Continue" }).press("Enter");

    await expect(page.getByRole("heading", { name: /Step 4 of 4/ })).toBeFocused();
    await expect(page.getByLabel(/Start in dry run/)).toBeChecked();
    await page.getByRole("button", { name: "Create and start" }).press("Enter");
    const totp = page.getByRole("dialog", { name: "Confirm this risk change" });
    await expect(totp).toContainText("Creating ETH hedge increases risk");
    await page.keyboard.type(VALID_TOTP);
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(/ws=autotrade&view=profile&id=cccccccc/);

    const create = be.writes.find((w) => w.path === "/mandates")!;
    expect(create.body).toMatchObject({
      name: "ETH hedge",
      markets: ["SPOT_MARGIN", "PERP"],
      direction: "SHORT",
      max_leverage: 2,
      max_risk_per_trade_pct: "0.5",
      daily_loss_limit_pct: "1.5",
      dry_run: true,
      environment: "PAPER",
      totp_code: VALID_TOTP,
      strategies: ["swing-trend-pullback-short-1h", "swing-squeeze-breakdown-short-1h"],
      timeframes: ["15m", "1h", "4h"],
    });
    expect(create.body.instruments).toEqual(["ETHUSDT-MARGIN.BINANCE", "ETHUSDT-PERP.BINANCE", "BTCUSDT-MARGIN.BINANCE", "BTCUSDT-PERP.BINANCE"]);
  });

  test("H3: an amber line says when the budget cannot place a perp order, in the wizard review and in Settings", async ({ page }) => {
    await mockAutoTrade(page, { profiles: [] });
    await page.goto("/?ws=autotrade&view=new");
    await page.getByLabel("Profile name").fill("Small perp");
    await page.getByLabel("Coins").fill("ETHUSDT");
    for (let i = 0; i < 3; i++) await page.getByRole("button", { name: "Continue" }).click();
    const warn = page.getByTestId("feasibility-warning");
    await expect(warn).toHaveCount(1);
    await expect(warn).toContainText("Perp entries will be refused");
    await expect(warn).toContainText("the minimum is $995");
    await expect(warn).toContainText("Raise the budget to about $9,950 or more.");

    await mockAutoTrade(page);
    await page.goto(`/?ws=autotrade&view=profile&id=${P1}&tab=settings`);
    await expect(page.getByTestId("feasibility-warning")).toContainText("Perp entries will be refused");
  });

  test("wizard: a Veto window profile is created in Ask me, then raised with a second code", async ({ page }) => {
    const be = await mockAutoTrade(page, { profiles: [] });
    await page.goto("/?ws=autotrade&view=new");
    await page.getByLabel("Profile name").fill("Veto ETH");
    await page.getByLabel("Coins").fill("ETHUSDT");
    for (let i = 0; i < 3; i++) await page.getByRole("button", { name: "Continue" }).click();
    await page.getByRole("radio", { name: /Veto window/ }).check();
    await page.getByRole("button", { name: "Create and start" }).click();
    const totp = page.getByRole("dialog", { name: "Confirm this risk change" });
    await totp.getByLabel("TOTP code").fill(VALID_TOTP);
    await totp.getByRole("button", { name: "Confirm and start" }).click();
    await expect(totp).toContainText("Switching Veto ETH to Veto window");
    await totp.getByLabel("TOTP code").fill(VALID_TOTP);
    await totp.getByRole("button", { name: "Confirm mode" }).click();
    await expect(page).toHaveURL(/view=profile&id=cccccccc/);
    const confirm = be.writes.find((w) => w.path.endsWith("/autonomy/confirm"))!;
    expect(confirm.body).toEqual({ level: 2, totp_code: VALID_TOTP });
  });

  test("wizard: validation, a locked coin, a coin outside the feed and the switch-off warning", async ({ page }) => {
    await mockAutoTrade(page);
    await page.goto("/?ws=autotrade&view=new");
    await page.getByRole("button", { name: "Continue" }).click();
    await expect(page.getByText("Enter a profile name.")).toBeVisible();
    await expect(page.getByLabel("Profile name")).toBeFocused();
    await page.getByLabel("Profile name").fill("Dupes");
    await page.getByLabel("Coins").fill("BTCUSDT, DOGEUSDT");
    await expect(page.getByText("BTCUSDT is used by profile BTC swing paper.")).toBeVisible();
    await expect(page.getByText("DOGEUSDT is not in the live feed.")).toBeVisible();
    await page.getByRole("button", { name: "Continue" }).click();
    await page.getByRole("radio", { name: "Short Needs Spot Margin or Perpetuals." }).check();
    await page.getByRole("button", { name: "Continue" }).click();
    await expect(page.getByText("Short profiles need Spot Margin or Perpetuals.")).toBeVisible();
    await page.getByRole("radio", { name: "Long", exact: true }).check();
    await page.getByRole("button", { name: "Continue" }).click();
    await page.getByLabel("Daily loss limit (%)").fill("0.5");
    await page.getByRole("button", { name: "Continue" }).click();
    await expect(page.getByText("Daily loss limit must be at least risk per trade.")).toBeVisible();
    await page.getByLabel("Daily loss limit (%)").fill("");
    await page.getByRole("button", { name: "Continue" }).click();
    await page.getByRole("radio", { name: /Fully automatic/ }).check();
    await expect(page.getByRole("alert").filter({ hasText: "The auto-trade switch is off" })).toBeVisible();
  });

  test("Cancel with a changed draft asks before discarding", async ({ page }) => {
    await mockAutoTrade(page);
    await page.goto("/?ws=autotrade&view=new");
    await page.getByLabel("Profile name").fill("Scratch");
    await page.getByRole("button", { name: "Cancel" }).click();
    const dialog = page.getByRole("dialog", { name: "Discard profile draft?" });
    await dialog.getByRole("button", { name: "Discard draft" }).click();
    await expect(page).toHaveURL(/view=list/);
  });

  test("detail: timeline pages, performance shows R coverage, positions show liquidation", async ({ page }) => {
    await mockAutoTrade(page);
    await page.goto(`/?ws=autotrade&id=${P1}`);
    await expect(page.getByRole("heading", { name: "BTC swing paper" })).toBeVisible();
    const tabs = page.getByRole("tablist", { name: "Profile sections" });
    await expect(tabs.getByRole("tab", { name: "Timeline" })).toHaveAttribute("aria-selected", "true");
    await expect(page.getByText("max open positions")).toBeVisible();
    await page.getByRole("button", { name: "Load older decisions" }).click();
    await expect(page.getByRole("button", { name: "Load older decisions" })).toHaveCount(0);

    await tabs.getByRole("tab", { name: "Timeline" }).focus();
    await page.keyboard.press("ArrowRight");
    await expect(tabs.getByRole("tab", { name: "Performance" })).toBeFocused();
    await expect(page).toHaveURL(/tab=performance/);
    await expect(page.getByText("Avg R over 0 of 1 audited exits (1 without R)")).toBeVisible();
    await expect(page.getByText(/Outcome metrics cover exit audit decisions only/)).toBeVisible();

    await page.keyboard.press("End");
    await page.keyboard.press("ArrowLeft");
    await expect(page).toHaveURL(/tab=positions/);
    await expect(page.getByText("50500 (50.0% away)")).toBeVisible();
  });

  test("settings: lowering risk saves directly; raising it asks for TOTP", async ({ page }) => {
    const be = await mockAutoTrade(page);
    await page.goto(`/?ws=autotrade&view=profile&id=${P1}&tab=settings`);
    await page.getByRole("button", { name: "Edit Risk per trade (%)" }).click();
    await page.getByLabel("Risk per trade (%)").fill("0.5");
    await page.getByRole("button", { name: "Save" }).click();
    await expect(page.getByRole("button", { name: "Edit Risk per trade (%)" })).toBeVisible();
    await page.getByRole("button", { name: "Edit Risk per trade (%)" }).click();
    await page.getByLabel("Risk per trade (%)").fill("1.5");
    await page.getByRole("button", { name: "Save" }).click();
    const totp = page.getByRole("dialog", { name: "Confirm this risk change" });
    await totp.getByLabel("TOTP code").fill(VALID_TOTP);
    await totp.getByRole("button", { name: "Save changes" }).click();
    await expect(totp).toHaveCount(0);
    expect(be.writes.map((w) => w.body)).toEqual([
      { max_risk_per_trade_pct: "0.5" },
      { max_risk_per_trade_pct: "1.5" },
      { max_risk_per_trade_pct: "1.5", totp_code: VALID_TOTP },
    ]);
  });

  test("Mission Control cards open the profile in Auto-Trade, and its editor tab points there", async ({ page }) => {
    await mockAutoTrade(page, { profiles: [profile()] });
    await page.goto("/?ws=mission&view=overview");
    await page.getByRole("button", { name: "Open BTC swing paper in Auto-Trade" }).click();
    await expect(page).toHaveURL(new RegExp(`ws=autotrade&view=profile&id=${P1}`));
    await expect(page.getByRole("heading", { name: "BTC swing paper" })).toBeVisible();
  });

  for (const width of [390, 1440]) {
    test(`no horizontal overflow at ${width}px on list, wizard and detail`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 });
      await mockAutoTrade(page);
      for (const url of ["/?ws=autotrade", "/?ws=autotrade&view=new", `/?ws=autotrade&id=${P1}&tab=performance`]) {
        await page.goto(url);
        await page.getByTestId("autotrade-workspace").waitFor();
        await page.waitForTimeout(300);
        const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
        expect(overflow, url).toBeLessThanOrEqual(0);
      }
    });
  }

  for (const [name, url] of [["list", "/?ws=autotrade"], ["wizard", "/?ws=autotrade&view=new"], ["detail", `/?ws=autotrade&id=${P1}`]] as const) {
    test(`axe: no serious or critical issues on the ${name}`, async ({ page }) => {
      await mockAutoTrade(page);
      await page.goto(url);
      await page.getByTestId("autotrade-workspace").waitFor();
      await page.waitForTimeout(500);
      const results = await new AxeBuilder({ page }).include("[data-testid='autotrade-workspace']").withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
      const serious = results.violations.filter((x) => x.impact === "serious" || x.impact === "critical");
      const report = serious.map((x) => `${x.id} (${x.impact}): ${x.nodes.slice(0, 3).map((n) => n.target.join(" ")).join(" | ")}`);
      expect(report, report.join("\n")).toEqual([]);
    });
  }
});
