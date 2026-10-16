import type { Page, Route } from "@playwright/test";
import { test, expect } from "./fixtures/offline";

/**
 * Brain Home (blueprint A3.3–A3.5) with a mocked SSE stream:
 * analyze → order card → approve (server-side card approval) → decision trace; reload recall; stop; brain-ask.
 */

const SESSION = "5f0c3a6e-1b2d-4c3e-9f00-000000000001";
const TRACE = "a91f0000-0000-4000-8000-000000000042";

const ORDER_CARD = {
  kind: "order_card",
  id: "call-1",
  instrument_id: "BTCUSDT.BINANCE",
  side: "BUY",
  quantity: 0.01,
  order_type: "MARKET",
  limit_price: null,
  allocation_pct: 5,
  stop_loss: 58000,
  take_profit: null,
  rationale: "4H reclaim of 60k; invalid below 58k.",
  risk_class: "ADD_RISK",
  status: "PENDING_APPROVAL",
  trace_id: TRACE,
  command_id: null,
};

function sse(events: [string, unknown][]): string {
  return events.map(([event, data]) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join("");
}

const turnWith = (card: Record<string, unknown>) => sse([
  ["session", { session_id: SESSION, trace_id: TRACE }],
  ["tool_call", { id: "call-0", tool: "show_price_chart", args: { symbol: "BTCUSDT", timeframe: "4h" }, phase: "start", status: "RUNNING" }],
  ["tool_call", { id: "call-0", tool: "show_price_chart", phase: "end", status: "OK" }],
  ["widget", { widget: { kind: "price_chart", id: "call-0", symbol: "BTCUSDT", timeframe: "4h" } }],
  ["token", { text: "BTC reclaimed 60k on the 4H. " }],
  ["tool_call", { id: "call-1", tool: "propose_order", args: {}, phase: "start", status: "RUNNING" }],
  ["tool_call", { id: "call-1", tool: "propose_order", phase: "end", status: "DENIED" }],
  ["widget", { widget: card }],
  ["token", { text: "The order card is waiting for your approval." }],
  [
    "done",
    {
      session_id: SESSION,
      message_id: 2,
      trace_id: TRACE,
      model: "google:gemini-2.5-pro",
      cost_usd: "0.0123",
      content: "BTC reclaimed 60k on the 4H. The order card is waiting for your approval.",
    },
  ],
]);
const TURN = turnWith(ORDER_CARD);

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

async function mockBackend(page: Page) {
  const calls = { stream: [] as any[], orders: [] as any[], notes: [] as any[], history: 0 };
  await page.route("**/api/v1/bars*", (route) => {
    const now = Math.floor(Date.now() / 1000);
    const bars = Array.from({ length: 40 }, (_, i) => ({ time: now - (40 - i) * 3600, close: String(58000 + i * 60) }));
    return route.fulfill({ json: { symbol: "BTCUSDT", timeframe: "4h", bars, count: bars.length } });
  });
  await page.route("**/api/v1/paper/positions*", (route) => route.fulfill({ json: [] }));
  await page.route("**/agent-api/brain/spend", (route) => route.fulfill({ json: { spent_usd: "3.10", cap_usd: "25.00" } }));
  await page.route(/\/agent-api\/brain\/sessions(\?.*)?$/, (route) =>
    route.fulfill({ json: { sessions: [{ id: SESSION, title: "Analyze BTC", created_at: "2026-09-25T10:00:00Z", updated_at: "2026-09-25T10:01:00Z" }] } }),
  );
  await page.route(`**/agent-api/brain/sessions/${SESSION}`, (route) => {
    calls.history++;
    return route.fulfill({
      json: {
        session: { id: SESSION, title: "Analyze BTC" },
        summary: null,
        messages: [
          { id: 1, role: "user", content: "Analyze BTC and propose a trade", widgets: [], trace_id: null, created_at: null },
          {
            id: 2,
            role: "assistant",
            content: "BTC reclaimed 60k on the 4H. The order card is waiting for your approval.",
            widgets: [{ ...ORDER_CARD, status: "APPROVED", command_id: "cmd-12345678" }],
            trace_id: TRACE,
            created_at: null,
          },
          { id: 3, role: "system", content: "Operator approved order card call-1. Paper command cmd-12345678 queued.", widgets: [], trace_id: null, created_at: null },
        ],
      },
    });
  });
  await page.route(`**/agent-api/brain/sessions/${SESSION}/notes`, (route) => {
    calls.notes.push(route.request().postDataJSON());
    return route.fulfill({ json: { status: "ok" } });
  });
  await page.route("**/agent-api/brain/traces/*", (route) =>
    route.fulfill({
      json: {
        trace: {
          id: TRACE, parent_id: null, role: "brain.plan", model: "google:gemini-2.5-pro", session_id: SESSION, mandate_id: null,
          status: "OK", error: null, cost_usd: "0.012300", latency_ms: 4200, created_at: "2026-09-25T10:01:00Z",
          tool_calls: [
            { tool: "show_price_chart", risk: "READ", args: { symbol: "BTCUSDT" }, status: "OK" },
            { tool: "propose_order", risk: "ADD_RISK", args: { side: "BUY", quantity: 0.01 }, status: "DENIED" },
          ],
          output: "BTC reclaimed 60k on the 4H.",
        },
        parent: null,
        children: [],
      },
    }),
  );
  await page.route(`**/agent-api/brain/sessions/${SESSION}/cards/*/approve`, async (route) => {
    calls.orders.push({ url: new URL(route.request().url()).pathname, csrf: route.request().headers()["x-csrf-token"] });
    return route.fulfill({
      json: { command_id: "cmd-12345678", status: "APPROVED", protection: { stop_loss_pct: 5, trail_activate_pct: 3, trail_pct: 2 } },
    });
  });
  await page.route("**/agent-api/brain/stream", (route: Route) => {
    calls.stream.push(route.request().postDataJSON());
    return route.fulfill({ status: 200, headers: { "content-type": "text/event-stream" }, body: TURN });
  });
  return calls;
}

async function openBrainHome(page: Page) {
  await page.addInitScript(() => localStorage.setItem("csrf_token", "csrf-test"));
  await page.goto("/");
  await page.getByRole("button", { name: "AI Brain", exact: true }).click();
  await expect(page.getByTestId("brain-home")).toBeVisible();
}

test.describe("Brain Home", () => {
  test("analyze → order card → server-side approval → decision trace", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    const calls = await mockBackend(page);
    await openBrainHome(page);

    // Context rail
    await expect(page.getByRole("meter", { name: "AI spend today" })).toHaveAttribute("aria-valuetext", "$3.10 of $25.00 daily cap");
    await expect(page.getByTestId("brain-rail-mandates")).toContainText("L1 Copilot");
    await expect(page.getByText("Flat. No open positions.")).toBeVisible();

    const input = page.getByRole("textbox", { name: "Message the Brain" });
    await input.fill("Analyze BTC and propose a trade");
    await page.getByRole("button", { name: "Send to the Brain" }).click();

    const answer = page.getByTestId("brain-assistant-message").last();
    await expect(answer).toContainText("The order card is waiting for your approval.");
    await expect(answer.getByTestId("brain-widget-price-chart")).toContainText("BTCUSDT · 4h");
    await expect(answer.getByRole("img", { name: /BTCUSDT price/ })).toBeVisible();
    await expect(answer.getByText("held for approval")).toBeVisible();
    expect(calls.stream[0]).toEqual({ message: "Analyze BTC and propose a trade", session_id: null });

    // The order card: risk-adding is labelled, not only coloured
    const card = answer.getByTestId("brain-widget-order-card");
    await expect(card).toContainText("ADDS RISK · PAPER");
    await expect(card).toContainText("BTCUSDT.BINANCE");
    // the size shown is the allocation that is approved, never the model's own quantity guess
    await expect(card).toContainText("5% of mandate");
    await expect(card).not.toContainText("0.01");
    await card.getByRole("button", { name: "Approve…" }).click();
    // The confirmation says how the order is sized and what actually stops it out
    await expect(card.getByRole("group", { name: "Confirm paper order" })).toContainText("sized at 5% of the mandate");
    await expect(card.getByRole("group", { name: "Confirm paper order" })).toContainText("exit monitor");
    await card.getByRole("button", { name: "Confirm paper order" }).click();
    await expect(card.getByRole("status")).toContainText("Approved · sending the order · paper command cmd-12345678");
    // One server-side call: the API claims the card, queues the order and records the note itself
    expect(calls.orders).toEqual([{ url: `/agent-api/brain/sessions/${SESSION}/cards/call-1/approve`, csrf: "csrf-test" }]);
    expect(calls.notes).toEqual([]);

    // Every order card links to its decision trace
    await card.getByRole("button", { name: /Open the decision trace/ }).click();
    const drawer = page.getByRole("dialog", { name: "Decision trace" });
    await expect(drawer).toBeVisible();
    await expect(drawer).toContainText("Tool · propose_order");
    await expect(drawer).toContainText("ADD_RISK held for approval");
    await expect(drawer).toContainText("Paper command cmd-12345678 queued for GuardedOrderDispatcher.");
    await page.keyboard.press("Escape");
    await expect(drawer).toBeHidden();

    // The next turn continues the same server session
    await input.fill("thanks");
    await page.getByRole("button", { name: "Send to the Brain" }).click();
    await expect.poll(() => calls.stream.length).toBe(2);
    expect(calls.stream[1]).toEqual({ message: "thanks", session_id: SESSION });

    expect(reactErrors).toEqual([]);
  });

  test("an entry with no mandate: the card creates one inline with TOTP, then approves it", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    await mockBackend(page);
    const mandateBodies: any[] = [];
    const orderBodies: any[] = [];
    await page.route("**/api/v1/ai/mandates", (route) => {
      if (route.request().method() !== "POST") return route.fulfill({ json: { mandates: [] } });
      mandateBodies.push(route.request().postDataJSON());
      return route.fulfill({ status: 201, json: { id: "mandate-1", name: "Brain BTCUSDT.BINANCE", instruments: ["BTCUSDT.BINANCE"], status: "ACTIVE" } });
    });
    // Registered after mockBackend, so it takes precedence: without a mandate the API refuses (409) before claiming the card
    await page.route(`**/agent-api/brain/sessions/${SESSION}/cards/*/approve`, (route) => {
      const body = route.request().postDataJSON();
      orderBodies.push(body);
      if (!body?.mandate_id) {
        return route.fulfill({
          status: 409,
          json: { detail: "NO_MANDATE: approve this card inside an ACTIVE mandate that allows BTCUSDT.BINANCE (Mission Control → Mandates)" },
        });
      }
      return route.fulfill({ json: { command_id: "cmd-12345678", status: "APPROVED", mandate_id: body.mandate_id } });
    });
    await openBrainHome(page);
    await page.getByRole("textbox", { name: "Message the Brain" }).fill("Analyze BTC and propose a trade");
    await page.getByRole("button", { name: "Send to the Brain" }).click();

    const card = page.getByTestId("brain-assistant-message").last().getByTestId("brain-widget-order-card");
    await card.getByRole("button", { name: "Approve…" }).click();
    await card.getByRole("button", { name: "Confirm paper order" }).click();

    // The refusal turns into the mandate form on the same card, not a trip to Mission Control
    const group = card.getByRole("group", { name: "Confirm paper order" });
    await expect(group).toContainText("No ACTIVE mandate allows BTCUSDT.BINANCE yet");
    const create = group.getByRole("button", { name: "Create mandate and confirm" });
    await expect(create).toBeDisabled();
    await group.getByLabel("TOTP code").fill("123456");
    await expect(create).toBeEnabled();
    await create.click();

    await expect(card.getByRole("status")).toContainText("Approved · sending the order · paper command cmd-12345678");
    // The API checks the TOTP code; the mandate starts at L1 with the editor's defaults, on this instrument only
    expect(mandateBodies).toEqual([
      {
        name: "Brain BTCUSDT.BINANCE", capital_budget_usd: "10000", instruments: ["BTCUSDT.BINANCE"], max_position_pct: 10,
        max_drawdown_pct: 20, veto_window_s: 600, allow_perps: false, totp_code: "123456",
      },
    ]);
    expect(orderBodies).toHaveLength(2);
    expect(orderBodies[0]?.mandate_id).toBeUndefined();
    expect(orderBodies[1]).toEqual({ mandate_id: "mandate-1" });
    expect(reactErrors).toEqual([]);
  });

  test("a paused mandate on the instrument: the API refuses the inline mandate and the card says why", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    await mockBackend(page);
    const mandateBodies: any[] = [];
    await page.route("**/api/v1/ai/mandates", (route) => {
      if (route.request().method() !== "POST") return route.fulfill({ json: { mandates: [] } });
      mandateBodies.push(route.request().postDataJSON());
      return route.fulfill({
        status: 409,
        json: { detail: "MANDATE_PAUSED: BTC paused already covers BTCUSDT.BINANCE; resume or edit it instead" },
      });
    });
    await page.route(`**/agent-api/brain/sessions/${SESSION}/cards/*/approve`, (route) =>
      route.fulfill({
        status: 409,
        json: { detail: "NO_MANDATE: approve this card inside an ACTIVE mandate that allows BTCUSDT.BINANCE (Mission Control → Mandates)" },
      }),
    );
    await openBrainHome(page);
    await page.getByRole("textbox", { name: "Message the Brain" }).fill("Analyze BTC and propose a trade");
    await page.getByRole("button", { name: "Send to the Brain" }).click();

    const card = page.getByTestId("brain-assistant-message").last().getByTestId("brain-widget-order-card");
    await card.getByRole("button", { name: "Approve…" }).click();
    await card.getByRole("button", { name: "Confirm paper order" }).click();
    const group = card.getByRole("group", { name: "Confirm paper order" });
    await group.getByLabel("TOTP code").fill("123456");
    await group.getByRole("button", { name: "Create mandate and confirm" }).click();

    // The server refuses (one source of truth); the card shows its reason and nothing is queued
    await expect(card.getByRole("alert")).toContainText("MANDATE_PAUSED: BTC paused already covers BTCUSDT.BINANCE");
    expect(mandateBodies).toHaveLength(1);
    expect(reactErrors).toEqual([]);
  });

  test("an approve refused by a paused mandate shows its reason and a link to Mission Control, with no TOTP box", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    await mockBackend(page);
    const mandateBodies: any[] = [];
    const orderBodies: any[] = [];
    await page.route("**/api/v1/ai/mandates", (route) => {
      if (route.request().method() === "POST") mandateBodies.push(route.request().postDataJSON());
      return route.fulfill({ json: { mandates: [] } });
    });
    await page.route(`**/agent-api/brain/sessions/${SESSION}/cards/*/approve`, (route) => {
      orderBodies.push(route.request().postDataJSON());
      return route.fulfill({
        status: 409,
        json: { detail: "MANDATE_PAUSED: BTC-ETH spot L1 already covers BTCUSDT.BINANCE; resume it in Mission Control, then approve this card" },
      });
    });
    await openBrainHome(page);
    await page.getByRole("textbox", { name: "Message the Brain" }).fill("Analyze BTC and propose a trade");
    await page.getByRole("button", { name: "Send to the Brain" }).click();

    const card = page.getByTestId("brain-assistant-message").last().getByTestId("brain-widget-order-card");
    await card.getByRole("button", { name: "Approve…" }).click();
    await card.getByRole("button", { name: "Confirm paper order" }).click();

    // The server's reason is shown and the operator is pointed at Mission Control; no mandate form, no TOTP box
    await expect(card.getByRole("alert")).toContainText("MANDATE_PAUSED: BTC-ETH spot L1 already covers BTCUSDT.BINANCE");
    await expect(card.getByRole("alert")).toContainText("resume it in Mission Control, then approve this card");
    await expect(card.getByRole("link", { name: "Open Mission Control" })).toHaveAttribute("href", "/?ws=mission&view=overview");
    await expect(card.getByLabel("TOTP code")).toHaveCount(0);
    await expect(card.getByRole("group", { name: "Confirm paper order" })).toHaveCount(0);
    // Approve stays available for a retry after the mandate is resumed; nothing was created or queued
    await expect(card.getByRole("button", { name: "Approve…" })).toBeEnabled();
    expect(orderBodies).toHaveLength(1);
    expect(orderBodies[0]?.mandate_id).toBeUndefined();
    expect(mandateBodies).toEqual([]);

    await card.getByRole("link", { name: "Open Mission Control" }).click();
    await expect(page).toHaveURL(/\?ws=mission&view=overview$/);
    expect(reactErrors).toEqual([]);
  });

  test("the Open Mission Control link is reachable and works from the keyboard", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    await mockBackend(page);
    await page.route(`**/agent-api/brain/sessions/${SESSION}/cards/*/approve`, (route) =>
      route.fulfill({
        status: 409,
        json: { detail: "MANDATE_PAUSED: BTC-ETH spot L1 already covers BTCUSDT.BINANCE; resume it in Mission Control, then approve this card" },
      }),
    );
    await openBrainHome(page);
    await page.getByRole("textbox", { name: "Message the Brain" }).fill("Analyze BTC and propose a trade");
    await page.getByRole("button", { name: "Send to the Brain" }).click();

    const card = page.getByTestId("brain-assistant-message").last().getByTestId("brain-widget-order-card");
    await card.getByRole("button", { name: "Approve…" }).click();
    await card.getByRole("button", { name: "Confirm paper order" }).click();
    const link = card.getByRole("link", { name: "Open Mission Control" });
    await expect(link).toBeVisible();
    await link.focus();
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(/\?ws=mission&view=overview$/);
    expect(reactErrors).toEqual([]);
  });

  test("a paused-mandate notice goes with the card when the operator rejects it", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    await mockBackend(page);
    await page.route(`**/agent-api/brain/sessions/${SESSION}/cards/*/approve`, (route) =>
      route.fulfill({
        status: 409,
        json: { detail: "MANDATE_PAUSED: BTC-ETH spot L1 already covers BTCUSDT.BINANCE; resume it in Mission Control, then approve this card" },
      }),
    );
    await openBrainHome(page);
    await page.getByRole("textbox", { name: "Message the Brain" }).fill("Analyze BTC and propose a trade");
    await page.getByRole("button", { name: "Send to the Brain" }).click();

    const card = page.getByTestId("brain-assistant-message").last().getByTestId("brain-widget-order-card");
    await card.getByRole("button", { name: "Approve…" }).click();
    await card.getByRole("button", { name: "Confirm paper order" }).click();
    await expect(card.getByRole("link", { name: "Open Mission Control" })).toBeVisible();

    // Rejected: the card no longer asks for approval, so the "then approve this card" notice goes too
    await card.getByRole("button", { name: "Reject" }).click();
    await expect(card).toContainText("Rejected by you");
    await expect(card.getByRole("link", { name: "Open Mission Control" })).toHaveCount(0);
    await expect(card).not.toContainText("then approve this card");
    expect(reactErrors).toEqual([]);
  });

  test("an inline mandate refused as paused: the notice replaces the form, and nothing is queued", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    await mockBackend(page);
    const mandateBodies: any[] = [];
    await page.route("**/api/v1/ai/mandates", (route) => {
      if (route.request().method() !== "POST") return route.fulfill({ json: { mandates: [] } });
      mandateBodies.push(route.request().postDataJSON());
      return route.fulfill({
        status: 409,
        json: { detail: "MANDATE_PAUSED: BTC-ETH spot L1 already covers BTCUSDT.BINANCE; resume it in Mission Control, then approve this card" },
      });
    });
    await page.route(`**/agent-api/brain/sessions/${SESSION}/cards/*/approve`, (route) =>
      route.fulfill({
        status: 409,
        json: { detail: "NO_MANDATE: approve this card inside an ACTIVE mandate that allows BTCUSDT.BINANCE (Mission Control → Mandates)" },
      }),
    );
    await openBrainHome(page);
    await page.getByRole("textbox", { name: "Message the Brain" }).fill("Analyze BTC and propose a trade");
    await page.getByRole("button", { name: "Send to the Brain" }).click();

    const card = page.getByTestId("brain-assistant-message").last().getByTestId("brain-widget-order-card");
    await card.getByRole("button", { name: "Approve…" }).click();
    await card.getByRole("button", { name: "Confirm paper order" }).click();
    const group = card.getByRole("group", { name: "Confirm paper order" });
    await group.getByLabel("TOTP code").fill("123456");
    await group.getByRole("button", { name: "Create mandate and confirm" }).click();

    // The form is gone (not only the code): the reason and the Mandates link replace it, and Approve is back
    await expect(card.getByRole("alert")).toContainText("MANDATE_PAUSED: BTC-ETH spot L1 already covers BTCUSDT.BINANCE");
    await expect(card.getByRole("link", { name: "Open Mission Control" })).toBeVisible();
    await expect(card.getByLabel("TOTP code")).toHaveCount(0);
    await expect(card.getByRole("group", { name: "Confirm paper order" })).toHaveCount(0);
    await expect(card.getByRole("button", { name: "Approve…" })).toBeVisible();
    expect(mandateBodies).toHaveLength(1);
    expect(reactErrors).toEqual([]);
  });

  test("a failed inline create empties the TOTP box, so a used code is never sent again", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    await mockBackend(page);
    const mandateBodies: any[] = [];
    await page.route("**/api/v1/ai/mandates", (route) => {
      if (route.request().method() !== "POST") return route.fulfill({ json: { mandates: [] } });
      mandateBodies.push(route.request().postDataJSON());
      if (mandateBodies.length === 1) {
        return route.fulfill({ status: 401, json: { detail: "TOTP_REQUIRED: enter your current TOTP code" } });
      }
      return route.fulfill({ status: 201, json: { id: "mandate-1", name: "Brain BTCUSDT.BINANCE", instruments: ["BTCUSDT.BINANCE"], status: "ACTIVE" } });
    });
    await page.route(`**/agent-api/brain/sessions/${SESSION}/cards/*/approve`, (route) => {
      if (!route.request().postDataJSON()?.mandate_id) {
        return route.fulfill({
          status: 409,
          json: { detail: "NO_MANDATE: approve this card inside an ACTIVE mandate that allows BTCUSDT.BINANCE (Mission Control → Mandates)" },
        });
      }
      return route.fulfill({ json: { command_id: "cmd-12345678", status: "APPROVED", mandate_id: "mandate-1" } });
    });
    await openBrainHome(page);
    await page.getByRole("textbox", { name: "Message the Brain" }).fill("Analyze BTC and propose a trade");
    await page.getByRole("button", { name: "Send to the Brain" }).click();

    const card = page.getByTestId("brain-assistant-message").last().getByTestId("brain-widget-order-card");
    await card.getByRole("button", { name: "Approve…" }).click();
    await card.getByRole("button", { name: "Confirm paper order" }).click();
    const group = card.getByRole("group", { name: "Confirm paper order" });
    await group.getByLabel("TOTP code").fill("123456");
    await group.getByRole("button", { name: "Create mandate and confirm" }).click();

    // The refused create keeps the form for a new code, and the box no longer holds the code it sent
    await expect(card.getByRole("alert")).toContainText("TOTP_REQUIRED: enter your current TOTP code");
    await expect(group.getByLabel("TOTP code")).toHaveValue("");
    await expect(group.getByRole("button", { name: "Create mandate and confirm" })).toBeDisabled();

    await group.getByLabel("TOTP code").fill("654321");
    await group.getByRole("button", { name: "Create mandate and confirm" }).click();
    await expect(card.getByRole("status")).toContainText("Approved · sending the order · paper command cmd-12345678");
    expect(mandateBodies.map((b) => b.totp_code)).toEqual(["123456", "654321"]);
    expect(reactErrors).toEqual([]);
  });

  test("a reused TOTP code is shown exactly as the API wrote it", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    await mockBackend(page);
    const reused = "TOTP_REUSED: this code was already used (each code works once); wait for the next code from your authenticator";
    await page.route("**/api/v1/ai/mandates", (route) => {
      if (route.request().method() !== "POST") return route.fulfill({ json: { mandates: [] } });
      return route.fulfill({ status: 401, json: { detail: reused } });
    });
    await page.route(`**/agent-api/brain/sessions/${SESSION}/cards/*/approve`, (route) =>
      route.fulfill({
        status: 409,
        json: { detail: "NO_MANDATE: approve this card inside an ACTIVE mandate that allows BTCUSDT.BINANCE (Mission Control → Mandates)" },
      }),
    );
    await openBrainHome(page);
    await page.getByRole("textbox", { name: "Message the Brain" }).fill("Analyze BTC and propose a trade");
    await page.getByRole("button", { name: "Send to the Brain" }).click();

    const card = page.getByTestId("brain-assistant-message").last().getByTestId("brain-widget-order-card");
    await card.getByRole("button", { name: "Approve…" }).click();
    await card.getByRole("button", { name: "Confirm paper order" }).click();
    const group = card.getByRole("group", { name: "Confirm paper order" });
    await group.getByLabel("TOTP code").fill("123456");
    await group.getByRole("button", { name: "Create mandate and confirm" }).click();

    await expect(card.getByRole("alert")).toHaveText(reused);
    await expect(group.getByLabel("TOTP code")).toHaveValue("");
    expect(reactErrors).toEqual([]);
  });

  test("a SELL card says it closes the holding, and a sell the mandate cannot cover says to reject it", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    await mockBackend(page);
    const sellCard = { ...ORDER_CARD, side: "SELL", rationale: "Taking profit on the BTC holding." };
    await page.route("**/agent-api/brain/stream", (route: Route) =>
      route.fulfill({ status: 200, headers: { "content-type": "text/event-stream" }, body: turnWith(sellCard) }),
    );
    await page.route(`**/agent-api/brain/sessions/${SESSION}/cards/*/approve`, (route) =>
      route.fulfill({
        status: 409,
        json: { detail: "SPOT_SELL_NOT_HELD: mandate Brain holds no BTCUSDT.BINANCE, and a spot sell only closes a holding; reject this card" },
      }),
    );
    await openBrainHome(page);
    await page.getByRole("textbox", { name: "Message the Brain" }).fill("Take profit on BTC");
    await page.getByRole("button", { name: "Send to the Brain" }).click();

    const card = page.getByTestId("brain-assistant-message").last().getByTestId("brain-widget-order-card");
    await expect(card).toContainText("CLOSES HOLDING · PAPER");
    await expect(card).not.toContainText("ADDS RISK");
    await expect(card).toContainText("Whole holding");
    await card.getByRole("button", { name: "Approve…" }).click();
    await expect(card.getByRole("group", { name: "Confirm paper order" })).toContainText("whole holding the mandate has in it");
    await card.getByRole("button", { name: "Confirm paper order" }).click();

    // refused before anything is queued: the card names the reason and stays pending for the operator to reject
    await expect(card.getByRole("alert")).toContainText("SPOT_SELL_NOT_HELD");
    await expect(card.getByRole("alert")).toContainText("reject this card");
    await card.getByRole("button", { name: "Cancel" }).click();
    await expect(card.getByRole("button", { name: "Reject" })).toBeVisible();
    expect(reactErrors).toEqual([]);
  });

  test("reload restores the conversation from the server session", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    const calls = await mockBackend(page);
    await openBrainHome(page);
    await page.getByRole("textbox", { name: "Message the Brain" }).fill("Analyze BTC and propose a trade");
    await page.getByRole("button", { name: "Send to the Brain" }).click();
    await expect(page.getByTestId("brain-widget-order-card")).toBeVisible();

    await page.reload();
    await page.getByRole("button", { name: "AI Brain", exact: true }).click();
    await expect.poll(() => calls.history).toBeGreaterThan(0);
    await expect(page.getByTestId("brain-user-message").first()).toContainText("Analyze BTC and propose a trade");
    await expect(page.getByTestId("brain-widget-order-card").getByRole("status")).toContainText("cmd-12345678");
    await expect(page.getByTestId("brain-system-note")).toContainText("Operator approved order card");
    expect(reactErrors).toEqual([]);
  });

  test("stop cancels a running turn", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    await mockBackend(page);
    let aborted = false;
    await page.route("**/agent-api/brain/stream", async (route) => {
      await new Promise((r) => setTimeout(r, 4000));
      try {
        await route.fulfill({ status: 200, headers: { "content-type": "text/event-stream" }, body: TURN });
      } catch {
        aborted = true; // the browser already aborted the request
      }
    });
    page.on("requestfailed", (req) => {
      if (req.url().includes("/agent-api/brain/stream")) aborted = true;
    });
    await openBrainHome(page);
    await page.getByRole("textbox", { name: "Message the Brain" }).fill("long analysis please");
    await page.getByRole("button", { name: "Send to the Brain" }).click();
    await expect(page.getByText("Thinking…")).toBeVisible();
    await page.getByRole("button", { name: "Stop the Brain" }).click();
    await expect(page.getByTestId("brain-assistant-message").last()).toContainText("Stopped.");
    await expect(page.getByRole("button", { name: "Send to the Brain" })).toBeVisible();
    await expect.poll(() => aborted, { timeout: 8000 }).toBe(true);
    expect(reactErrors).toEqual([]);
  });

  test("cryptonite:brain-ask from another workspace opens the Brain with the prompt", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    await mockBackend(page);
    await page.goto("/");
    const copilot = page.getByRole("dialog", { name: "Brain copilot" });
    // Re-dispatch until the page has hydrated and its listener is attached
    await expect
      .poll(async () => {
        await page.evaluate(() =>
          window.dispatchEvent(new CustomEvent("cryptonite:brain-ask", { detail: { prompt: "Ask about this chart: BTCUSDT 1h" } })),
        );
        return copilot.isVisible();
      })
      .toBe(true);
    await expect(copilot.getByRole("textbox", { name: "Message the Brain" })).toHaveValue("Ask about this chart: BTCUSDT 1h");
    expect(reactErrors).toEqual([]);
  });

  /** An order on an instrument the live feed does not allow: the card says why, and unlocks only once the feed streams it */
  const SOL_CARD = {
    ...ORDER_CARD,
    instrument_id: "SOLUSDT.BINANCE",
    rationale: "Reclaim of the 4H range.",
    feed_refusal: "SYMBOL_NOT_INGESTED: ingest has no feed for SOLUSDT (fed: BTCUSDT); onboard it first",
  };

  /** /market/universe as the API answers it: instruments are SYMBOL.VENUE */
  const universe = (instruments: string[]) => ({ available: true, instruments });

  async function openCard(page: Page, card: Record<string, unknown>) {
    await mockBackend(page);
    await page.route("**/agent-api/brain/stream", (route: Route) =>
      route.fulfill({ status: 200, headers: { "content-type": "text/event-stream" }, body: turnWith(card) }),
    );
    await openBrainHome(page);
    await page.getByRole("textbox", { name: "Message the Brain" }).fill("Propose a trade");
    await page.getByRole("button", { name: "Send to the Brain" }).click();
    return page.getByTestId("brain-assistant-message").last().getByTestId("brain-widget-order-card");
  }

  test("an un-fed symbol: the card offers onboarding, locks Approve, and unlocks once the symbol streams", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    let streaming = false;
    const onboardBodies: { body: unknown; csrf: string | undefined }[] = [];
    await page.route("**/api/v1/market/universe", (route) =>
      route.fulfill({ json: universe(streaming ? ["BTCUSDT.BINANCE", "SOLUSDT.BINANCE"] : ["BTCUSDT.BINANCE"]) }),
    );
    await page.route("**/api/v1/instruments/onboard", (route) => {
      onboardBodies.push({ body: route.request().postDataJSON(), csrf: route.request().headers()["x-csrf-token"] });
      streaming = true; // ingest picks the symbol up; the card's next feed check sees it
      return route.fulfill({ json: { status: "ok", message: "Onboarding SOLUSDT..." } });
    });
    const card = await openCard(page, SOL_CARD);

    await expect(card).toContainText("SOLUSDT is not in the live data feed, so this order cannot be approved yet.");
    await expect(card.getByRole("button", { name: "Approve…" })).toBeDisabled();
    await expect(card.getByLabel("TOTP code")).toHaveCount(0);
    await expect(card.getByRole("button", { name: "Reject" })).toBeEnabled();

    await card.getByRole("button", { name: "Onboard SOLUSDT" }).click();
    await expect(card.getByRole("button", { name: "Onboarding SOLUSDT…" })).toBeDisabled();
    await expect.poll(() => onboardBodies).toEqual([{ body: { symbol: "SOLUSDT" }, csrf: "csrf-test" }]);

    // The feed is checked every 5 s; once SOLUSDT.BINANCE streams the normal approve flow appears
    await expect(card.getByRole("button", { name: "Approve…" })).toBeEnabled({ timeout: 15_000 });
    await expect(card.getByRole("status")).toContainText("SOLUSDT is streaming; you can approve now.");
    await expect(card).not.toContainText("cannot be approved yet");
    await card.getByRole("button", { name: "Approve…" }).click();
    await card.getByRole("button", { name: "Confirm paper order" }).click();
    await expect(card.getByRole("status")).toContainText("Approved · sending the order · paper command cmd-12345678");
    expect(reactErrors).toEqual([]);
  });

  test("a symbol the feed already streams unlocks on first render, with no Onboard click", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    const onboardBodies: unknown[] = [];
    await page.route("**/api/v1/market/universe", (route) => route.fulfill({ json: universe(["BTCUSDT.BINANCE", "SOLUSDT.BINANCE"]) }));
    await page.route("**/api/v1/instruments/onboard", (route) => {
      onboardBodies.push(route.request().postDataJSON());
      return route.fulfill({ json: { status: "ok" } });
    });
    const card = await openCard(page, SOL_CARD);

    await expect(card.getByRole("button", { name: "Approve…" })).toBeEnabled();
    await expect(card.getByRole("status")).toContainText("SOLUSDT is streaming; you can approve now.");
    await expect(card).not.toContainText("cannot be approved yet");
    await expect(card.getByRole("button", { name: /Onboard/ })).toHaveCount(0);
    expect(onboardBodies).toEqual([]);
    expect(reactErrors).toEqual([]);
  });

  test("onboarding that never streams gives up after 3 minutes, with a retry message", async ({ page }) => {
    test.setTimeout(150_000); // the fake clock must run past the 3-minute window
    const reactErrors = collectReactErrors(page);
    await page.clock.install();
    const onboardBodies: unknown[] = [];
    await page.route("**/api/v1/market/universe", (route) => route.fulfill({ json: universe(["BTCUSDT.BINANCE"]) }));
    await page.route("**/api/v1/instruments/onboard", (route) => {
      onboardBodies.push(route.request().postDataJSON());
      return route.fulfill({ json: { status: "ok" } });
    });
    const card = await openCard(page, SOL_CARD);

    await card.getByRole("button", { name: "Onboard SOLUSDT" }).click();
    await expect.poll(() => onboardBodies.length).toBe(1);
    // Each feed check waits on a 5 s timer of the fake clock: advance it until the 3-minute window has passed
    await expect
      .poll(
        async () => {
          await page.clock.runFor(5_000);
          return card.getByText("Still not streaming; try again or check the Watchlist.").count();
        },
        { timeout: 120_000 },
      )
      .toBe(1);
    await expect(card.getByRole("button", { name: "Onboard SOLUSDT" })).toBeEnabled();
    await expect(card.getByRole("button", { name: "Approve…" })).toBeDisabled();
    expect(onboardBodies).toHaveLength(1);
    expect(reactErrors).toEqual([]);
  });

  test("a card that is no longer pending is not locked by its refusal", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    let universeCalls = 0;
    await page.route("**/api/v1/market/universe", (route) => {
      universeCalls++;
      return route.fulfill({ json: universe(["BTCUSDT.BINANCE"]) });
    });
    const card = await openCard(page, { ...SOL_CARD, status: "REJECTED" });

    await expect(card).toContainText("Rejected by you");
    await expect(card.getByTestId("brain-order-feed-gate")).toHaveCount(0);
    await expect(card.getByRole("button", { name: "Approve…" })).toHaveCount(0);
    expect(universeCalls).toBe(0);
    expect(reactErrors).toEqual([]);
  });

  test("a pending SELL is never locked by the feed, even if its refusal is set", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    let universeCalls = 0;
    await page.route("**/api/v1/market/universe", (route) => {
      universeCalls++;
      return route.fulfill({ json: universe(["BTCUSDT.BINANCE"]) });
    });
    // The API sends feed_refusal null for a SELL; this card carries one anyway to prove the UI does not lock it
    const sell = { ...SOL_CARD, side: "SELL", rationale: "Taking profit on the SOL holding." };
    const card = await openCard(page, sell);

    await expect(card.getByRole("button", { name: "Approve…" })).toBeEnabled();
    await expect(card.getByTestId("brain-order-feed-gate")).toHaveCount(0);
    await expect(card).not.toContainText("cannot be approved yet");
    await expect(card.getByRole("button", { name: /Onboard/ })).toHaveCount(0);
    expect(universeCalls).toBe(0);
    expect(reactErrors).toEqual([]);
  });

  test("an instrument on another venue stays locked with its reason, and offers no Onboard", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    await page.route("**/api/v1/market/universe", (route) => route.fulfill({ json: universe(["BTCUSDT.BINANCE"]) }));
    const bybit = {
      ...SOL_CARD,
      instrument_id: "BTCUSDT.BYBIT",
      rationale: "Breakout on the BYBIT book.",
      feed_refusal: "VENUE_NOT_INGESTED: ingest streams BINANCE only, not BYBIT",
    };
    const card = await openCard(page, bybit);

    await expect(card).toContainText("VENUE_NOT_INGESTED: ingest streams BINANCE only, not BYBIT. This order cannot be approved.");
    await expect(card.getByRole("button", { name: /Onboard/ })).toHaveCount(0);
    await expect(card.getByRole("button", { name: "Approve…" })).toBeDisabled();
    await expect(card.getByLabel("TOTP code")).toHaveCount(0);
    await expect(card.getByRole("button", { name: "Reject" })).toBeEnabled();
    expect(reactErrors).toEqual([]);
  });

  test("a failed onboard shows the API's reason on the card and keeps Approve locked", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    await page.route("**/api/v1/market/universe", (route) => route.fulfill({ json: universe(["BTCUSDT.BINANCE"]) }));
    await page.route("**/api/v1/instruments/onboard", (route) =>
      route.fulfill({ status: 401, json: { detail: "Not authenticated" } }),
    );
    const card = await openCard(page, SOL_CARD);

    await card.getByRole("button", { name: "Onboard SOLUSDT" }).click();
    await expect(card.getByRole("alert")).toContainText("Not authenticated");
    await expect(card.getByRole("button", { name: "Onboard SOLUSDT" })).toBeEnabled();
    await expect(card.getByRole("button", { name: "Approve…" })).toBeDisabled();
    expect(reactErrors).toEqual([]);
  });

  /** The dry run Approve would get: the mock answers each read of /cards/{id}/preflight and counts the reads */
  async function mockPreflight(page: Page, answer: () => { status?: number; json: unknown }) {
    const reads = { count: 0, methods: [] as string[] };
    await page.route(`**/agent-api/brain/sessions/${SESSION}/cards/*/preflight`, (route) => {
      reads.count++;
      reads.methods.push(route.request().method());
      const reply = answer();
      return route.fulfill({ status: reply.status ?? 200, json: reply.json });
    });
    return reads;
  }

  /** A dry run that would size a BUY inside an ACTIVE mandate; `over` replaces fields */
  const buyPreflight = (over: Record<string, unknown> = {}) => ({
    ok: true,
    mandate: { id: "mandate-btc", name: "BTC L1", status: "ACTIVE" },
    side: "BUY",
    allocation_pct: 5,
    budget_usd: 10000,
    clipped_from_pct: null,
    sell_quantity: null,
    summary: "Sized at 5% of the mandate.",
    ...over,
  });

  test("a BUY card says what Approve would size to, before the operator confirms", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    const reads = await mockPreflight(page, () => ({ json: buyPreflight() }));
    const card = await openCard(page, ORDER_CARD);

    await expect(card.getByTestId("brain-card-preflight")).toHaveText("Would size to 5% of BTC L1 ($10,000)");
    await expect(card.getByRole("button", { name: "Approve…" })).toBeEnabled();
    expect(reads.count).toBe(1);
    expect(reads.methods).toEqual(["GET"]);
    expect(reactErrors).toEqual([]);
  });

  test("a size the room under the position limit clipped says so", async ({ page }) => {
    await mockPreflight(page, () => ({ json: buyPreflight({ allocation_pct: 10, clipped_from_pct: 12 }) }));
    const card = await openCard(page, ORDER_CARD);

    await expect(card.getByTestId("brain-card-preflight")).toHaveText(
      "Would size to 10% of BTC L1 ($10,000) (clipped from 12% — room left)",
    );
  });

  test("a SELL card says what it would sell and from which mandate", async ({ page }) => {
    await mockPreflight(page, () => ({ json: buyPreflight({ side: "SELL", sell_quantity: "0.015" }) }));
    const card = await openCard(page, {
      ...ORDER_CARD,
      side: "SELL",
      rationale: "Taking profit on the BTC holding.",
    });

    await expect(card.getByTestId("brain-card-preflight")).toHaveText("Would sell 0.015 from BTC L1");
    await expect(card.getByRole("button", { name: "Approve…" })).toBeEnabled();
  });

  test("a BUY with no ACTIVE mandate says approving creates one, and Approve keeps its normal flow", async ({ page }) => {
    await mockPreflight(page, () => ({
      json: {
        ok: false,
        code: "NO_MANDATE",
        detail: "NO_MANDATE: approve this card inside an ACTIVE mandate that allows BTCUSDT.BINANCE (Mission Control → Mandates)",
        needs_mandate: true,
      },
    }));
    const card = await openCard(page, ORDER_CARD);

    await expect(card.getByTestId("brain-card-preflight")).toHaveText(
      "No ACTIVE mandate allows BTCUSDT yet: approving creates one (TOTP)",
    );
    await expect(card.getByRole("button", { name: "Approve…" })).toBeEnabled();
    await expect(card.getByRole("button", { name: "Approve anyway…" })).toHaveCount(0);
  });

  test("a refusal says why, and Approve stays enabled as Approve anyway", async ({ page }) => {
    await mockPreflight(page, () => ({
      json: {
        ok: false,
        code: "MANDATE_PAUSED",
        detail: "MANDATE_PAUSED: BTC paused already covers BTCUSDT.BINANCE; resume or edit it instead",
        needs_mandate: false,
      },
    }));
    const card = await openCard(page, ORDER_CARD);

    await expect(card.getByTestId("brain-card-preflight")).toHaveText(
      "Would be refused: MANDATE_PAUSED: BTC paused already covers BTCUSDT.BINANCE; resume or edit it instead",
    );
    const anyway = card.getByRole("button", { name: "Approve anyway…" });
    await expect(anyway).toBeEnabled();
    await expect(card.getByRole("button", { name: "Approve…" })).toHaveCount(0);
    await anyway.click();
    await expect(card.getByRole("group", { name: "Confirm paper order" })).toBeVisible();
  });

  test("an unavailable dry run shows nothing and never blocks Approve", async ({ page }) => {
    const reads = await mockPreflight(page, () => ({ status: 500, json: { detail: "preflight unavailable" } }));
    const card = await openCard(page, ORDER_CARD);

    await expect.poll(() => reads.count).toBe(1);
    await expect(card.getByTestId("brain-card-preflight")).toHaveCount(0);
    await expect(card.getByRole("button", { name: "Approve…" })).toBeEnabled();
  });

  test("a dry run answered NOT_PENDING (decided elsewhere) shows no refusal and keeps the normal label", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    const reads = await mockPreflight(page, () => ({
      json: { ok: false, code: "NOT_PENDING", detail: "NOT_PENDING: the card is APPROVED", needs_mandate: false },
    }));
    const answered = page.waitForResponse(/\/cards\/[^/]+\/preflight/);
    const card = await openCard(page, ORDER_CARD);

    await answered;
    await page.waitForTimeout(300); // the card has rendered the answer by now
    expect(reads.count).toBe(1);
    await expect(card.getByTestId("brain-card-preflight")).toHaveCount(0);
    await expect(card.getByRole("button", { name: "Approve…" })).toBeEnabled();
    await expect(card.getByRole("button", { name: "Approve anyway…" })).toHaveCount(0);
    expect(reactErrors).toEqual([]);
  });

  /** A 200 that is not the dry-run contract (a proxy's HTML page, an object without a boolean `ok`) is treated as unavailable */
  async function oddDryRun(page: Page, contentType: string, body: string) {
    const reactErrors = collectReactErrors(page);
    let reads = 0;
    await page.route(`**/agent-api/brain/sessions/${SESSION}/cards/*/preflight`, (route) => {
      reads++;
      return route.fulfill({ status: 200, contentType, body });
    });
    const answered = page.waitForResponse(/\/cards\/[^/]+\/preflight/);
    const card = await openCard(page, ORDER_CARD);

    await answered;
    await page.waitForTimeout(300);
    expect(reads).toBe(1);
    await expect(card.getByTestId("brain-card-preflight")).toHaveCount(0);
    await expect(card.getByRole("button", { name: "Approve…" })).toBeEnabled();
    expect(reactErrors).toEqual([]);
  }

  test("a dry run answered with a non-JSON page shows nothing and never blocks Approve", async ({ page }) => {
    await oddDryRun(page, "text/html", "<!doctype html><html><body>Sign in</body></html>");
  });

  test("a dry run answered with an object whose ok is not a boolean shows nothing and never blocks Approve", async ({ page }) => {
    await oddDryRun(page, "application/json", JSON.stringify({ ok: "yes", mandate: { name: "BTC L1" }, allocation_pct: 5 }));
  });

  test("a refused Approve reads the dry run again, so the refusal replaces the normal line", async ({ page }) => {
    const refusal = "PRICE_COLLAR_REJECT: the limit is too far from the market";
    let refused = false;
    const reads = await mockPreflight(page, () =>
      refused
        ? { json: { ok: false, code: "PRICE_COLLAR_REJECT", detail: refusal, needs_mandate: false } }
        : { json: buyPreflight() },
    );
    // Registered after openCard: its mockBackend registers the approve success route, and the later route wins
    const card = await openCard(page, ORDER_CARD);
    await page.route(`**/agent-api/brain/sessions/${SESSION}/cards/*/approve`, (route) => {
      refused = true; // the market moved after the dry run
      return route.fulfill({ status: 409, json: { detail: refusal } });
    });

    await expect(card.getByTestId("brain-card-preflight")).toHaveText("Would size to 5% of BTC L1 ($10,000)");
    await card.getByRole("button", { name: "Approve…" }).click();
    await card.getByRole("button", { name: "Confirm paper order" }).click();

    await expect(card.getByRole("alert")).toContainText(refusal);
    await expect(card.getByTestId("brain-card-preflight")).toHaveText(`Would be refused: ${refusal}`);
    // mount, the confirm step, then the read after the refused Approve
    await expect.poll(() => reads.count).toBe(3);
  });

  test("opening the confirm step reads the dry run again", async ({ page }) => {
    const reads = await mockPreflight(page, () => ({ json: buyPreflight() }));
    const card = await openCard(page, ORDER_CARD);

    await expect(card.getByTestId("brain-card-preflight")).toBeVisible();
    const before = reads.count;
    await card.getByRole("button", { name: "Approve…" }).click();
    await expect(card.getByRole("group", { name: "Confirm paper order" })).toBeVisible();
    await expect.poll(() => reads.count).toBe(before + 1);
    expect(reads.methods.every((m) => m === "GET")).toBe(true);
  });

  test("window focus re-reads the dry run, at most every 5 seconds", async ({ page }) => {
    const reads = await mockPreflight(page, () => ({ json: buyPreflight() }));
    const card = await openCard(page, ORDER_CARD);

    await expect(card.getByTestId("brain-card-preflight")).toBeVisible();
    expect(reads.count).toBe(1);
    // Focus within 5 s of the last read is ignored
    await page.evaluate(() => window.dispatchEvent(new Event("focus")));
    await page.waitForTimeout(300);
    expect(reads.count).toBe(1);
    // Once 5 s have passed since the last read, focus reads it again
    await page.waitForTimeout(5_000);
    await page.evaluate(() => window.dispatchEvent(new Event("focus")));
    await expect.poll(() => reads.count).toBe(2);
  });

  test("a card that is no longer pending is not read", async ({ page }) => {
    const reads = await mockPreflight(page, () => ({ json: buyPreflight() }));
    const card = await openCard(page, { ...ORDER_CARD, status: "REJECTED" });

    await expect(card).toContainText("Rejected by you");
    // A read started with the card would have reached the route by now
    await page.waitForTimeout(500);
    expect(reads.count).toBe(0);
  });

  test("a feed-locked card is not read until its symbol streams, then it is", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    let streaming = false;
    let universeCalls = 0;
    await page.route("**/api/v1/market/universe", (route) => {
      universeCalls++;
      return route.fulfill({ json: universe(streaming ? ["BTCUSDT.BINANCE", "SOLUSDT.BINANCE"] : ["BTCUSDT.BINANCE"]) });
    });
    await page.route("**/api/v1/instruments/onboard", (route) => {
      streaming = true; // ingest picks the symbol up; the card's next feed check sees it
      return route.fulfill({ json: { status: "ok" } });
    });
    const reads = await mockPreflight(page, () => ({
      json: buyPreflight({ mandate: { id: "mandate-sol", name: "Brain SOLUSDT.BINANCE", status: "ACTIVE" } }),
    }));
    const card = await openCard(page, SOL_CARD);

    await expect(card).toContainText("cannot be approved yet");
    await expect.poll(() => universeCalls).toBeGreaterThan(0);
    expect(reads.count).toBe(0);

    await card.getByRole("button", { name: "Onboard SOLUSDT" }).click();
    await expect(card.getByRole("button", { name: "Approve…" })).toBeEnabled({ timeout: 15_000 });
    await expect(card.getByTestId("brain-card-preflight")).toHaveText("Would size to 5% of Brain SOLUSDT.BINANCE ($10,000)");
    expect(reads.count).toBe(1);
    expect(reactErrors).toEqual([]);
  });

  /** Past lessons on this instrument, as the server sends them (alpha in percent, dates as ISO timestamps) */
  const LESSONS = [
    { lesson_text: "Chasing the 4H breakout into the weekly high lost the edge.", outcome: "LOSS", alpha_pct: -0.8, created_at: "2026-10-01T09:30:00Z" },
    { lesson_text: "Waiting for the retest paid off.", outcome: "WIN", alpha_pct: 1.2, created_at: "2026-09-20T14:00:00+00:00" },
  ];

  test("past lessons are collapsed by default, and expand to show each lesson with its outcome, alpha and date", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    const card = await openCard(page, { ...ORDER_CARD, lessons: LESSONS });

    const toggle = card.getByRole("button", { name: "Past lessons (2)" });
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await expect(card.getByText("Waiting for the retest paid off.")).toBeHidden();

    // Keyboard: the disclosure is a real button, so Enter opens it
    await toggle.focus();
    await page.keyboard.press("Enter");
    await expect(toggle).toHaveAttribute("aria-expanded", "true");

    const items = card.locator("li");
    await expect(items).toHaveCount(2);
    await expect(items.nth(0)).toContainText("LOSS");
    await expect(items.nth(0)).toContainText("−0.8%");
    await expect(items.nth(0)).toContainText("2026-10-01");
    await expect(items.nth(0)).toContainText("“Chasing the 4H breakout into the weekly high lost the edge.”");
    await expect(items.nth(1)).toContainText("WIN");
    await expect(items.nth(1)).toContainText("+1.2%");
    await expect(items.nth(1)).toContainText("2026-09-20");
    await expect(items.nth(1)).toContainText("“Waiting for the retest paid off.”");

    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await expect(card.getByText("Waiting for the retest paid off.")).toBeHidden();
    expect(reactErrors).toEqual([]);
  });

  test("a card without past lessons shows no disclosure", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    const card = await openCard(page, ORDER_CARD);

    await expect(card).toContainText("4H reclaim of 60k");
    await expect(card.getByRole("button", { name: /Past lessons/ })).toHaveCount(0);
    await expect(card).not.toContainText("Past lessons");
    expect(reactErrors).toEqual([]);
  });

  test("lesson text is plain text: markup in a lesson renders literally, and a null outcome shows a dash", async ({ page }) => {
    const card = await openCard(page, {
      ...ORDER_CARD,
      lessons: [
        {
          lesson_text: "<b>Size</b> was <img src=x onerror=alert(1)> too big",
          outcome: null,
          alpha_pct: null,
          created_at: "2026-10-02T00:00:00Z",
        },
      ],
    });

    await card.getByRole("button", { name: "Past lessons (1)" }).click();
    await expect(card.locator("li")).toContainText("“<b>Size</b> was <img src=x onerror=alert(1)> too big”");
    await expect(card.locator("li")).toContainText("—");
    await expect(card.locator("li")).toContainText("2026-10-02");
    await expect(card.locator("b")).toHaveCount(0);
    await expect(card.locator("img")).toHaveCount(0);
  });

  test("past lessons show on a card in any status, a SELL included", async ({ page }) => {
    const card = await openCard(page, {
      ...ORDER_CARD,
      side: "SELL",
      status: "FILLED",
      filled_qty: "0.01",
      avg_fill_price: "60100",
      lessons: [LESSONS[1]],
    });

    await expect(card.getByTestId("card-outcome")).toContainText("Filled");
    await expect(card.getByRole("button", { name: "Past lessons (1)" })).toHaveAttribute("aria-expanded", "false");
  });
});

/**
 * WS-3C debate cards: a card follows its debate run (asked on mount, then every 10 s while QUEUED or RUNNING), stops at
 * a final state, and says a gate refusal in words. The server stores only a final card's status, so such a card is
 * asked once on mount for its answer.
 */
test.describe("Debate cards follow their run", () => {
  const DEBATE_RUN = "3f2a1c00-0000-4000-8000-0000000000d1";
  const VERDICT = { recommendation: "BUY", conviction_score: 0.62, allocation_pct: 5, approval_status: "PENDING" };

  const debateWidget = (id: string, over: Record<string, unknown> = {}) => ({
    kind: "debate_summary",
    id,
    instrument_id: "XRPUSDT.BINANCE",
    timeframe: "5m",
    recommendation: null,
    conviction_score: null,
    status: "QUEUED",
    message: "Proposal staged to Action Inbox",
    message_ref: "1728900000000-0",
    run_id: null,
    verdict: null,
    reason: null,
    ...over,
  });

  /**
   * Opens a session whose history holds these cards. Each card's debate route answers its asks from `answers[cardId]`
   * in turn (a card with no entry is asked a QUEUED answer). Returns the number of asks each card received.
   */
  async function openDebates(page: Page, widgets: Record<string, unknown>[], answers: Record<string, Record<string, unknown>[]> = {}) {
    const asks: Record<string, number> = Object.fromEntries(widgets.map((w) => [String(w.id), 0]));
    const queued = { status: "QUEUED", run_id: null, verdict: null, reason: null };
    await page.route("**/api/v1/paper/positions*", (route) => route.fulfill({ json: [] }));
    await page.route("**/agent-api/brain/spend", (route) => route.fulfill({ json: { spent_usd: "0.10", cap_usd: "25.00" } }));
    await page.route(/\/agent-api\/brain\/sessions(\?.*)?$/, (route) => route.fulfill({ json: { sessions: [], next_cursor: null } }));
    await page.route(`**/agent-api/brain/sessions/${SESSION}`, (route) =>
      route.fulfill({
        json: {
          session: { id: SESSION, title: "XRP debates" },
          summary: null,
          messages: [
            { id: 1, role: "user", content: "Debate the XRP setups", widgets: [], trace_id: null, created_at: null },
            { id: 2, role: "assistant", content: "The debates are below.", widgets, trace_id: null, created_at: null },
          ],
        },
      }),
    );
    await page.route(/\/agent-api\/brain\/sessions\/[^/]+\/cards\/[^/]+\/debate$/, (route) => {
      const cardId = decodeURIComponent(new URL(route.request().url()).pathname.split("/").at(-2) ?? "");
      const seq = answers[cardId] ?? [queued];
      const n = asks[cardId] ?? 0;
      asks[cardId] = n + 1;
      return route.fulfill({ json: seq[Math.min(n, seq.length - 1)] });
    });
    await page.addInitScript((sid) => {
      localStorage.setItem("csrf_token", "csrf-test");
      localStorage.setItem("cryptonite-brain", JSON.stringify({ state: { sessionId: sid, historyOpen: false }, version: 1 }));
    }, SESSION);
    await page.goto("/?ws=brain&view=home");
    await expect(page.getByTestId("brain-home")).toBeVisible();
    return asks;
  }

  test("a queued debate follows its run to the verdict, then stops asking", async ({ page }) => {
    test.setTimeout(60_000); // asks at once and every 10 s, then waits one more interval to prove it stopped
    const reactErrors = collectReactErrors(page);
    const asks = await openDebates(
      page,
      [debateWidget("call-2")],
      {
        "call-2": [
          { status: "QUEUED", run_id: null, verdict: null, reason: null },
          { status: "RUNNING", run_id: DEBATE_RUN, verdict: null, reason: null },
          { status: "VERDICT", run_id: DEBATE_RUN, verdict: VERDICT, reason: null },
        ],
      },
    );
    const card = page.getByTestId("brain-widget-debate");
    const outcome = card.getByTestId("debate-outcome");

    await expect(outcome).toHaveText("Debate running…", { timeout: 15_000 });
    await expect(card).toContainText("RUNNING");
    await expect(outcome).toHaveText("Verdict: BUY 5% · conviction 62% · awaiting approval", { timeout: 20_000 });
    await expect(card).toContainText("VERDICT");

    const atVerdict = asks["call-2"];
    expect(atVerdict).toBe(3);
    await page.waitForTimeout(11_000); // one full poll interval after the verdict
    expect(asks["call-2"]).toBe(atVerdict);
    expect(reactErrors).toEqual([]);
  });

  test("a final card the store wrote without its answer is asked once on mount and reads the answer", async ({ page }) => {
    test.setTimeout(60_000); // asks once, then waits one poll interval to prove it is not asked again
    const reactErrors = collectReactErrors(page);
    // The server stores only the status of a final card: no run_id, verdict or reason
    const asks = await openDebates(
      page,
      [
        debateWidget("call-2", { status: "VERDICT" }),
        debateWidget("call-3", { status: "NO_TRADE" }),
        debateWidget("call-4", { status: "GATE_REFUSED" }),
        debateWidget("call-5", { status: "GATE_REFUSED" }),
        debateWidget("call-6", { status: "GATE_REFUSED" }),
        debateWidget("call-7", { status: "FAILED", message: "The debate could not be started" }),
      ],
      {
        "call-2": [{ status: "VERDICT", run_id: DEBATE_RUN, verdict: VERDICT, reason: null }],
        "call-3": [{ status: "NO_TRADE", run_id: DEBATE_RUN, verdict: null, reason: "NO_PRICE" }],
        "call-4": [{ status: "GATE_REFUSED", run_id: null, verdict: null, reason: "KILL_SWITCH_LATCHED" }],
        "call-5": [{ status: "GATE_REFUSED", run_id: null, verdict: null, reason: "DAILY_LOSS_LATCHED" }],
        "call-6": [{ status: "GATE_REFUSED", run_id: null, verdict: null, reason: "POSITION_AT_CAP" }],
        "call-7": [{ status: "FAILED", run_id: null, verdict: null, reason: null }],
      },
    );

    await expect(page.getByTestId("debate-outcome")).toHaveText([
      "Verdict: BUY 5% · conviction 62% · awaiting approval",
      "No trade (no usable price)",
      "Not run: the kill switch is latched",
      "Not run: the daily-loss latch is on",
      "Not run: the position is at its cap",
      "Debate failed",
    ]);
    await expect(page.getByTestId("brain-widget-debate").filter({ hasText: "NOT RUN" })).toHaveCount(3);
    await page.waitForTimeout(11_000); // one full poll interval after the answers
    expect(asks).toEqual({ "call-2": 1, "call-3": 1, "call-4": 1, "call-5": 1, "call-6": 1, "call-7": 1 });
    expect(reactErrors).toEqual([]);
  });

  test("a final card that already carries its answer is never asked", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    const asks = await openDebates(page, [
      debateWidget("call-2", { status: "VERDICT", run_id: DEBATE_RUN, verdict: VERDICT }),
      debateWidget("call-3", { status: "NO_TRADE", run_id: DEBATE_RUN, reason: "NO_PRICE" }),
      debateWidget("call-4", { status: "GATE_REFUSED", run_id: DEBATE_RUN, reason: "KILL_SWITCH_LATCHED" }),
      debateWidget("call-5", { status: "GATE_REFUSED", run_id: DEBATE_RUN, reason: "DAILY_LOSS_LATCHED" }),
      debateWidget("call-6", { status: "GATE_REFUSED", run_id: DEBATE_RUN, reason: "POSITION_AT_CAP" }),
      debateWidget("call-7", { status: "FAILED", run_id: DEBATE_RUN, message: "The debate could not be started" }),
    ]);

    await expect(page.getByTestId("debate-outcome")).toHaveText([
      "Verdict: BUY 5% · conviction 62% · awaiting approval",
      "No trade (no usable price)",
      "Not run: the kill switch is latched",
      "Not run: the daily-loss latch is on",
      "Not run: the position is at its cap",
      "Debate failed",
    ]);
    await expect(page.getByTestId("brain-widget-debate").filter({ hasText: "NOT RUN" })).toHaveCount(3);
    expect(Object.values(asks)).toEqual([0, 0, 0, 0, 0, 0]);
    expect(reactErrors).toEqual([]);
  });

  test("the verdict line names the approval state and says nothing when there is none", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    const asks = await openDebates(page, [
      ...["PENDING", "APPROVED", "VETOED", "EXPIRED", "EXPIRED_PROCEEDED", "CANCELLED", null].map((approval, i) =>
        debateWidget(`call-${i + 2}`, {
          status: "VERDICT",
          run_id: DEBATE_RUN,
          verdict: { ...VERDICT, approval_status: approval },
        }),
      ),
    ]);

    await expect(page.getByTestId("debate-outcome")).toHaveText([
      "Verdict: BUY 5% · conviction 62% · awaiting approval",
      "Verdict: BUY 5% · conviction 62% · approved",
      "Verdict: BUY 5% · conviction 62% · vetoed",
      "Verdict: BUY 5% · conviction 62% · expired (no order)",
      "Verdict: BUY 5% · conviction 62% · expired and proceeded",
      "Verdict: BUY 5% · conviction 62% · cancelled",
      "Verdict: BUY 5% · conviction 62%",
    ]);
    expect(Object.values(asks)).toEqual([0, 0, 0, 0, 0, 0, 0]);
    expect(reactErrors).toEqual([]);
  });
});
