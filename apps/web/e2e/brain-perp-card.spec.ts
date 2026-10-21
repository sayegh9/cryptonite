import type { Page, Route } from "@playwright/test";
import { test, expect } from "./fixtures/offline";

/**
 * Perp order cards (leverage plan 4.3, 4.4, 4.8): the dry run's leverage plan on the card, the leverage pills (a pill
 * above the recommended leverage is disabled; picking one reads the plan again), TOTP on every perp approval, the
 * second confirmation at 8x and above, the ALLOW_PERPS and RAISE_MAX_LEVERAGE fixes (each spends its own code), a
 * refused plan, a short, and the pills locked once the card is approved. Every API call is mocked.
 */

const SESSION = "5f0c3a6e-1b2d-4c3e-9f00-0000000000a1";
const TRACE = "a91f0000-0000-4000-8000-0000000000a1";

const PERP_CARD = {
  kind: "order_card",
  id: "call-1",
  instrument_id: "BTCUSDT-PERP.BINANCE",
  side: "BUY",
  quantity: null,
  order_type: "MARKET",
  limit_price: null,
  allocation_pct: 2.5,
  stop_loss: 57000,
  take_profit: 66000,
  rationale: "4H reclaim of 60k; invalid below 57k.",
  risk_class: "ADD_RISK",
  status: "PENDING_APPROVAL",
  trace_id: TRACE,
  command_id: null,
  leverage: null,
  setup_grade: "A",
  grade_reasons: ["3 of 3 timeframes aligned (4h, 1h, 15m)", "reward/risk 2.0"],
};

/** The server's plan at a leverage (the numbers follow the API test: $250 margin, 5% stop) */
function plan(leverage: number, over: Record<string, unknown> = {}) {
  return {
    instrument_id: "BTCUSDT-PERP.BINANCE", side: "BUY", reject: null, ok: true, leverage,
    leverage_label: leverage === 7 ? "7x (risk at stop)" : `${leverage}x`, margin_mode: "ISOLATED",
    requested_leverage: null, allowed_max_leverage: 7, binding_ceiling: "risk at stop", capped: leverage === 7,
    ceilings: { "risk at stop": 7, "liquidation buffer": 9, exposure: 10, "mandate maximum": 10, "setup grade": 10, exchange: 125 },
    margin_usd: "250", notional_usd: String(250 * leverage), quantity: "0.029", entry_price: "60000", stop_price: "57000",
    stop_distance_pct: "5", liquidation_price: String(60000 * (1 - 1 / leverage + 0.004)), liquidation_distance_pct: String(100 / leverage - 0.4),
    buffer_used_pct: String((5.3 / (100 / leverage - 0.4)) * 100), loss_at_stop_usd: String(250 * leverage * 0.055),
    loss_at_stop_pct: String(250 * leverage * 0.055 / 100), loss_if_liquidated_usd: "250", funding_8h_usd: String(-0.025 * leverage),
    margin_cut: false, ...over,
  };
}

const okPreflight = (leverage: number, over: Record<string, unknown> = {}) => ({
  ok: true,
  mandate: { id: "mandate-perp", name: "Perps L1", status: "ACTIVE" },
  side: "BUY",
  allocation_pct: 2.5,
  budget_usd: 10000,
  clipped_from_pct: null,
  sell_quantity: null,
  summary: `LONG BTCUSDT-PERP.BINANCE MARKET ${leverage}x ISOLATED`,
  leverage_plan: plan(leverage),
  needs_totp: true,
  high_leverage_confirm: leverage >= 8,
  leverage_remedy: null,
  ...over,
});

function sse(events: [string, unknown][]): string {
  return events.map(([event, data]) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join("");
}

const turnWith = (card: Record<string, unknown>) => sse([
  ["session", { session_id: SESSION, trace_id: TRACE }],
  ["tool_call", { id: "call-1", tool: "propose_order", args: {}, phase: "start", status: "RUNNING" }],
  ["tool_call", { id: "call-1", tool: "propose_order", phase: "end", status: "DENIED" }],
  ["widget", { widget: card }],
  ["token", { text: "The perp card is waiting for your approval." }],
  ["done", { session_id: SESSION, message_id: 2, trace_id: TRACE, model: "google:gemini-2.5-pro", cost_usd: "0.0100",
    content: "The perp card is waiting for your approval." }],
]);

function collectReactErrors(page: Page): string[] {
  const errors: string[] = [];
  const reactError = /hydrat|did not match|Maximum update depth|Minified React error|Cannot update a component/i;
  page.on("pageerror", (err) => errors.push(`pageerror: ${err.message}`));
  page.on("console", (msg) => {
    if (msg.type() === "error" && reactError.test(msg.text())) errors.push(msg.text());
  });
  return errors;
}

interface Mocks {
  preflightLeverages: (string | null)[];
  approvals: any[];
  mandatePatches: { url: string; body: any }[];
}

/**
 * Mocks the Brain for one perp card. `preflight` answers each dry-run read with the leverage asked (null: the card's
 * own); `approve` answers each approval.
 */
async function openPerpCard(
  page: Page,
  card: Record<string, unknown>,
  preflight: (leverage: string | null) => unknown,
  approve: (body: any) => { status?: number; json: unknown } = () => ({
    json: { command_id: "cmd-perp-1", status: "APPROVED", leverage: 7, protection: {} },
  }),
): Promise<{ card: ReturnType<Page["getByTestId"]>; mocks: Mocks }> {
  const mocks: Mocks = { preflightLeverages: [], approvals: [], mandatePatches: [] };
  await page.route("**/api/v1/bars*", (route) => route.fulfill({ json: { bars: [], count: 0 } }));
  await page.route("**/api/v1/paper/positions*", (route) => route.fulfill({ json: [] }));
  await page.route("**/agent-api/brain/spend", (route) => route.fulfill({ json: { spent_usd: "1.00", cap_usd: "25.00" } }));
  await page.route(/\/agent-api\/brain\/sessions(\?.*)?$/, (route) => route.fulfill({ json: { sessions: [] } }));
  await page.route(`**/agent-api/brain/sessions/${SESSION}/notes`, (route) => route.fulfill({ json: { status: "ok" } }));
  await page.route(`**/agent-api/brain/sessions/${SESSION}/cards/*/outcome`, (route) =>
    route.fulfill({ json: { status: "APPROVED", state: "QUEUED" } }));
  await page.route(`**/agent-api/brain/sessions/${SESSION}/cards/*/preflight*`, (route) => {
    const lev = new URL(route.request().url()).searchParams.get("leverage");
    mocks.preflightLeverages.push(lev);
    return route.fulfill({ json: preflight(lev) });
  });
  await page.route(`**/agent-api/brain/sessions/${SESSION}/cards/*/approve`, (route) => {
    const body = route.request().postDataJSON();
    mocks.approvals.push(body);
    const reply = approve(body);
    return route.fulfill({ status: reply.status ?? 200, json: reply.json });
  });
  await page.route("**/api/v1/ai/mandates/*", (route) => {
    if (route.request().method() !== "PATCH") return route.fallback();
    mocks.mandatePatches.push({ url: new URL(route.request().url()).pathname, body: route.request().postDataJSON() });
    return route.fulfill({ json: { id: "mandate-perp", name: "Perps L1", instruments: ["BTCUSDT-PERP.BINANCE"], status: "ACTIVE" } });
  });
  await page.route("**/agent-api/brain/stream", (route: Route) =>
    route.fulfill({ status: 200, headers: { "content-type": "text/event-stream" }, body: turnWith(card) }),
  );
  await page.addInitScript(() => localStorage.setItem("csrf_token", "csrf-test"));
  await page.goto("/");
  await page.getByRole("button", { name: "AI Brain", exact: true }).click();
  await expect(page.getByTestId("brain-home")).toBeVisible();
  await page.getByRole("textbox", { name: "Message the Brain" }).fill("Propose a perp trade");
  await page.getByRole("button", { name: "Send to the Brain" }).click();
  return { card: page.getByTestId("brain-assistant-message").last().getByTestId("brain-widget-order-card"), mocks };
}

test.describe("Brain perp order card", () => {
  test("shows the leverage plan, and a lower pill reads the plan again", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    const { card, mocks } = await openPerpCard(page, PERP_CARD, (lev) => okPreflight(lev ? Number(lev) : 7));

    const panel = card.getByTestId("perp-plan");
    await expect(card.getByText("LONG", { exact: true })).toBeVisible();
    await expect(panel.getByTestId("perp-plan-pill")).toHaveText("7x ISOLATED");
    await expect(panel.getByTestId("perp-plan-label")).toHaveText("7x (risk at stop)");
    await expect(panel.getByTestId("perp-plan-margin")).toHaveText("$250.00");
    await expect(panel.getByTestId("perp-plan-notional")).toHaveText("$1,750.00");
    await expect(panel.getByTestId("perp-plan-loss-liq")).toHaveText("$250.00");
    await expect(panel.getByTestId("perp-plan-funding")).toHaveText("pays $0.175");
    await expect(panel.getByTestId("perp-plan-grade")).toHaveText("Grade A");
    await expect(panel.getByTestId("perp-plan-buffer")).toBeVisible();
    await expect(panel.getByTestId("perp-plan-ladder")).toBeVisible();
    // 7x is recommended; 10x is above the risk-at-stop ceiling
    await expect(panel.getByTestId("leverage-pill-7")).toHaveText("7x ★");
    await expect(panel.getByTestId("leverage-pill-10")).toBeDisabled();
    await expect(panel.getByTestId("leverage-pill-10")).toHaveAttribute("title", "Above the risk at stop ceiling (7x)");

    await panel.getByTestId("leverage-pill-5").click();
    await expect(panel.getByTestId("perp-plan-pill")).toHaveText("5x ISOLATED");
    await expect(panel.getByTestId("leverage-pill-5")).toHaveAttribute("aria-pressed", "true");
    expect(mocks.preflightLeverages).toContain("5");
    expect(reactErrors).toEqual([]);
  });

  test("approve needs a TOTP code, sends the picked leverage, and locks the pills", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    const { card, mocks } = await openPerpCard(page, PERP_CARD, (lev) => okPreflight(lev ? Number(lev) : 7),
      (body) => ({ json: { command_id: "cmd-perp-1", status: "APPROVED", leverage: body.leverage ?? 7, protection: {} } }));

    await card.getByTestId("perp-plan").getByTestId("leverage-pill-5").click();
    await expect(card.getByTestId("perp-plan-pill")).toHaveText("5x ISOLATED");
    await card.getByRole("button", { name: "Approve…" }).click();
    const group = card.getByRole("group", { name: "Confirm paper order" });
    await expect(group.getByTestId("card-confirm-loss-stop")).toHaveText("$68.75");
    await expect(group.getByTestId("card-confirm-loss-liq")).toHaveText("$250.00");
    const confirm = group.getByRole("button", { name: "Confirm paper order" });
    await expect(confirm).toBeDisabled();
    await group.getByLabel("TOTP code").fill("123456");
    await expect(confirm).toBeEnabled();
    await confirm.click();

    await expect(card.getByTestId("card-outcome")).toContainText("cmd-perp-1");
    expect(mocks.approvals).toEqual([{ totp_code: "123456", leverage: 5 }]);
    // Decided: the approved leverage stays, the pills are gone
    await expect(card.getByTestId("perp-plan-pill")).toHaveText("5x ISOLATED");
    await expect(card.getByTestId("leverage-pill-3")).toHaveCount(0);
    expect(reactErrors).toEqual([]);
  });

  test("8x and above needs a second confirmation", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    const nine = (over: Record<string, unknown> = {}) =>
      okPreflight(9, { leverage_plan: plan(9, { allowed_max_leverage: 9, leverage_label: "9x (liquidation buffer)" }), ...over });
    const { card, mocks } = await openPerpCard(page, { ...PERP_CARD, stop_loss: 59100 }, () => nine(),
      () => ({ json: { command_id: "cmd-perp-9", status: "APPROVED", leverage: 9, protection: {} } }));

    await expect(card.getByTestId("perp-plan-pill")).toHaveText("9x ISOLATED");
    await card.getByRole("button", { name: "Approve…" }).click();
    const group = card.getByRole("group", { name: "Confirm paper order" });
    await group.getByLabel("TOTP code").fill("123456");
    const confirm = group.getByRole("button", { name: "Confirm paper order" });
    await expect(confirm).toBeDisabled();
    await group.getByTestId("card-confirm-high-leverage").getByRole("checkbox").check();
    await expect(confirm).toBeEnabled();
    await confirm.click();
    await expect(card.getByTestId("card-outcome")).toContainText("cmd-perp-9");
    expect(mocks.approvals).toEqual([{ totp_code: "123456", confirm_high_leverage: true }]);
    expect(reactErrors).toEqual([]);
  });

  test("a mandate without perps: Allow perps spends the code, approve waits for the next one", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    let allowed = false;
    const { card, mocks } = await openPerpCard(page, PERP_CARD, () => allowed ? okPreflight(3, {
      leverage_plan: plan(3, { allowed_max_leverage: 3, binding_ceiling: "mandate maximum" }),
    }) : {
      ok: false, code: "MANDATE_PERPS_NOT_ALLOWED", detail: "MANDATE_PERPS_NOT_ALLOWED: Perps L1 does not allow perps",
      needs_mandate: false, needs_totp: true, leverage_plan: null,
      remedy: { kind: "ALLOW_PERPS", mandate_id: "mandate-perp", mandate_name: "Perps L1", autonomy_level: 1, max_leverage_after: 3 },
    });

    await expect(card.getByTestId("brain-card-preflight")).toContainText("to allow perps in Perps L1 (at 3x)");
    await card.getByRole("button", { name: "Fix and approve…" }).click();
    const group = card.getByRole("group", { name: "Confirm paper order" });
    await expect(group.getByTestId("card-fix-allow-perps")).toContainText("at most 3x");
    await group.getByLabel("TOTP code").fill("111111");
    allowed = true;
    await group.getByRole("button", { name: "Allow perps" }).click();

    await expect(card.getByTestId("card-fix-done")).toContainText("perps allowed (at most 3x, ISOLATED)");
    await expect(card.getByTestId("card-fix-done")).toContainText("Enter the next TOTP code to approve the order.");
    expect(mocks.mandatePatches).toEqual([{
      url: "/api/v1/ai/mandates/mandate-perp",
      body: { allow_perps: true, totp_code: "111111", source: `brain_card:${SESSION}:call-1` },
    }]);
    expect(mocks.approvals).toEqual([]);
    // The used code is gone; the next one approves
    await expect(group.getByLabel("TOTP code")).toHaveValue("");
    await group.getByLabel("TOTP code").fill("222222");
    await group.getByRole("button", { name: "Confirm paper order" }).click();
    await expect(card.getByTestId("card-outcome")).toContainText("cmd-perp-1");
    expect(mocks.approvals).toEqual([{ totp_code: "222222" }]);
    expect(reactErrors).toEqual([]);
  });

  test("the mandate maximum binds: the confirm step can raise it first", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    const { card, mocks } = await openPerpCard(page, { ...PERP_CARD, leverage: 7 }, () => okPreflight(3, {
      leverage_plan: plan(3, { allowed_max_leverage: 3, binding_ceiling: "mandate maximum", capped: true, leverage_label: "3x (mandate maximum)" }),
      leverage_remedy: { kind: "RAISE_MAX_LEVERAGE", mandate_id: "mandate-perp", mandate_name: "Perps L1", from: 3, to: 7 },
    }));

    await expect(card.getByTestId("brain-card-preflight")).toContainText("Perps L1 allows at most 3x");
    await card.getByRole("button", { name: "Approve…" }).click();
    const group = card.getByRole("group", { name: "Confirm paper order" });
    await group.getByTestId("card-fix-raise-leverage").getByRole("checkbox").check();
    await group.getByLabel("TOTP code").fill("333333");
    await group.getByRole("button", { name: "Raise max leverage" }).click();
    await expect(card.getByTestId("card-fix-done")).toContainText("max leverage raised 3x → 7x");
    expect(mocks.mandatePatches.map((p) => p.body)).toEqual([
      { max_leverage: 7, totp_code: "333333", source: `brain_card:${SESSION}:call-1` },
    ]);
    expect(mocks.approvals).toEqual([]);
    expect(reactErrors).toEqual([]);
  });

  test("a refused plan says which ceiling refused it", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    const { card } = await openPerpCard(page, PERP_CARD, () => ({
      ok: false, code: "LIQUIDATION_BUFFER_TOO_THIN", detail: "LIQUIDATION_BUFFER_TOO_THIN: perp sizing (liquidation buffer)",
      needs_mandate: false, needs_totp: true, remedy: null,
      leverage_plan: plan(0, { ok: false, reject: "LIQUIDATION_BUFFER_TOO_THIN", allowed_max_leverage: 0, binding_ceiling: "liquidation buffer" }),
    }));
    await expect(card.getByTestId("perp-plan-refused")).toHaveText("No leverage fits: LIQUIDATION_BUFFER_TOO_THIN (liquidation buffer)");
    await expect(card.getByRole("button", { name: "Approve anyway…" })).toBeVisible();
    expect(reactErrors).toEqual([]);
  });

  test("a short card", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    const { card } = await openPerpCard(page, { ...PERP_CARD, side: "SELL", stop_loss: 63000, take_profit: 54000 }, () =>
      okPreflight(7, { side: "SELL", leverage_plan: plan(7, { side: "SELL", liquidation_price: "68300", stop_price: "63000" }) }));
    await expect(card.getByText("SHORT", { exact: true })).toBeVisible();
    await expect(card.getByTestId("brain-card-preflight")).toContainText("Would open a short at 7x ISOLATED in Perps L1");
    await expect(card.getByText("ADDS RISK · PAPER")).toBeVisible();
    expect(reactErrors).toEqual([]);
  });
});
