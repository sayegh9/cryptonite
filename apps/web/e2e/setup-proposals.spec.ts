import type { Page } from "@playwright/test";
import { test, expect } from "./fixtures/offline";
import { mockAutoTrade, VALID_TOTP } from "./fixtures/autotrade";

/** H13b: Setups to review in the Auto-Trade workspace: a setup the debate said HOLD on, and only the operator's click. */

const SPOT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const PERP_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

function proposal(id: string, instrument: string, side = "BUY", over: Record<string, unknown> = {}) {
  return {
    id, instrument_id: instrument, side, status: "PENDING", refusal: null, command_id: null,
    expires_at: new Date(Date.now() + 600_000).toISOString(), created_at: new Date().toISOString(), decided_at: null,
    mandate_name: "BTC swing paper",
    card: {
      allocation_pct: 25, stop_loss: 58000, take_profit: 65000, setup_grade: "B", grade_reasons: ["trend up"],
      grade_inputs: { entry: 60000 }, against_debate_verdict: "HOLD", debate_reasoning: "Momentum is fading on the higher frame.",
      preset_slug: "trend-pullback", timeframe: "1h",
      ...(instrument.includes("-PERP") ? { spot_levels: { entry: 59970, stop: 57970, target: 64970 }, perp_shift: 30 } : {}),
    },
    ...over,
  };
}

const OK_FLIGHT = {
  ok: true, side: "BUY", allocation_pct: 25, budget_usd: 2500, clipped_from_pct: null, sell_quantity: null,
  mandate: { id: "m", name: "BTC swing paper", status: "ACTIVE" }, summary: "BUY BTCUSDT.BINANCE MARKET at 25% of mandate",
  tp1: { price: 63000, scale_pct: 50, tp_r: 1.5, basis: "r", text: "TP1" },
  live_entry: 60000, live_rr: 2.5, rr_floor: 1.5, meets_rr_floor: true,
};

interface Mock {
  rows: Record<string, unknown>[];
  flight: (id: string) => Record<string, unknown>;
  posts: { path: string; body: unknown }[];
  approve?: { status: number; json: unknown };
}

async function mockProposals(page: Page, rows: Record<string, unknown>[], flight: Mock["flight"] = () => OK_FLIGHT): Promise<Mock> {
  const mock: Mock = { rows, flight, posts: [] };
  await page.route(/\/api\/v1\/proposals/, async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    if (req.method() === "GET") {
      const pending = mock.rows.filter((r) => r.status === "PENDING").length;
      return route.fulfill({ json: { now: new Date().toISOString(), pending, proposals: mock.rows } });
    }
    const [, , , , id, action] = url.pathname.split("/");
    if (action === "preflight") return route.fulfill({ json: mock.flight(id) });
    mock.posts.push({ path: `${id}/${action}`, body: req.postDataJSON() });
    if (action === "approve" && mock.approve) return route.fulfill(mock.approve);
    mock.rows = mock.rows.map((r) => (r.id === id ? { ...r, status: action === "approve" ? "APPROVED" : "REJECTED" } : r));
    return route.fulfill({ json: { status: action === "approve" ? "APPROVED" : "REJECTED", command_id: "c" } });
  });
  return mock;
}

test.describe("Setups to review", () => {
  test("a row shows the setup, the debate's verdict and the plain statement that nothing trades by itself", async ({ page }) => {
    await mockAutoTrade(page);
    await mockProposals(page, [proposal(SPOT_ID, "BTCUSDT.BINANCE")]);
    await page.goto("/?ws=autotrade");
    const section = page.getByTestId("setup-proposals");
    await expect(section).toContainText("Nothing trades unless you approve");
    await expect(page.getByTestId("proposal-count")).toHaveText("1");
    const row = page.getByTestId("proposal-row");
    for (const text of ["BTC", "Long", "Spot", "Grade B", "trend-pullback · 1h", "60,000", "58,000", "63,000",
      "The debate said HOLD.", "Momentum is fading"]) {
      await expect(row).toContainText(text);
    }
    await expect(page.getByTestId("proposal-rr")).toHaveText("2.50x");
    await expect(page.getByTestId("proposal-expiry")).toContainText("expires in");
    await expect(page.getByTestId("proposal-perp-shift")).toHaveCount(0);
  });

  test("approve needs the acknowledgement, and sends it", async ({ page }) => {
    await mockAutoTrade(page);
    const mock = await mockProposals(page, [proposal(SPOT_ID, "BTCUSDT.BINANCE")]);
    await page.goto("/?ws=autotrade");
    const approve = page.getByRole("button", { name: "Approve BTC" });
    await expect(approve).toBeDisabled();
    await page.getByLabel("The debate said HOLD").check();
    await approve.click();
    await expect(page.getByTestId("proposal-count")).toHaveText("0");
    expect(mock.posts).toEqual([{ path: `${SPOT_ID}/approve`, body: { acknowledge_against_debate: true } }]);
  });

  test("a perp row needs TOTP, and sends the leverage chosen", async ({ page }) => {
    await mockAutoTrade(page);
    const mock = await mockProposals(page, [proposal(PERP_ID, "BTCUSDT-PERP.BINANCE", "SELL")], () => ({
      ...OK_FLIGHT, side: "SELL", needs_totp: true, high_leverage_confirm: false,
      leverage_plan: { ok: true, leverage: 2, leverage_label: "2x", reject: null },
    }));
    await page.goto("/?ws=autotrade");
    const row = page.getByTestId("proposal-row");
    await expect(row).toContainText("Short");
    await expect(row).toContainText("Perp");
    await expect(page.getByTestId("proposal-perp-shift")).toHaveText("Levels moved to the perp price by +30");
    await page.getByLabel("The debate said HOLD").check();
    const approve = page.getByRole("button", { name: "Approve BTC" });
    await expect(approve).toBeDisabled();
    await page.getByLabel("Leverage (at most)").selectOption("2");
    await page.getByLabel("TOTP code").fill(VALID_TOTP);
    await approve.click();
    await expect.poll(() => mock.posts.length).toBe(1);
    expect(mock.posts[0].body).toEqual({ acknowledge_against_debate: true, totp_code: VALID_TOTP, leverage: 2 });
  });

  test("a slip below the floor says so and cannot be approved", async ({ page }) => {
    await mockAutoTrade(page);
    await mockProposals(page, [proposal(SPOT_ID, "BTCUSDT.BINANCE")], () => ({
      ok: false, code: "REWARD_TO_RISK_SLIPPED", detail: "REWARD_TO_RISK_SLIPPED: at the live price", needs_mandate: false,
      remedy: null, live_entry: 62500, live_rr: 0.56, rr_floor: 1.5, meets_rr_floor: false,
    }));
    await page.goto("/?ws=autotrade");
    await expect(page.getByTestId("proposal-floor")).toHaveText("Below the 1.5 floor, cannot be approved.");
    await page.getByLabel("The debate said HOLD").check();
    await expect(page.getByRole("button", { name: "Approve BTC" })).toBeDisabled();
  });

  test("a refused approve shows the server's reason and keeps the row", async ({ page }) => {
    await mockAutoTrade(page);
    const mock = await mockProposals(page, [proposal(SPOT_ID, "BTCUSDT.BINANCE")]);
    mock.approve = { status: 409, json: { detail: "MANDATE_HALTED: approve inside an ACTIVE mandate" } };
    await page.goto("/?ws=autotrade");
    await page.getByLabel("The debate said HOLD").check();
    await page.getByRole("button", { name: "Approve BTC" }).click();
    await expect(page.getByRole("alert").filter({ hasText: "MANDATE_HALTED" })).toBeVisible();
    await expect(page.getByTestId("proposal-row")).toHaveCount(1);
  });

  test("reject removes the row, and decided rows can be shown", async ({ page }) => {
    await mockAutoTrade(page);
    const mock = await mockProposals(page, [proposal(SPOT_ID, "BTCUSDT.BINANCE")]);
    await page.goto("/?ws=autotrade");
    await page.getByRole("button", { name: "Reject BTC" }).click();
    await expect(page.getByTestId("proposal-row")).toHaveCount(0);
    expect(mock.posts[0].path).toBe(`${SPOT_ID}/reject`);
    await page.getByRole("button", { name: /Show decided/ }).click();
    await expect(page.getByTestId("proposal-decided")).toContainText("REJECTED");
  });

  test("an expired row cannot be approved", async ({ page }) => {
    await mockAutoTrade(page);
    await mockProposals(page, [proposal(SPOT_ID, "BTCUSDT.BINANCE", "BUY", { expires_at: new Date(Date.now() - 1000).toISOString() })]);
    await page.goto("/?ws=autotrade");
    await expect(page.getByTestId("proposal-expiry")).toHaveText("expired");
    await page.getByLabel("The debate said HOLD").check();
    await expect(page.getByRole("button", { name: "Approve BTC" })).toBeDisabled();
  });

  test("a failed load is not shown as an empty queue", async ({ page }) => {
    await mockAutoTrade(page);
    await page.goto("/?ws=autotrade");
    await expect(page.getByTestId("proposal-error")).toBeVisible();
    await expect(page.getByTestId("proposal-count")).toHaveText("?");
  });
});

const FUNNEL = {
  now: new Date().toISOString(), hours: 24, rows_truncated: false,
  counts: {
    evaluations: { NO_TRIGGER: 1200, REJECTED_RR: 30, STALE: 2, MATCHED_FRESH: 17 }, candidates: 17, gate_passed: 9, gate_refused: 8,
    verdicts: { HOLD: 9 }, proposals: { PENDING: 0, APPROVED: 0, REJECTED: 0, EXPIRED: 1 }, approved: 0, unaccounted: 1,
  },
  caveats: ["What-if figures are model figures, not trades.", "Setups overlap, so these are not independent wins: several rows can be the same move."],
  rows: [
    { candidate_id: "c1", time: "2026-11-04T10:00:00+00:00", instrument_id: "WLDUSDT.BINANCE", coin: "WLD", timeframe: "1h", preset: "momentum-breakout-with-a-long-name",
      direction: "LONG", entry: 1.2, stop: 1.1, target: 1.5, stage: "UNACCOUNTED", reason: "QUEUED_NO_DEBATE", verdict: null, policy_a: null, policy_b: null, policy_c: null,
      what_if_r: null, what_if_state: "PENDING", what_if_exit: null },
    { candidate_id: "c2", time: "2026-11-04T09:00:00+00:00", instrument_id: "BTCUSDT.BINANCE", coin: "BTC", timeframe: "15m", preset: "trend-pullback",
      direction: "SHORT", entry: 60000, stop: 61000, target: 58000, stage: "PROPOSAL_SKIPPED", reason: "RR_BELOW_FLOOR", verdict: "HOLD", policy_a: "TAKE", policy_b: "SKIP", policy_c: "ERROR",
      what_if_r: 1.25, what_if_state: "PROVISIONAL", what_if_exit: "OPEN" },
    { candidate_id: "c3", time: "2026-11-04T08:00:00+00:00", instrument_id: "ETHUSDT.BINANCE", coin: "ETH", timeframe: "1h", preset: "trend-pullback",
      direction: "LONG", entry: 2300, stop: 2250, target: 2400, stage: "EXPIRED", reason: null, verdict: "HOLD", policy_a: "TAKE", policy_b: "SKIP", policy_c: "TAKE",
      what_if_r: -1, what_if_state: "FINAL", what_if_exit: null },
  ],
};

test.describe("What happened to setups", () => {
  async function open(page: Page) {
    await mockAutoTrade(page);
    await mockProposals(page, []);
    await page.route(/\/api\/v1\/funnel/, (route) => route.fulfill({ json: FUNNEL }));
    await page.goto("/?ws=autotrade");
    await page.getByRole("button", { name: "What happened to setups" }).click();
  }

  test("counts, stages in plain language, what-if states and the caveats are shown; unaccounted stands out", async ({ page }) => {
    await open(page);
    await expect(page.getByTestId("funnel-counts")).toContainText("no trigger 1,200");
    await expect(page.getByTestId("funnel-counts")).toContainText("17 setups");
    await expect(page.getByTestId("funnel-counts")).toContainText("unaccounted 1");
    await expect(page.getByTestId("funnel-caveats")).toContainText("model figures");
    await expect(page.getByTestId("funnel-caveats")).toContainText("not independent wins");
    const rows = page.getByTestId("funnel-row");
    await expect(rows).toHaveCount(3);
    await expect(rows.nth(0)).toHaveAttribute("data-stage", "UNACCOUNTED");
    await expect(rows.nth(0)).toHaveClass(/bg-amber/);
    await expect(rows.nth(0)).toContainText("nothing records where it stopped");
    await expect(rows.nth(0)).toContainText("queued for a debate that never started");
    await expect(rows.nth(0).getByTestId("funnel-what-if")).toHaveText("not scored yet");
    await expect(rows.nth(1)).toContainText("The debate said HOLD and no card was made");
    await expect(rows.nth(1)).toContainText("reward to risk is below the floor");
    await expect(rows.nth(1).getByTestId("funnel-what-if")).toHaveText("+1.25R provisional, still open");
    await expect(rows.nth(1)).not.toHaveClass(/bg-amber/);
    await expect(rows.nth(2).getByTestId("funnel-what-if")).toHaveText("-1.00R final");
  });

  test("the view fits a phone without page-level sideways scrolling", async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 812 });
    await open(page);
    await expect(page.getByTestId("funnel-row")).toHaveCount(3);
    const overflow = await page.evaluate(() => {
      const view = document.querySelector('[data-testid="funnel-view"]')!;
      const wide = view.querySelector(".overflow-x-auto")!;
      return { page: document.documentElement.scrollWidth - window.innerWidth, table: wide.scrollWidth - wide.clientWidth };
    });
    expect(overflow.page).toBeLessThanOrEqual(0);
    expect(overflow.table).toBeLessThanOrEqual(0);
  });
});
