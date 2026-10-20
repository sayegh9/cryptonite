import { test, expect, type Page, type WebSocketRoute } from "@playwright/test";

const PERP = "BTCUSDT-PERP.BINANCE";

/**
 * A confirmed operator session. The session is shared app-wide, and an operator-only endpoint that answers 401
 * (the mandate refresh does) marks it signed out, so the AI endpoints answer as signed in too.
 */
async function mockOperatorSession(page: Page) {
  await page.route("**/api/v1/auth/me", (route) =>
    route.fulfill({ status: 200, json: { authenticated: true, username: "op", csrf_token: "csrf-test" } }));
  await page.route("**/api/v1/ai/mandates", (route) => route.fulfill({ status: 200, json: { mandates: [] } }));
  await page.route("**/api/v1/ai/approvals*", (route) =>
    route.fulfill({ status: 200, json: { approvals: [], now: new Date().toISOString() } }));
  await page.route("**/api/v1/ai/status", (route) =>
    route.fulfill({ status: 200, json: { kill_switch: { latched: false, reason: null, latched_by: null }, halted_mandates: [], account: null } }));
  await page.route("**/api/v1/control/kill-switch/status*", (route) =>
    route.fulfill({ status: 200, json: { environment: "PAPER", account_id: "PAPER-ACCOUNT-001", is_latched: false, latch_generation: 1 } }));
}

/** Fail the test on React render/hydration errors (backend-offline network noise is ignored). */
function collectReactErrors(page: Page): string[] {
  const errors: string[] = [];
  const reactError = /hydrat|did not match|Maximum update depth|Minified React error|Cannot update a component/i;
  page.on("pageerror", (err) => errors.push(`pageerror: ${err.message}`));
  page.on("console", (msg) => {
    if (msg.type() === "error" && reactError.test(msg.text())) errors.push(msg.text());
  });
  return errors;
}

async function openTradeSidebar(page: Page) {
  await page.goto("/?ws=terminal");
  await page.getByRole("button", { name: "Show Trade Sidebar" }).click();
  const ticket = page.getByRole("form", { name: "Order entry" });
  const hud = page.getByRole("region", { name: "Margin risk" });
  await expect(ticket).toBeVisible();
  await expect(hud).toBeVisible();
  return { ticket, hud };
}

test.describe("Perpetuals cockpit: order ticket and margin HUD", () => {
  test("perp ticket: leverage, margin mode, reduce-only validation, pre-flight, and order payload", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    let orderBody: Record<string, unknown> | null = null;
    await page.route("**/api/v1/paper/orders", async (route) => {
      if (route.request().method() !== "POST") return route.fulfill({ status: 200, json: [] });
      orderBody = route.request().postDataJSON();
      await route.fulfill({ status: 200, json: { command_id: "cmd-12345678", command_type: "MANUAL_ORDER", status: "PENDING" } });
    });
    // The ticket only sends orders from a confirmed operator session, with the kill switch known and not latched
    await mockOperatorSession(page);

    const { ticket } = await openTradeSidebar(page);

    // SPOT -> PERP reveals the derivatives controls
    await expect(ticket.locator("#perp-leverage")).toHaveCount(0);
    await ticket.getByRole("button", { name: "PERP", exact: true }).click();
    await expect(ticket.getByText(PERP)).toBeVisible();
    const slider = ticket.locator("#perp-leverage");
    await expect(slider).toBeVisible();
    const presets = ticket.getByRole("group", { name: "Leverage presets" });
    // The account maximum is 10x (ADR 0009 D2): the 20x chip is gone
    for (const chip of ["1x", "2x", "3x", "5x", "7x", "10x"]) {
      await expect(presets.getByRole("button", { name: chip, exact: true })).toBeVisible();
    }
    await expect(presets.getByRole("button", { name: "20x", exact: true })).toHaveCount(0);

    // 10x chip drives the slider
    await presets.getByRole("button", { name: "10x", exact: true }).click();
    await expect(slider).toHaveValue("10");
    await expect(presets.getByRole("button", { name: "10x", exact: true })).toHaveAttribute("aria-pressed", "true");

    // ISOLATED margin mode
    const isolated = ticket.getByRole("group", { name: "Margin mode" }).getByRole("button", { name: "ISOLATED" });
    await isolated.click();
    await expect(isolated).toHaveAttribute("aria-pressed", "true");

    // Reduce-only with no open position is flagged and blocks submission
    const reduceOnly = ticket.getByRole("checkbox", { name: /Reduce-Only/ });
    await reduceOnly.check();
    await expect(ticket.getByText("No open position to reduce.")).toBeVisible();
    await expect(ticket.getByRole("button", { name: /^SUBMIT BUY/ })).toBeDisabled();
    await reduceOnly.uncheck();
    await expect(ticket.getByText("No open position to reduce.")).toHaveCount(0);

    // LIMIT 0.05 BTC @ 50,000, 10x ISOLATED: notional 2,500, IM 250,
    // P_liq = (2,500 - 250) / (0.05 * (1 - 0.004)) = 45,180.72
    await ticket.getByRole("group", { name: "Order type" }).getByRole("button", { name: "LIMIT" }).click();
    await ticket.getByLabel("Quantity").fill("0.05");
    await ticket.getByLabel("Limit Price").fill("50000");
    const estimate = ticket.locator('dl[aria-label="Pre-trade estimate"]');
    await expect(estimate).toContainText("2,500.00 USDT");
    await expect(estimate).toContainText("250.00 USDT");
    await expect(estimate).toContainText("45,180.72");

    await ticket.getByRole("button", { name: "SUBMIT BUY 10x" }).click();
    await expect(ticket.getByRole("status")).toContainText("Order queued (cmd-1234)");
    expect(orderBody).toEqual({
      instrument_id: PERP,
      side: "BUY",
      order_type: "LIMIT",
      quantity: 0.05,
      price: 50000,
      leverage: 10,
      margin_mode: "ISOLATED",
      reduce_only: false,
    });
    expect(reactErrors).toEqual([]);
  });

  test("perp ticket: the dispatcher's preview disables refused pills, and an attached stop is sent", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    let orderBody: Record<string, unknown> | null = null;
    const previews: URLSearchParams[] = [];
    await page.route("**/api/v1/paper/orders", async (route) => {
      if (route.request().method() !== "POST") return route.fulfill({ status: 200, json: [] });
      orderBody = route.request().postDataJSON();
      await route.fulfill({ status: 200, json: { command_id: "cmd-87654321", command_type: "MANUAL_ORDER", status: "PENDING" } });
    });
    await page.route("**/api/v1/paper/perp/plan*", (route) => {
      const q = new URL(route.request().url()).searchParams;
      previews.push(q);
      const lev = Number(q.get("leverage"));
      const pills: Record<string, string | null> = {};
      for (let l = 1; l <= 10; l++) pills[String(l)] = q.get("stop_loss") && l > 6 ? "LIQUIDATION_BUFFER_TOO_THIN" : null;
      return route.fulfill({
        json: {
          ok: pills[String(lev)] === null, reject: pills[String(lev)], leverage: lev, pills,
          recommended_leverage: q.get("stop_loss") ? 6 : 10, margin_usd: String(2500 / lev), notional_usd: "2500",
          liquidation_price: "45180.72", liquidation_distance_pct: "9.6", buffer_used_pct: q.get("stop_loss") ? "43" : null,
          loss_at_stop_usd: q.get("stop_loss") ? "107.5" : null, loss_at_stop_pct: q.get("stop_loss") ? "1.08" : null,
          loss_if_liquidated_usd: String(2500 / lev), funding_8h_usd: "-0.25",
        },
      });
    });
    await mockOperatorSession(page);
    const { ticket } = await openTradeSidebar(page);
    await ticket.getByRole("button", { name: "PERP", exact: true }).click();
    const presets = ticket.getByRole("group", { name: "Leverage presets" });
    await presets.getByRole("button", { name: "5x", exact: true }).click();
    await ticket.getByRole("group", { name: "Order type" }).getByRole("button", { name: "LIMIT" }).click();
    await ticket.getByLabel("Quantity").fill("0.05");
    await ticket.getByLabel("Limit Price").fill("50000");

    // Attach a stop: the preview reads the stop, 7x and 10x would be refused, 6x is recommended
    await ticket.getByRole("checkbox", { name: "Attach stop" }).check();
    await expect(ticket.getByRole("button", { name: "SUBMIT BUY 5x" })).toBeDisabled(); // no stop price yet
    await ticket.getByLabel("Stop price").fill("48000");
    const plan = ticket.getByRole("group", { name: "Leverage plan" });
    await expect(plan).toContainText("5x ISOLATED");
    await expect(plan).toContainText("$107.50 (1.08% of equity)");
    await expect(presets.getByRole("button", { name: "6x ★", exact: true })).toBeVisible();
    await expect(presets.getByRole("button", { name: "10x", exact: true })).toBeDisabled();
    await expect(presets.getByRole("button", { name: "10x", exact: true })).toHaveAttribute("title", "Refused at 10x: LIQUIDATION_BUFFER_TOO_THIN");
    expect(previews.at(-1)?.get("stop_loss")).toBe("48000");

    await ticket.getByRole("button", { name: "SUBMIT BUY 5x" }).click();
    await expect(ticket.getByRole("status")).toContainText("Order queued (cmd-8765)");
    expect(orderBody).toEqual({
      instrument_id: PERP, side: "BUY", order_type: "LIMIT", quantity: 0.05, price: 50000, leverage: 5,
      margin_mode: "ISOLATED", reduce_only: false, stop_loss: 48000,
    });
    expect(reactErrors).toEqual([]);
  });

  test("header: Flatten perps shows while a perp is open and needs a TOTP code", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    const bodies: unknown[] = [];
    await page.route("**/api/v1/paper/positions*", (route) =>
      route.fulfill({
        status: 200,
        json: [
          { instrument_id: PERP, side: "LONG", quantity: "0.04", status: "OPEN" },
          { instrument_id: "BTCUSDT.BINANCE", side: "LONG", quantity: "0.1", status: "OPEN" },
        ],
      }));
    await page.route("**/api/v1/paper/perps/flatten", (route) => {
      bodies.push(route.request().postDataJSON());
      return route.fulfill({ status: 200, json: { closed: 1, commands: [] } });
    });
    await mockOperatorSession(page);
    await page.addInitScript(() => localStorage.setItem("csrf_token", "csrf-test"));
    await page.goto("/?ws=terminal");

    const button = page.getByRole("button", { name: "Flatten perps (1)" });
    await expect(button).toBeVisible();
    await button.click();
    const form = page.getByRole("form", { name: "Flatten perps" });
    await expect(form).toContainText("Closes BTCUSDT-PERP with reduce-only market orders");
    const submit = form.getByRole("button", { name: "Flatten 1 perp" });
    await expect(submit).toBeDisabled();
    await form.getByLabel("TOTP code").fill("123456");
    await submit.click();
    await expect(form).toHaveCount(0);
    expect(bodies).toEqual([{ totp_code: "123456" }]);
    expect(reactErrors).toEqual([]);
  });

  test("margin HUD: live marks drive risk bands, funding countdown, and liquidation banner", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    // Isolated long 0.04 BTC @ 50,000, 10x -> IM 200, P_liq 45,180.72, MM 8 at 2,000 notional
    await page.route("**/api/v1/paper/positions*", (route) =>
      route.fulfill({
        status: 200,
        json: [{
          instrument_id: PERP, side: "LONG", quantity: "0.04", entry_price: "50000", current_price: "50000",
          leverage: 10, margin_mode: "ISOLATED", isolated_margin: "200", liquidation_price: "45180.72289157",
          maintenance_margin: "8", unrealized_pnl: "0", status: "OPEN",
        }],
      }),
    );
    await page.route("**/api/v1/paper/accounts*", (route) => route.fulfill({ status: 200, json: [{ total_equity: "10000" }] }));

    // Mock the /ws/stream gateway: ack subscriptions, then let the test push perp events
    let gateway: WebSocketRoute | null = null;
    let perpSubscribed = false;
    await page.routeWebSocket("**/ws/stream", (ws) => {
      gateway = ws;
      ws.onMessage((raw) => {
        const msg = JSON.parse(String(raw));
        if (msg.action !== "subscribe") return;
        ws.send(JSON.stringify({ status: "subscribed", channels: msg.channels, instruments: msg.instruments ?? [] }));
        if ((msg.channels ?? []).includes("perpetuals")) perpSubscribed = true;
      });
    });
    const push = (type: string, data: Record<string, unknown>) => gateway!.send(JSON.stringify({ type, data }));
    const markTick = (price: string) => ({
      type: "perp.mark", v: 1, instrument_id: PERP, symbol: "BTCUSDT", mark_price: price, index_price: price,
      funding_rate: "0.0001", ts_event_ns: Date.now() * 1e6, next_funding_time_ns: (Date.now() + 3_600_000) * 1e6,
    });

    const { hud } = await openTradeSidebar(page);
    await expect.poll(() => perpSubscribed).toBe(true);
    await expect(hud.getByText("LONG 0.04")).toBeVisible();

    // Mark 50,000: 9.64% from liquidation -> CAUTION; margin ratio 8 / 200 = 4.00%
    push("perp_mark_tick", markTick("50000"));
    await expect(hud).toContainText("CAUTION · 9.64%");
    await expect(hud).toContainText("4.00%");
    await expect(hud).toContainText("longs pay");
    await expect(hud.getByLabel("Time to next funding")).toHaveText(/^00:[0-5]\d:[0-5]\d$/);
    const meter = hud.getByRole("meter");
    await expect(meter).toHaveAttribute("aria-valuetext", /CAUTION/);

    // Mark 47,000: 3.87% -> DANGER
    push("perp_mark_tick", markTick("47000"));
    await expect(hud).toContainText("DANGER · 3.87%");
    await expect(meter).toHaveAttribute("aria-valuetext", /DANGER/);

    // Liquidation notice -> banner in the HUD
    push("perp_liquidation", {
      type: "perp.liquidation", v: 1, client_order_id: "LIQ-1", instrument_id: PERP, side: "LONG",
      quantity: "0.04", mark_price: "45180", fill_price: "45180", liquidation_price: "45180.72",
      realized_pnl: "-192.8", trading_fee: "1.8072", clearance_fee: "9.036", insurance_fund_deficit: "3.6432",
    });
    const banner = hud.getByRole("alert");
    await expect(banner).toContainText("LIQUIDATED BTCUSDT-PERP LONG");
    await banner.getByRole("button", { name: "Dismiss liquidation alert" }).click();
    await expect(hud.getByRole("alert")).toHaveCount(0);
    expect(reactErrors).toEqual([]);
  });

  test("trade sidebar kill switch targets the PAPER engine", async ({ page }) => {
    const statusQueries: string[] = [];
    let tripBody: Record<string, unknown> | null = null;
    await mockOperatorSession(page);
    await page.route("**/api/v1/control/kill-switch/status*", (route) => {
      statusQueries.push(new URL(route.request().url()).search);
      route.fulfill({ status: 200, json: { environment: "PAPER", account_id: "PAPER-ACCOUNT-001", is_latched: tripBody !== null } });
    });
    await page.route("**/api/v1/control/kill-switch/trip", async (route) => {
      tripBody = route.request().postDataJSON();
      await route.fulfill({ status: 200, json: { success: true, kill_switch_latched: true } });
    });
    page.on("dialog", (dialog) => dialog.accept());

    await openTradeSidebar(page);
    await expect(page.getByTestId("kill-switch-paper")).toBeVisible();
    expect(statusQueries[0]).toContain("environment=PAPER");
    expect(statusQueries[0]).toContain("account_id=PAPER-ACCOUNT-001");

    await page.getByRole("button", { name: "TRIP KILL-SWITCH" }).click();
    await expect.poll(() => tripBody).not.toBeNull();
    expect(tripBody).toMatchObject({ environment: "PAPER", account_id: "PAPER-ACCOUNT-001" });
  });
});
