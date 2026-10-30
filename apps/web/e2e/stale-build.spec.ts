import type { Page } from "@playwright/test";
import { test, expect } from "./fixtures/offline";

/**
 * 2026-10-21: a tab loaded before the 09:15 UTC deploy kept the old order card, created a perp mandate without
 * `allow_perps`, and the order was refused. A tab now asks /build-id which build the server runs (every minute, on
 * focus, when it becomes visible); once that differs from the build it loaded, a banner asks for a reload and the
 * order-card Approve / Confirm, the approval queue's Approve and the mandate create / edit / resume forms are disabled.
 * Reject and Veto (risk going down) stay open. The changed build is simulated by mocking /build-id.
 */

const SESSION = "5f0c3a6e-1b2d-4c3e-9f00-0000000000d1";
const M1 = "11111111-1111-4111-8111-111111111111";

const CARD = {
  kind: "order_card", id: "call-1", instrument_id: "XRPUSDT.BINANCE", side: "BUY", quantity: null, order_type: "MARKET",
  limit_price: null, allocation_pct: 5, stop_loss: 1.36, take_profit: null, rationale: "Breakout above the range.",
  risk_class: "ADD_RISK", status: "PENDING_APPROVAL", trace_id: null, command_id: null,
};

const PREFLIGHT = {
  ok: true, mandate: { id: M1, name: "Alt-momentum", status: "ACTIVE" }, side: "BUY", allocation_pct: 5, budget_usd: 10000,
  clipped_from_pct: null, sell_quantity: null, summary: "BUY XRPUSDT.BINANCE MARKET 5% of $10,000",
};

function mandate(over: Record<string, unknown> = {}) {
  return {
    id: M1, name: "Alt-momentum", capital_budget_usd: "10000", instruments: ["BTCUSDT.BINANCE"], max_position_pct: "10",
    max_drawdown_pct: "20", allow_perps: false, max_leverage: 1, strategies: [], autonomy_level: 1, level_name: "L1 Copilot",
    status: "PAUSED", active_hours_utc: null, veto_window_s: 600, halted_reason: null, halted_at: null,
    level_since: "2026-09-01T00:00:00Z", equity: "10000", hwm: "10000", drawdown_pct: "0", exposure_usd: "0",
    budget_used_pct: "0", positions: [], pending_approvals: 1, pending_proposal: null, ...over,
  };
}

const APPROVAL = {
  id: "aaaaaaaa-0000-4000-8000-000000000001", mandate_id: M1, mandate: "Alt-momentum", action_class: "ADD_RISK",
  command_type: "SUBMIT_ORDER", summary: "BUY ETHUSDT 6% of mandate ($600)", source: "debate", level: 1, timeout_policy: "DENY",
  status: "PENDING", expires_at: new Date(Date.now() + 595_000).toISOString(), seconds_left: 595, decided_via: null,
  decided_by: null, decided_at: null, command_id: null, trace_id: null, payload: {}, created_at: new Date().toISOString(),
};

/** The server moves to another build; focusing the tab makes it ask at once instead of within the minute */
async function deployNewBuild(page: Page) {
  await page.route("**/build-id", (route) => route.fulfill({ json: { build_id: "newer-build" }, headers: { "cache-control": "no-store" } }));
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
}

test.describe("Stale build", () => {
  test("/build-id answers this build, uncached, and a fresh tab shows no banner", async ({ page, request }) => {
    const res = await request.get("/build-id");
    expect(res.ok()).toBe(true);
    expect(res.headers()["cache-control"]).toContain("no-store");
    expect((await res.json()).build_id).toEqual(expect.any(String));

    const answered = page.waitForResponse((r) => r.url().endsWith("/build-id"));
    await page.goto("/");
    await answered;
    await page.evaluate(() => window.dispatchEvent(new Event("focus")));
    await expect(page.getByTestId("stale-build-banner")).toHaveCount(0);
  });

  test("an unreachable /build-id is not a new build", async ({ page }) => {
    await page.route("**/build-id", (route) => route.fulfill({ status: 503, json: { detail: "restarting" } }));
    const answered = page.waitForResponse((r) => r.url().endsWith("/build-id"));
    await page.goto("/");
    await answered;
    await expect(page.getByTestId("stale-build-banner")).toHaveCount(0);
  });

  test("order card: a new build disables Approve and Confirm until reload; Reject stays", async ({ page }) => {
    const approvals: unknown[] = [];
    await page.route("**/api/v1/paper/positions*", (route) => route.fulfill({ json: [] }));
    await page.route("**/agent-api/brain/spend", (route) => route.fulfill({ json: { spent_usd: "0.10", cap_usd: "25.00" } }));
    await page.route(/\/agent-api\/brain\/sessions(\?.*)?$/, (route) => route.fulfill({ json: { sessions: [], next_cursor: null } }));
    await page.route(`**/agent-api/brain/sessions/${SESSION}`, (route) =>
      route.fulfill({
        json: {
          session: { id: SESSION, title: "XRP" },
          summary: null,
          messages: [
            { id: 1, role: "user", content: "analyze XRPUSDT", widgets: [], trace_id: null, created_at: null },
            { id: 2, role: "assistant", content: "Proposed a breakout buy.", widgets: [CARD], trace_id: null, created_at: null },
          ],
        },
      }),
    );
    await page.route(`**/agent-api/brain/sessions/${SESSION}/cards/*/preflight*`, (route) => route.fulfill({ json: PREFLIGHT }));
    await page.route(`**/agent-api/brain/sessions/${SESSION}/cards/*/approve`, (route) => {
      approvals.push(route.request().postDataJSON());
      return route.fulfill({ json: { command_id: "cmd-1", status: "APPROVED" } });
    });
    await page.addInitScript((sid) => {
      localStorage.setItem("csrf_token", "csrf-test");
      localStorage.setItem("cryptonite-brain", JSON.stringify({ state: { sessionId: sid, historyOpen: false }, version: 1 }));
    }, SESSION);
    await page.goto("/?ws=brain&view=home");
    const card = page.getByTestId("brain-widget-order-card");
    await expect(card).toBeVisible();

    // The same build: Approve opens the confirm step, and Confirm is enabled
    await card.getByRole("button", { name: "Approve…" }).click();
    const group = card.getByRole("group", { name: "Confirm paper order" });
    const confirm = group.getByRole("button", { name: "Confirm paper order" });
    await expect(confirm).toBeEnabled();

    // A deploy lands while the confirm step is open
    await deployNewBuild(page);
    const banner = page.getByTestId("stale-build-banner");
    await expect(banner).toBeVisible();
    await expect(banner).toHaveAttribute("role", "alert");
    await expect(banner).toContainText("A new version is live — reload");
    await expect(confirm).toBeDisabled();
    await expect(confirm).toHaveAttribute("title", /reload this tab first/);
    await group.getByRole("button", { name: "Cancel" }).click();
    await expect(card.getByRole("button", { name: "Approve…" })).toBeDisabled();
    await expect(card.getByRole("button", { name: "Reject" })).toBeEnabled();
    expect(approvals).toEqual([]);

    // Reload loads the new build: the server answers the build this tab now runs, and the actions come back
    await page.unroute("**/build-id");
    await banner.getByRole("button", { name: "Reload" }).click();
    await expect(page.getByTestId("brain-widget-order-card")).toBeVisible();
    await expect(page.getByTestId("brain-widget-order-card").getByRole("button", { name: "Approve…" })).toBeEnabled();
    await expect(page.getByTestId("stale-build-banner")).toHaveCount(0);
  });

  test("mission control: a new build disables approving and resuming; veto stays", async ({ page }) => {
    const writes: string[] = [];
    await page.route("**/api/v1/ai/status", (route) =>
      route.fulfill({
        json: {
          kill_switch: { latched: false, reason: null, latched_by: null }, halted_mandates: [],
          account: { equity: "100000", hwm: "100000", drawdown_pct: "0", backstop_pct: "20" },
          exit_monitor_up: true, notifier_up: true, budget_alert: false, degraded: [],
        },
      }),
    );
    await page.route("**/api/v1/ai/mandates", (route) => {
      if (route.request().method() !== "GET") writes.push(`${route.request().method()} mandates`);
      return route.fulfill({ json: { mandates: [mandate()] } });
    });
    await page.route(/\/api\/v1\/ai\/approvals\?/, (route) => route.fulfill({ json: { now: new Date().toISOString(), approvals: [APPROVAL] } }));
    await page.route("**/api/v1/ai/approvals/*/decision", (route) => {
      writes.push("decision");
      return route.fulfill({ json: { approval_id: APPROVAL.id, status: "APPROVED", command_id: "cmd-1", replayed: false } });
    });
    await page.route("**/api/v1/ai/mandates/*/resume", (route) => {
      writes.push("resume");
      return route.fulfill({ json: mandate({ status: "ACTIVE" }) });
    });
    await page.route("**/api/v1/control/kill-switch/status*", (route) =>
      route.fulfill({ json: { environment: "PAPER", account_id: "PAPER-ACCOUNT-001", is_latched: false } }),
    );
    await page.addInitScript(() => localStorage.setItem("csrf_token", "csrf-test"));
    await page.goto("/");
    await page.getByRole("button", { name: "Mission Control", exact: true }).click();
    await expect(page.getByTestId("mission-control")).toBeVisible();

    const row = page.getByTestId("approval-row");
    await expect(row.getByRole("button", { name: `Approve: ${APPROVAL.summary}` })).toBeEnabled();
    const mandateCard = page.getByTestId("mandate-card");
    await mandateCard.getByRole("button", { name: "Resume… 🔐" }).click();
    const resume = mandateCard.getByRole("form", { name: "Resume Alt-momentum" });
    await resume.getByLabel("TOTP").fill("123456");
    await expect(resume.getByRole("button", { name: "Resume" })).toBeEnabled();

    await deployNewBuild(page);
    await expect(page.getByTestId("stale-build-banner")).toBeVisible();
    await expect(row.getByRole("button", { name: `Approve: ${APPROVAL.summary}` })).toBeDisabled();
    await expect(row.getByRole("button", { name: `Veto: ${APPROVAL.summary}` })).toBeEnabled();
    await expect(resume.getByRole("button", { name: "Resume" })).toBeDisabled();
    // Enter in the TOTP box does not submit either
    await resume.getByLabel("TOTP").press("Enter");

    expect(writes).toEqual([]);
  });
});
