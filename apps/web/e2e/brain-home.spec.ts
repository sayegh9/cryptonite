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

const TURN = sse([
  ["session", { session_id: SESSION, trace_id: TRACE }],
  ["tool_call", { id: "call-0", tool: "show_price_chart", args: { symbol: "BTCUSDT", timeframe: "4h" }, phase: "start", status: "RUNNING" }],
  ["tool_call", { id: "call-0", tool: "show_price_chart", phase: "end", status: "OK" }],
  ["widget", { widget: { kind: "price_chart", id: "call-0", symbol: "BTCUSDT", timeframe: "4h" } }],
  ["token", { text: "BTC reclaimed 60k on the 4H. " }],
  ["tool_call", { id: "call-1", tool: "propose_order", args: {}, phase: "start", status: "RUNNING" }],
  ["tool_call", { id: "call-1", tool: "propose_order", phase: "end", status: "DENIED" }],
  ["widget", { widget: ORDER_CARD }],
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
  await page.route("**/agent-api/brain/sessions", (route) =>
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
    await expect(card.getByRole("status")).toContainText("Approved · paper command cmd-12345678");
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
});
