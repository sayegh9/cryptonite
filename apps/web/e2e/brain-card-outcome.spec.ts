import type { Page } from "@playwright/test";
import { test, expect } from "./fixtures/offline";

/**
 * 2026-10-13: an approved order card kept reading "Approved" while the orchestrator had refused the order. The card
 * now follows its order: sending, working, filled, or not executed with the reason.
 */

const SESSION = "5f0c3a6e-1b2d-4c3e-9f00-0000000000c1";
const CMD = "6dd7c0ed-f5f4-4b9d-b3f4-2745c3f194fa";

const card = {
  kind: "order_card", id: "call-1", instrument_id: "XRPUSDT.BINANCE", side: "BUY", order_type: "LIMIT",
  limit_price: 1.3825, allocation_pct: 5, stop_loss: 1.36, take_profit: null, rationale: "Breakout above the range.",
  risk_class: "ADD_RISK", status: "APPROVED", trace_id: null, command_id: CMD,
};

async function openWithCard(page: Page, outcomes: Record<string, unknown>[]) {
  let n = 0;
  await page.route("**/api/v1/paper/positions*", (route) => route.fulfill({ json: [] }));
  await page.route("**/agent-api/brain/spend", (route) => route.fulfill({ json: { spent_usd: "0.10", cap_usd: "25.00" } }));
  await page.route(/\/agent-api\/brain\/sessions(\?.*)?$/, (route) => route.fulfill({ json: { sessions: [], next_cursor: null } }));
  await page.route(`**/agent-api/brain/sessions/${SESSION}`, (route) =>
    route.fulfill({
      json: {
        session: { id: SESSION, title: "XRP" },
        summary: null,
        messages: [
          { id: 1, role: "user", content: "analyze XRPUSDT on the 5m", widgets: [], trace_id: null, created_at: null },
          { id: 2, role: "assistant", content: "Proposed a breakout buy.", widgets: [card], trace_id: null, created_at: null },
        ],
      },
    }),
  );
  await page.route(`**/agent-api/brain/sessions/${SESSION}/cards/call-1/outcome`, (route) =>
    route.fulfill({ json: outcomes[Math.min(n++, outcomes.length - 1)] }),
  );
  await page.addInitScript((sid) => {
    localStorage.setItem("csrf_token", "csrf-test");
    localStorage.setItem("cryptonite-brain", JSON.stringify({ state: { sessionId: sid, historyOpen: false }, version: 1 }));
  }, SESSION);
  await page.goto("/?ws=brain&view=home");
  await expect(page.getByTestId("brain-home")).toBeVisible();
}

test("a refused order reads Not executed with the reason, never Approved", async ({ page }) => {
  await openWithCard(page, [{ status: "NOT_EXECUTED", state: "NOT_EXECUTED", reason: "MANDATE_SIZING_CONFLICT" }]);
  const outcome = page.getByTestId("card-outcome");
  await expect(outcome).toContainText("Not executed: the system could not size it inside the mandate");
  await expect(outcome).toContainText("No position was opened.");
  await expect(outcome).toContainText("MANDATE_SIZING_CONFLICT");
  await expect(outcome).toHaveAttribute("role", "alert");
});

test("a resting limit reads Working, then Filled with the fill", async ({ page }) => {
  test.setTimeout(75_000); // a resting limit is checked every 30 s, as in production
  await openWithCard(page, [
    { status: "WORKING", state: "WORKING", limit_price: "1.3825" },
    { status: "FILLED", state: "FILLED", filled_qty: "361.8", avg_fill_price: "1.3821" },
  ]);
  const outcome = page.getByTestId("card-outcome");
  await expect(outcome).toContainText("Working · limit BUY at 1.3825, not filled yet");
  await expect(outcome).toContainText("Filled · 361.8 at 1.3821", { timeout: 40_000 });
});
