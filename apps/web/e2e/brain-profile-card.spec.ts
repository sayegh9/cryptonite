import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";
import { test, expect } from "./fixtures/offline";
import { mockAutoTrade, VALID_TOTP, type Backend } from "./fixtures/autotrade";

/**
 * P3 profile proposal card (D-P3-5): created only from the read-only tool's result; Create goes through the existing
 * POST /ai/mandates with TOTP, a Veto proposal raises the mode with a second code, an INVALID proposal only opens the
 * wizard, a refused start is reported. Every API call is mocked (P2's Auto-Trade fixture plus a stubbed Brain stream).
 */

const SESSION = "5f0c3a6e-1b2d-4c3e-9f00-0000000000b1";
const TRACE = "a91f0000-0000-4000-8000-0000000000b1";
/** Gate L screenshots are written only when EVIDENCE_DIR is set (the lead's local run), never in CI. */
const EVIDENCE = process.env.EVIDENCE_DIR;
const shot = async (page: Page, name: string) => { if (EVIDENCE) await page.screenshot({ path: `${EVIDENCE}/${name}.png` }); };

const SPEC = {
  name: "BTC ETH swing", coins: ["BTCUSDT", "ETHUSDT"], markets: ["PERP"], capital_budget_usd: "1000",
  style: "SWING", direction: "BOTH", max_position_pct: "10", max_leverage: 3, max_open_positions: 3,
  max_debates_per_day: 6, max_risk_per_trade_pct: "1", daily_loss_limit_pct: "3", daily_loss_defaulted: true,
  max_drawdown_pct: "20", exit_tp_r: "2", exit_tp1_scale_pct: "50", exit_trail_activate_pct: null, exit_trail_pct: null,
  exit_max_hold_hours: null, notify_level: "IMPORTANT", breaker_max_loss_streak: 4, breaker_max_reject_streak: 5,
  mode: "ASK", dry_run: true,
};

const card = (id: string, over: Record<string, unknown> = {}, spec: Record<string, unknown> = {}) => ({
  kind: "profile_proposal", id, status: "PROPOSED", errors: [], feed_status: "known",
  warnings: ["Negative replay: -0.24 average R over 34 trades for BTCUSDT / swing-trend-pullback-1h. This replay lost on average."],
  proposal: { ...SPEC, ...spec }, ...over,
});

const sse = (events: [string, unknown][]) =>
  events.map(([event, data]) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join("");

/** Opens the Brain and sends one turn whose stream renders the given proposal widgets. */
async function openCards(page: Page, widgets: Record<string, unknown>[], opts: { masterSwitch?: "ON" | "OFF" } = {}): Promise<Backend> {
  const be = await mockAutoTrade(page);
  if (opts.masterSwitch) {
    await page.route("**/api/v1/ai/profiles/estimate*", (route) =>
      route.fulfill({ json: { coins: [], master_switch: opts.masterSwitch, feed: { ingest_max_symbols: 20, coins_in_feed: [], not_in_feed: [] }, estimate: {} } }));
  }
  await page.route("**/api/v1/bars*", (route) => route.fulfill({ json: { bars: [], count: 0 } }));
  await page.route("**/api/v1/paper/positions*", (route) => route.fulfill({ json: [] }));
  await page.route("**/agent-api/brain/spend", (route) => route.fulfill({ json: { spent_usd: "1.00", cap_usd: "25.00" } }));
  await page.route(/\/agent-api\/brain\/sessions(\?.*)?$/, (route) => route.fulfill({ json: { sessions: [] } }));
  await page.route(`**/agent-api/brain/sessions/${SESSION}/notes`, (route) => route.fulfill({ json: { status: "ok" } }));
  const events: [string, unknown][] = [["session", { session_id: SESSION, trace_id: TRACE }]];
  for (const w of widgets) {
    events.push(["tool_call", { id: w.id, tool: "propose_profile", args: {}, phase: "start", status: "RUNNING" }]);
    events.push(["tool_call", { id: w.id, tool: "propose_profile", phase: "end", status: "OK" }]);
    events.push(["widget", { widget: w }]);
  }
  events.push(["token", { text: "Here is the proposal. Nothing is created until you press Create profile." }]);
  events.push(["done", { session_id: SESSION, message_id: 2, trace_id: TRACE, model: "google:gemini-2.5-pro", cost_usd: "0.0100",
    content: "Here is the proposal. Nothing is created until you press Create profile." }]);
  await page.route("**/agent-api/brain/stream", (route) =>
    route.fulfill({ status: 200, headers: { "content-type": "text/event-stream" }, body: sse(events) }));
  await page.goto("/");
  await page.getByRole("button", { name: "AI Brain", exact: true }).click();
  await expect(page.getByTestId("brain-home")).toBeVisible();
  await page.getByRole("textbox", { name: "Message the Brain" }).fill("Run a swing bot on BTC/ETH perps, both ways, max 3x");
  await page.getByRole("button", { name: "Send to the Brain" }).click();
  return be;
}

const proposal = (page: Page, name = "BTC ETH swing") => page.getByRole("region", { name: `Profile proposal for ${name}` });

test.describe("Brain profile proposal card", () => {
  test("Create asks for TOTP, retries a wrong code and posts the wizard's exact body (keyboard only)", async ({ page }) => {
    const be = await openCards(page, [card("call-1")]);
    const c = proposal(page);
    await expect(c.getByRole("status")).toHaveText("This is a proposal. No profile exists yet.");
    await expect(c.getByText("3% (default: 3 × risk per trade, capped at maximum drawdown)")).toBeVisible();
    await expect(c.getByRole("list", { name: "Proposal warnings" })).toContainText("Negative replay");
    await shot(page, "1440-proposed");

    await c.getByRole("button", { name: "Create profile" }).focus();
    await page.keyboard.press("Enter");
    const totp = page.getByRole("dialog", { name: "Confirm this risk change" });
    await expect(totp.getByLabel("TOTP code")).toBeFocused();
    await page.keyboard.type("000000");
    await page.keyboard.press("Enter");
    await expect(totp.getByRole("alert")).toContainText("That code wasn't accepted");
    await totp.getByLabel("TOTP code").fill(VALID_TOTP);
    await page.keyboard.press("Enter");
    await expect(c.getByRole("status")).toHaveText("Created BTC ETH swing. Started in Ask me. Dry run is on.");

    const creates = be.writes.filter((w) => w.method === "POST" && w.path === "/mandates");
    expect(creates).toHaveLength(2); // the refused code, then the accepted one
    expect(creates[1].body).toEqual({
      name: "BTC ETH swing", capital_budget_usd: "1000",
      instruments: ["BTCUSDT-PERP.BINANCE", "ETHUSDT-PERP.BINANCE"], max_position_pct: "10", markets: ["PERP"],
      allow_perps: true, style: "SWING", direction: "BOTH", environment: "PAPER", max_risk_per_trade_pct: "1",
      max_drawdown_pct: "20", max_leverage: 3, max_open_positions: 3, max_debates_per_day: 6, exit_tp_r: "2",
      exit_tp1_scale_pct: "50", notify_level: "IMPORTANT", breaker_max_loss_streak: 4, breaker_max_reject_streak: 5,
      dry_run: true, daily_loss_limit_pct: "3", totp_code: VALID_TOTP, strategies: [], timeframes: [],
    });
    await page.setViewportSize({ width: 390, height: 900 });
    await c.scrollIntoViewIfNeeded();
    await shot(page, "390-created");
    await c.getByRole("button", { name: "Open profile" }).click();
    await expect(page).toHaveURL(/ws=autotrade/);
    await expect(page).toHaveURL(/id=cccccccc-3333-4333-8333-333333333333/);
  });

  test("a Veto window proposal is created in Ask me, then raised with a second code", async ({ page }) => {
    const be = await openCards(page, [card("call-1", {}, { mode: "VETO" })], { masterSwitch: "ON" });
    const c = proposal(page);
    await expect(c.getByText("A second TOTP code is needed to switch to Veto window.")).toBeVisible();
    await c.getByRole("button", { name: "Create profile" }).click();
    const totp = page.getByRole("dialog", { name: "Confirm this risk change" });
    await totp.getByLabel("TOTP code").fill(VALID_TOTP);
    await totp.getByRole("button", { name: "Create profile" }).click();
    await expect(c.getByRole("status")).toContainText("Enter a second TOTP code to switch to Veto window.");
    await totp.getByLabel("TOTP code").fill(VALID_TOTP);
    await totp.getByRole("button", { name: "Set Veto window" }).click();
    await expect(c.getByRole("status")).toHaveText("BTC ETH swing is now in Veto window.");
    const raise = be.writes.find((w) => w.path.endsWith("/autonomy/confirm"));
    expect(raise?.body).toMatchObject({ level: 2, totp_code: VALID_TOTP });
    expect(be.writes.filter((w) => w.method === "POST" && w.path === "/mandates")).toHaveLength(1);
  });

  test("an INVALID proposal only opens the wizard prefilled; a refused start is reported", async ({ page }) => {
    const invalid = card("call-1", { status: "INVALID", errors: ["SHORT_NEEDS_MARGIN_OR_PERP"] },
      { name: "XRP short", coins: ["XRPUSDT"], markets: ["SPOT"], direction: "SHORT", max_leverage: 1 });
    const be = await openCards(page, [invalid, card("call-2", {}, { name: "ETH refused" })]);
    await page.route("**/api/v1/ai/mandates", (route) => route.request().method() !== "POST" ? route.fallback() :
      route.fulfill({ status: 201, json: { id: "dddddddd-4444-4444-8444-444444444444", name: "ETH refused", status: "STOPPED",
        started: false, start_error: { code: "COIN_LOCKED", detail: "COIN_LOCKED: ETH is used by profile BTC swing paper; pause or stop it first" } } }));

    const bad = proposal(page, "XRP short");
    await expect(bad.getByRole("alert")).toHaveText("This proposal needs changes before it can be created.");
    await expect(bad.getByRole("list", { name: "Proposal errors" })).toContainText("Short profiles need Spot Margin or Perpetuals");
    await expect(bad.getByRole("button", { name: "Create profile" })).toHaveCount(0);
    await page.setViewportSize({ width: 390, height: 900 });
    await bad.scrollIntoViewIfNeeded();
    await shot(page, "390-invalid");

    const refused = proposal(page, "ETH refused");
    await refused.getByRole("button", { name: "Create profile" }).click();
    const totp = page.getByRole("dialog", { name: "Confirm this risk change" });
    await totp.getByLabel("TOTP code").fill(VALID_TOTP);
    await totp.getByRole("button", { name: "Create profile" }).click();
    await expect(refused.getByRole("alert")).toContainText("Created ETH refused, but it couldn't start: ETH is used by profile BTC swing paper.");

    await bad.getByRole("button", { name: "Review in wizard" }).click();
    await expect(page).toHaveURL(/ws=autotrade/);
    await expect(page.getByLabel("Profile name")).toHaveValue("XRP short");
    expect(be.writes.some((w) => w.method === "POST" && w.path === "/mandates" && w.body.name === "XRP short")).toBe(false);
  });

  test("the card has no serious accessibility violations and no overflow at 390 and 1440 px", async ({ page }) => {
    await openCards(page, [card("call-1")]);
    for (const width of [1440, 390]) {
      await page.setViewportSize({ width, height: 900 });
      await expect(proposal(page)).toBeVisible();
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      expect(overflow, `horizontal overflow at ${width}px`).toBeLessThanOrEqual(0);
      const results = await new AxeBuilder({ page }).include("section[aria-label^='Profile proposal for']")
        .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
      expect(results.violations.filter((v) => v.impact === "serious" || v.impact === "critical")).toEqual([]);
    }
    await proposal(page).scrollIntoViewIfNeeded();
    await shot(page, "390-proposed");
  });
});
