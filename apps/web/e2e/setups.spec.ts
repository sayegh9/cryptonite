import type { Page } from "@playwright/test";
import { test, expect } from "./fixtures/offline";

/** Setups inbox: list, filters and the row drawer, with the API mocked. Read-only; "Ask the Brain" only pre-fills. */

const A = "5e700000-0000-4000-8000-00000000000a";
const B = "5e700000-0000-4000-8000-00000000000b";
const C = "5e700000-0000-4000-8000-00000000000c";
const OWNER = "5e700000-0000-4000-8000-0000000000f1";

const base = {
  entry: "100.00", stop: "95.00", target: "110.00", created_at: new Date().toISOString(), age_seconds: 600,
  cost: { cost_r: 0.048, cost_model: "spot-v1", market: "spot", hold_hours: 24 },
  policies: [] as unknown[], brief: { matched_rules: ["rsi<30"], metrics: { rsi: 27.5 } },
};
const prov = (preset: string, tf = "1h") => ({ preset, timeframe: tf, scan_run_id: "run-1", spec_version_id: "ver-1" });

const setups = [
  {
    ...base, id: A, instrument_id: "BTCUSDT.BINANCE", direction: "LONG", provenance: prov("swing-trend-pullback-1h"),
    profile: { id: OWNER, name: "BTC swing", status: "ACTIVE", eligible: true, reason: null },
    eligibility: { eligible: true, reason: null },
    funnel: { furthest_stage: "DEBATE", outcome: "SKIPPED", reason: "LOW_CONVICTION", trail: [
      { id: 1, stage: "SCAN", outcome: "CANDIDATE", reason: null, mandate_id: OWNER, ts: null },
      { id: 2, stage: "GATE", outcome: "PASSED", reason: null, mandate_id: OWNER, ts: null },
      { id: 3, stage: "DEBATE", outcome: "SKIPPED", reason: "LOW_CONVICTION", mandate_id: OWNER, ts: null },
    ] },
    policies: [{ policy: "A", decision: "TAKE", side: "LONG", size_factor: "1", reason: "rule", model: null, cost_usd: "0", decided_at: null }],
  },
  {
    ...base, id: B, instrument_id: "SOLUSDT.BINANCE", direction: "SHORT", provenance: prov("scalp-multi-tf-short", "15m"),
    entry: "100.00", stop: "105.00", target: "90.00", profile: null,
    eligibility: { eligible: false, reason: "NO_OWNER" },
    funnel: { furthest_stage: null, outcome: null, reason: null, trail: [] },
  },
  {
    ...base, id: C, instrument_id: "ETHUSDT.BINANCE", direction: "LONG", provenance: prov("swing-trend-pullback-1h"),
    profile: { id: "5e700000-0000-4000-8000-0000000000f2", name: "ETH swing", status: "PAUSED", eligible: false, reason: "PROFILE_PAUSED" },
    eligibility: { eligible: false, reason: "PROFILE_PAUSED" },
    funnel: { furthest_stage: "GATE", outcome: "REFUSED", reason: "NOT_ACTIVE", trail: [
      { id: 4, stage: "GATE", outcome: "REFUSED", reason: "NOT_ACTIVE", mandate_id: "m", ts: null },
    ] },
  },
];

async function mock(page: Page, opts: { fail?: boolean } = {}) {
  const calls: string[] = [];
  await page.route(/\/api\/v1\/setups(\?.*)?$/, (route) => {
    calls.push(route.request().url());
    if (opts.fail) return route.fulfill({ status: 503, json: { detail: "Setups unavailable" } });
    return route.fulfill({ json: { since: new Date().toISOString(), count: setups.length, cost_model_version: "v1", setups } });
  });
  return calls;
}

test.describe("Setups inbox", () => {
  test("lists the candidates with owner, cost and stage", async ({ page }) => {
    const calls = await mock(page);
    await page.goto("/?ws=setups");
    const rows = page.getByTestId("setup-row");
    await expect(rows).toHaveCount(3);
    await expect(rows.first()).toContainText("BTCUSDT");
    await expect(rows.first()).toContainText("BTC swing");
    await expect(rows.first()).toContainText("0.048");
    await expect(rows.first()).toContainText("DEBATE SKIPPED");
    await expect(rows.nth(1)).toContainText("none");
    expect(calls[0]).toContain("since=");
  });

  test("filters by direction, eligibility and owner, and clears them", async ({ page }) => {
    await mock(page);
    await page.goto("/?ws=setups");
    const rows = page.getByTestId("setup-row");
    await expect(rows).toHaveCount(3);

    await page.getByLabel("Direction").selectOption("SHORT");
    await expect(rows).toHaveCount(1);
    await expect(rows.first()).toContainText("SOLUSDT");

    await page.getByLabel("Direction").selectOption("all");
    await page.getByLabel("Eligibility").selectOption("eligible");
    await expect(rows).toHaveCount(1);
    await expect(rows.first()).toContainText("BTCUSDT");

    await page.getByRole("button", { name: "Clear filters" }).click();
    await page.getByLabel("Profile").selectOption("none");
    await expect(rows).toHaveCount(1);
    await page.getByRole("button", { name: "Clear filters" }).click();
    await expect(rows).toHaveCount(3);

    await page.getByTestId("setups-panel").getByLabel("Preset", { exact: true }).selectOption("scalp-multi-tf-short");
    await page.getByLabel("Eligibility").selectOption("eligible");
    await expect(page.getByText("No setups match the filters.")).toBeVisible();
  });

  test("the drawer shows the brief and funnel trail, and Ask the Brain only pre-fills", async ({ page }) => {
    await mock(page);
    await page.goto("/?ws=setups");
    await page.evaluate(() => {
      (window as unknown as { __asks: unknown[] }).__asks = [];
      window.addEventListener("cryptonite:brain-ask", (e) => (window as unknown as { __asks: unknown[] }).__asks.push((e as CustomEvent).detail));
    });
    await page.getByTestId("setup-row").first().click();
    const drawer = page.getByTestId("setup-drawer");
    await expect(drawer).toBeVisible();
    await expect(drawer).toContainText("swing-trend-pullback-1h");
    await expect(drawer).toContainText("rsi<30");
    await expect(drawer.getByTestId("setup-eligibility")).toContainText("Eligible");
    await expect(drawer).toContainText("Policy A: TAKE");
    await expect(drawer.getByTestId("funnel-trail").getByRole("listitem")).toHaveCount(3);
    await expect(drawer.getByTestId("funnel-trail")).toContainText("DEBATE SKIPPED");

    await drawer.getByRole("button", { name: "Ask the Brain" }).click();
    const asks = await page.evaluate(() => (window as unknown as { __asks: { prompt: string; send?: boolean }[] }).__asks);
    expect(asks).toEqual([{ prompt: `Explain setup ${A} and propose an order if it is eligible` }]);

    await page.keyboard.press("Escape");
    await expect(page.getByTestId("setup-drawer")).toHaveCount(0);
  });

  test("a failed load is reported, never shown as an empty inbox", async ({ page }) => {
    await mock(page, { fail: true });
    await page.goto("/?ws=setups");
    await expect(page.getByTestId("setups-panel").getByRole("alert")).toContainText("could not be loaded");
    await expect(page.getByText("No candidates in this window.")).toHaveCount(0);
  });
});
