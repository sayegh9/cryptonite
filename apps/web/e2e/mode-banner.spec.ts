import { test, expect, type Page } from "@playwright/test";

/**
 * G1 truthful mode banner (F-M12): the header label is computed from /api/v1/ai/mandates and
 * /api/v1/ai/status (stubbed here), never hardcoded, and says "unknown" when those fail.
 */

function mandate(id: string, over: Record<string, unknown> = {}) {
  return {
    id,
    name: `Mandate ${id.slice(0, 4)}`,
    capital_budget_usd: "10000",
    instruments: ["BTCUSDT.BINANCE"],
    max_position_pct: "10",
    max_drawdown_pct: "20",
    allow_perps: false,
    max_leverage: 1,
    strategies: [],
    autonomy_level: 1,
    level_name: "L1 Copilot",
    status: "ACTIVE",
    active_hours_utc: null,
    veto_window_s: 600,
    halted_reason: null,
    halted_at: null,
    level_since: "2026-09-01T00:00:00Z",
    equity: "10000",
    hwm: "10000",
    drawdown_pct: "0",
    exposure_usd: "0",
    budget_used_pct: "0",
    positions: [],
    pending_approvals: 0,
    pending_proposal: null,
    ...over,
  };
}

const A = "aaaaaaaa-0000-4000-8000-000000000001";
const B = "bbbbbbbb-0000-4000-8000-000000000002";
const C = "cccccccc-0000-4000-8000-000000000003";

function status(latched = false, reason: string | null = null) {
  return {
    kill_switch: { latched, reason, latched_by: latched ? "operator" : null },
    halted_mandates: [],
    account: null,
    exit_monitor_up: true,
    notifier_up: true,
    budget_alert: false,
    degraded: [],
  };
}

type Reply = { json: unknown } | { status: number };

async function stub(page: Page, opts: { mandates: Reply; status: Reply }) {
  const reply = (r: Reply) => ("json" in r ? { json: r.json } : { status: r.status, json: { detail: "stubbed failure" } });
  await page.route("**/api/v1/ai/mandates", (route) => route.fulfill(reply(opts.mandates)));
  await page.route("**/api/v1/ai/status", (route) => route.fulfill(reply(opts.status)));
  await page.route(/\/api\/v1\/ai\/approvals\?/, (route) =>
    route.fulfill({ json: { now: new Date().toISOString(), approvals: [] } }),
  );
}

const header = (page: Page) => page.locator("header").getByTestId("mode-banner");
const headerText = (page: Page) => header(page).getByTestId("mode-banner-text");

test.describe("Mode banner (G1)", () => {
  test.beforeEach(async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem("cryptonite:ai-brain-tour-seen", "true"));
  });

  test("L1 mandates: approvals required, no hardcoded authority claims", async ({ page }) => {
    await stub(page, { mandates: { json: { mandates: [mandate(A), mandate(B), mandate(C, { status: "ARCHIVED" })] } }, status: { json: status() } });
    await page.goto("/?ws=markets");
    await expect(headerText(page)).toHaveText("PAPER · 2 mandates at L1 · approvals required");
    await expect(header(page)).toHaveAttribute("data-tone", "gated");
    await expect(header(page)).toHaveAttribute("title", /Every risk-adding order waits for your approval/);
    await expect(page.getByText("ZERO TRADING AUTHORITY")).toHaveCount(0);
  });

  test("mixed levels show the strongest authority and halted mandates", async ({ page }) => {
    await stub(page, {
      mandates: {
        json: {
          mandates: [
            mandate(A),
            mandate(B, { autonomy_level: 2, level_name: "L2 Supervised" }),
            mandate(C, { status: "HALTED", halted_reason: "hard stop" }),
          ],
        },
      },
      status: { json: status() },
    });
    await page.goto("/?ws=markets");
    await expect(headerText(page)).toHaveText("PAPER · 1 mandate at L1, 1 at L2 · acts after veto window · 1 halted");
    await expect(header(page)).toHaveAttribute("data-tone", "acting");
  });

  test("kill switch latched outranks mandates", async ({ page }) => {
    await stub(page, { mandates: { json: { mandates: [mandate(A)] } }, status: { json: status(true, "drawdown backstop") } });
    await page.goto("/?ws=markets");
    await expect(headerText(page)).toHaveText("PAPER · kill switch latched");
    await expect(header(page)).toHaveAttribute("data-tone", "latched");
    await expect(header(page)).toHaveAttribute("title", /latched: drawdown backstop/);
  });

  test("no active mandates", async ({ page }) => {
    await stub(page, { mandates: { json: { mandates: [mandate(A, { status: "PAUSED" })] } }, status: { json: status() } });
    await page.goto("/?ws=markets");
    await expect(headerText(page)).toHaveText("PAPER · no active mandates · 1 paused");
    await expect(header(page)).toHaveAttribute("data-tone", "idle");
  });

  test("status fetch fails: kill switch unknown", async ({ page }) => {
    await stub(page, { mandates: { json: { mandates: [mandate(A)] } }, status: { status: 500 } });
    await page.goto("/?ws=markets");
    await expect(headerText(page)).toHaveText("PAPER · 1 mandate at L1 · approvals required · kill switch unknown");
  });

  test("both fetches fail: status unknown", async ({ page }) => {
    await stub(page, { mandates: { status: 502 }, status: { status: 502 } });
    await page.goto("/?ws=markets");
    await expect(headerText(page)).toHaveText("PAPER · status unknown");
    await expect(header(page)).toHaveAttribute("data-tone", "unknown");
    await expect(header(page)).toHaveAttribute("title", /could not be loaded/);
  });

  test("signed out: status unknown with a sign-in hint", async ({ page }) => {
    await stub(page, { mandates: { status: 401 }, status: { status: 401 } });
    await page.goto("/?ws=markets");
    await expect(headerText(page)).toHaveText("PAPER · status unknown");
    await expect(header(page)).toHaveAttribute("title", /Sign in as operator/);
  });

  test("AI Brain shows the same computed banner, not RISK-FREE", async ({ page }) => {
    await stub(page, { mandates: { json: { mandates: [mandate(A)] } }, status: { json: status() } });
    await page.goto("/?ws=brain&view=home");
    await expect(page.getByTestId("brain-home")).toBeVisible();
    const banners = page.getByTestId("mode-banner");
    await expect(banners).toHaveCount(2);
    await expect(banners.nth(1).getByTestId("mode-banner-text")).toHaveText("PAPER · 1 mandate at L1 · approvals required");
    await expect(page.getByText("RISK-FREE")).toHaveCount(0);
  });
});
