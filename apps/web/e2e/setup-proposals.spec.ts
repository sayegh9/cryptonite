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
