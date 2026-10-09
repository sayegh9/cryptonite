import type { Page, Route } from "@playwright/test";
import { test, expect } from "./fixtures/offline";

/**
 * K1 Phase 1 web: PriceChartCard empty/stale states.
 *
 * Drives the Brain Home stream with a mocked `price_chart` widget (4h, BTCUSDT) and
 * varies the `/api/v1/bars*` payload so the card shows its three states:
 *   - empty result  -> "No stored bars for BTCUSDT."
 *   - fresh bars    -> chart renders with no stale warning
 *   - stale bars    -> last bar closed >2 timeframes ago -> amber note with the bar age,
 *                      and the stored-chart-vs-live-REST-discussion distinction.
 */

const SESSION = "5f0c3a6e-1b2d-4c3e-9f00-000000000001";
const TRACE = "a91f0000-0000-4000-8000-000000000042";
const INTERVAL_MS = 4 * 3600 * 1000; // 4h widget timeframe

function sse(events: [string, unknown][]): string {
  return events.map(([event, data]) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join("");
}

const TURN = sse([
  ["session", { session_id: SESSION, trace_id: TRACE }],
  ["tool_call", { id: "call-0", tool: "show_price_chart", args: { symbol: "BTCUSDT", timeframe: "4h" }, phase: "start", status: "RUNNING" }],
  ["tool_call", { id: "call-0", tool: "show_price_chart", phase: "end", status: "OK" }],
  ["widget", { widget: { kind: "price_chart", id: "call-0", symbol: "BTCUSDT", timeframe: "4h" } }],
  ["token", { text: "BTC reclaimed 60k on the 4H. " }],
  [
    "done",
    {
      session_id: SESSION,
      message_id: 2,
      trace_id: TRACE,
      model: "google:gemini-2.5-pro",
      cost_usd: "0.0123",
      content: "BTC reclaimed 60k on the 4H.",
    },
  ],
]);

/** 40 synthetic 4h bars, oldest -> newest, ending `staleOffsetMs` before now. Empty when staleOffsetMs is null and empty=true. */
function barsFor({ empty, staleOffsetMs }: { empty?: boolean; staleOffsetMs?: number }): any[] {
  if (empty) return [];
  const now = Date.now();
  const lastCloseMs = staleOffsetMs != null ? now - staleOffsetMs : now - INTERVAL_MS / 2;
  const rows: any[] = [];
  for (let i = 1; i <= 40; i++) {
    const closeMs = lastCloseMs - (40 - i) * INTERVAL_MS;
    rows.push({
      time: Math.floor((closeMs - INTERVAL_MS) / 1000),
      open: String(57900 + i),
      high: String(58400 + i),
      low: String(57600 + i),
      close: String(58000 + i),
      volume: "10.0000",
      bar_open_ns: (closeMs - INTERVAL_MS) * 1_000_000,
      bar_close_inclusive_ns: closeMs * 1_000_000 - 1_000_000,
      interval_end_exclusive_ns: closeMs * 1_000_000,
    });
  }
  return rows;
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

async function mockBackend(page: Page, barsPayload: any) {
  await page.route("**/api/v1/bars*", (route) => route.fulfill({ json: barsPayload }));
  await page.route("**/api/v1/paper/positions*", (route) => route.fulfill({ json: [] }));
  await page.route("**/agent-api/brain/spend", (route) => route.fulfill({ json: { spent_usd: "3.10", cap_usd: "25.00" } }));
  await page.route("**/agent-api/brain/sessions", (route) =>
    route.fulfill({ json: { sessions: [{ id: SESSION, title: "Analyze BTC", created_at: "2026-09-25T10:00:00Z", updated_at: "2026-09-25T10:01:00Z" }] } }),
  );
  await page.route(`**/agent-api/brain/sessions/${SESSION}`, (route) =>
    route.fulfill({
      json: {
        session: { id: SESSION, title: "Analyze BTC" },
        summary: null,
        messages: [
          { id: 1, role: "user", content: "Analyze BTC", widgets: [], trace_id: null, created_at: null },
          { id: 2, role: "assistant", content: "BTC reclaimed 60k on the 4H.", widgets: [], trace_id: TRACE, created_at: null },
        ],
      },
    }),
  );
  await page.route("**/agent-api/brain/stream", (route: Route) =>
    route.fulfill({ status: 200, headers: { "content-type": "text/event-stream" }, body: TURN }),
  );
}

async function openBrainHome(page: Page) {
  await page.addInitScript(() => localStorage.setItem("csrf_token", "csrf-test"));
  await page.goto("/");
  await page.getByRole("button", { name: "AI Brain", exact: true }).click();
  await expect(page.getByTestId("brain-home")).toBeVisible();
}

function chartFor(page: Page) {
  return page.getByTestId("brain-assistant-message").last().getByTestId("brain-widget-price-chart");
}

test.describe("K1 PriceChartCard empty/stale states", () => {
  test("empty result shows 'No stored bars for BTCUSDT' and no chart", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    await mockBackend(page, { symbol: "BTCUSDT", timeframe: "4h", bars: [], count: 0 });
    await openBrainHome(page);

    const input = page.getByRole("textbox", { name: "Message the Brain" });
    await input.fill("Analyze BTC");
    await page.getByRole("button", { name: "Send to the Brain" }).click();

    const card = chartFor(page);
    await expect(card).toContainText("BTCUSDT · 4h");
    await expect(card.getByTestId("brain-widget-price-chart-empty")).toContainText("No stored bars for BTCUSDT");
    await expect(page.getByRole("img", { name: /BTCUSDT price/ })).toHaveCount(0);
    expect(reactErrors).toEqual([]);
  });

  test("fresh bars render the chart without a stale warning", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    await mockBackend(page, {
      symbol: "BTCUSDT",
      timeframe: "4h",
      bars: barsFor({}),
      count: 40,
    });
    await openBrainHome(page);

    const input = page.getByRole("textbox", { name: "Message the Brain" });
    await input.fill("Analyze BTC");
    await page.getByRole("button", { name: "Send to the Brain" }).click();

    const card = chartFor(page);
    await expect(card.getByRole("img", { name: /BTCUSDT price/ })).toBeVisible();
    await expect(card.getByTestId("brain-widget-price-chart-stale")).toHaveCount(0);
    expect(reactErrors).toEqual([]);
  });

  test("stale bars (last bar >2 timeframes old) show the bar age and stored-vs-live distinction", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    await mockBackend(page, {
      symbol: "BTCUSDT",
      timeframe: "4h",
      bars: barsFor({ staleOffsetMs: 3 * INTERVAL_MS }),
      count: 40,
    });
    await openBrainHome(page);

    const input = page.getByRole("textbox", { name: "Message the Brain" });
    await input.fill("Analyze BTC");
    await page.getByRole("button", { name: "Send to the Brain" }).click();

    const card = chartFor(page);
    await expect(card.getByRole("img", { name: /BTCUSDT price/ })).toBeVisible();
    const stale = card.getByTestId("brain-widget-price-chart-stale");
    await expect(stale).toBeVisible();
    await expect(stale).toContainText("12h ago");
    await expect(stale).toContainText(">2×4h");
    await expect(stale).toContainText("live REST");
    expect(reactErrors).toEqual([]);
  });
});