import type { Page } from "@playwright/test";
import { test, expect } from "./fixtures/offline";
import { mockAutoTrade } from "./fixtures/autotrade";

/**
 * H5 swing radar: a real match on an uncovered coin is listed under "Informational: not tradeable yet" with its bar
 * time and reason, offers no Propose action, and "Cover this coin" only pre-fills the composer. Every call is mocked.
 */

const SESSION = "5f0c3a6e-1b2d-4c3e-9f00-0000000000c5";
const TRACE = "a91f0000-0000-4000-8000-0000000000c5";

const RADAR = {
  kind: "swing_radar", id: "call-radar", preset_slug: "all", timeframe: "1h", scanned_count: 5, candidates: [],
  uncovered_coins: [{ coin: "MUBARAKUSDT", reason: "no ACTIVE mandate covers MUBARAKUSDT" }],
  untradeable_setups: [{
    instrument_id: "SOLUSDT.BINANCE", preset_slug: "swing-trend-pullback-1h", timeframe: "1h", direction: "LONG",
    entry: 150, stop: 145, target: 160, rr_ratio: 2, bar_close: "2026-11-01T18:59:59.999000+00:00", source: "live_scan",
    reason: "no ACTIVE mandate covers SOLUSDT",
  }],
  rejections: { counts: { STALE: 0, REJECTED_RR: 0, NO_TRIGGER: 4 }, by_coin_preset: [], truncated: 0 },
};

const sse = (events: [string, unknown][]) =>
  events.map(([event, data]) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join("");

async function openRadar(page: Page) {
  await mockAutoTrade(page);
  await page.route("**/api/v1/bars*", (route) => route.fulfill({ json: { bars: [], count: 0 } }));
  await page.route("**/api/v1/paper/positions*", (route) => route.fulfill({ json: [] }));
  await page.route("**/agent-api/brain/spend", (route) => route.fulfill({ json: { spent_usd: "1.00", cap_usd: "25.00" } }));
  await page.route(/\/agent-api\/brain\/sessions(\?.*)?$/, (route) => route.fulfill({ json: { sessions: [] } }));
  await page.route(`**/agent-api/brain/sessions/${SESSION}/notes`, (route) => route.fulfill({ json: { status: "ok" } }));
  const text = "No tradeable setup. SOLUSDT matched at 18:59 UTC but no mandate covers it.";
  const events: [string, unknown][] = [
    ["session", { session_id: SESSION, trace_id: TRACE }],
    ["tool_call", { id: RADAR.id, tool: "scan_swing_setups", args: {}, phase: "start", status: "RUNNING" }],
    ["tool_call", { id: RADAR.id, tool: "scan_swing_setups", phase: "end", status: "OK" }],
    ["widget", { widget: RADAR }],
    ["token", { text }],
    ["done", { session_id: SESSION, message_id: 2, trace_id: TRACE, model: "google:gemini-2.5-pro", cost_usd: "0.0100", content: text }],
  ];
  await page.route("**/agent-api/brain/stream", (route) =>
    route.fulfill({ status: 200, headers: { "content-type": "text/event-stream" }, body: sse(events) }));
  await page.goto("/");
  await page.getByRole("button", { name: "AI Brain", exact: true }).click();
  await expect(page.getByTestId("brain-home")).toBeVisible();
  await page.getByRole("textbox", { name: "Message the Brain" }).fill("Any swing setups?");
  await page.getByRole("button", { name: "Send to the Brain" }).click();
}

test("an untradeable match is informational, with its bar time, and Cover this coin pre-fills the composer", async ({ page }) => {
  await openRadar(page);
  const radar = page.getByTestId("swing-radar-card");
  await expect(radar.getByTestId("swing-radar-empty")).toBeVisible();
  const group = radar.getByTestId("swing-radar-untradeable");
  await expect(group).toContainText("Informational: not tradeable yet");
  const item = group.getByTestId("swing-radar-untradeable-item");
  await expect(item).toHaveCount(1);
  await expect(item).toContainText("SOLUSDT");
  await expect(item).toContainText("no ACTIVE mandate covers SOLUSDT");
  await expect(item.getByTestId("swing-radar-untradeable-bar")).toHaveText("1 Nov 18:59 UTC");
  await expect(radar.getByTestId("swing-radar-propose-btn")).toHaveCount(0);
  await expect(radar).not.toContainText("MUBARAK");  // a coverage note is never shown as a setup

  await item.getByTestId("swing-radar-cover-btn").click();
  await expect(page.getByRole("textbox", { name: "Message the Brain" }))
    .toHaveValue("Help me cover SOLUSDT: add it to the feed and to a mandate or profile");
});
