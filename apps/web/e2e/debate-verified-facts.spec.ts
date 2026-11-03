import type { Page } from "@playwright/test";
import { test, expect } from "./fixtures/offline";

/**
 * H10: the debate view shows the run's verified facts (the evidence snapshot, computed by code) apart from the
 * agents' arguments, evidence status and non-actionable reasons as warnings, an entry HOLD as "Add no new position",
 * and "No snapshot" for runs from before snapshots. The four paper cases match the Python end-to-end tests in
 * tests/test_h10_evidence_snapshot.py (HOLD, a valid proposal waiting on an unapproved card, a blocked proposal, an
 * existing position); the evidence endpoint is mocked with the shape that endpoint returns.
 */

const RUN = "0b0e1d2a-3c4f-4a5b-8c6d-7e8f90a1b2c3";
const PERP_SHORT = {
  instrument_id: "BTCUSDT-PERP.BINANCE", market: "PERP", side: "SHORT", quantity: 0.02598, entry_price: 76979.4,
  leverage: 2, stop: 77578.61, take_profit: 75581.41, mandate_name: "BTC swing", opened_at: "2026-11-02T22:24:00+00:00",
};

function item(status: string, value: unknown, extra: Record<string, unknown> = {}) {
  return { status, value, source: "code", as_of: "2026-11-03T12:00:00+00:00", unit: null, ...extra };
}

function evidence(over: Record<string, unknown> = {}, positions: unknown[] = []) {
  return {
    run_id: RUN, instrument_id: "BTCUSDT.BINANCE", status: "NO_TRADE", trigger_source: "user_on_demand",
    evidence_status: "OK", failure_reason: null, non_actionable_reason: null,
    verdict: { recommendation: "HOLD", conviction_level: "Hold", conviction_score: 0.6, allocation_pct: 0,
               decision_price: 76900, action_label: "Add no new position" },
    snapshot_version: 1, snapshot_versions: [{ version: 1, created_at: "2026-11-03T12:00:00+00:00", essential_ok: true }],
    snapshot: {
      schema_version: 1, version: 1, purpose: "ENTRY", instrument_id: "BTCUSDT.BINANCE", market: "SPOT",
      built_at: "2026-11-03T12:00:00+00:00", essential: ["bars", "quote", "profile", "positions"],
      optional: ["mark", "funding", "open_interest", "news", "scanner"],
      items: {
        quote: item("OK", 76900.5),
        positions: item("OK", positions),
        profile: item("OK", { name: "BTC swing", status: "ACTIVE", direction: "BOTH" }),
        bars: item("OK", { timeframe: "1h", expected: 48, returned: 48, summary: { net_change_pct: -1.2,
                     window_high: 77900, window_low: 75800 } }),
        funding: item("OK", { rate_raw: 0.0001, rate_pct: 0.01 }),
        open_interest: item("ERROR", null, { reason: "derivatives context could not be read (TimeoutError)" }),
        mark: item("OK", 76910.2),
        scanner: item("OK", []),
        news: item("MISSING", [], { reason: "no recent headlines" }),
      },
    },
    validation: [], routing: { outcome: "NO_TRADE", reason: "HOLD" }, approval: null,
    ...over,
  };
}

async function openDebate(page: Page, body: unknown) {
  await page.addInitScript((runId) => {
    localStorage.setItem("debate-storage", JSON.stringify({
      state: { activeDebates: { [runId]: { runId, instrumentId: "BTCUSDT.BINANCE", status: "NO_TRADE", turns: [
        { runId, role: "Bear Researcher", content: "The agents' argument text.", timestamp: 1 },
      ] } } },
      version: 0,
    }));
  }, RUN);
  await page.route(`**/api/v1/agents/debates/${RUN}/evidence`, (route) => route.fulfill({ json: body }));
  await page.goto("/?ws=terminal");
  await page.getByRole("button", { name: "Show AI Debate Sidebar" }).click();
  return page.getByTestId("verified-facts");
}

test("an entry HOLD reads 'Add no new position' and the facts sit apart from the arguments", async ({ page }) => {
  const facts = await openDebate(page, evidence());
  await expect(facts).toBeVisible();
  await expect(facts.getByTestId("verdict-action")).toHaveText(/Add no new position/);
  await expect(facts).toContainText("0.01% per 8h (raw 0.0001)");
  await expect(facts.getByTestId("evidence-status-open_interest")).toHaveText("ERROR");
  await expect(facts.getByTestId("related-positions")).toContainText("None open on this coin");
  await expect(facts.getByTestId("evidence-warning")).toHaveCount(0);
  await expect(facts).not.toContainText("The agents' argument text.");
  await expect(page.getByText("Agents' arguments")).toBeVisible();
});

test("a valid proposal shows its unapproved card", async ({ page }) => {
  const facts = await openDebate(page, evidence({
    status: "PENDING_APPROVAL",
    verdict: { recommendation: "BUY", conviction_level: "Buy", conviction_score: 0.8, allocation_pct: 3,
               decision_price: 65010, action_label: "BUY" },
    routing: { outcome: "APPROVAL_CREATED", reason: null },
    approval: { id: "a1", status: "PENDING", action_class: "ADD_RISK" },
  }));
  await expect(facts.getByTestId("verdict-action")).toHaveText(/BUY · approval PENDING/);
  await expect(facts.getByTestId("evidence-warning")).toHaveCount(0);
});

test("a blocked proposal is a warning with its named reason, not a confident verdict", async ({ page }) => {
  const reason = "OPPOSITE_POSITION_OPEN: BUY is opposite to the open BTC perp SHORT 2x; no hedge is opened";
  const facts = await openDebate(page, evidence({
    evidence_status: "NON_ACTIONABLE", failure_reason: "OPPOSITE_POSITION_OPEN", non_actionable_reason: reason,
    verdict: { recommendation: "BUY", conviction_level: "Overweight", conviction_score: 0.7, allocation_pct: 6,
               decision_price: 76900, action_label: "BUY" },
    routing: { outcome: "NO_TRADE", reason: "OPPOSITE_POSITION_OPEN" },
  }, [PERP_SHORT]));
  await expect(facts.getByTestId("verdict-action")).toHaveText(/Non-actionable · BUY/);
  await expect(facts.getByTestId("evidence-warning")).toContainText(reason);
});

test("an existing position is listed with its stop, and a no-position claim is flagged", async ({ page }) => {
  const facts = await openDebate(page, evidence({
    evidence_status: "NON_ACTIONABLE", failure_reason: "EVIDENCE_CONTRADICTED",
    non_actionable_reason: "EVIDENCE_CONTRADICTED: Bear Researcher NO_POSITION_CLAIM",
    validation: [{ snapshot_version: 1, role: "Bear Researcher", code: "NO_POSITION_CLAIM", fact: "positions",
                   severity: "BLOCK", detail: "claims 'no position' while BTC perp SHORT 2x is open" }],
  }, [PERP_SHORT]));
  await expect(facts.getByTestId("related-positions")).toContainText("BTCUSDT perp SHORT 2x");
  await expect(facts.getByTestId("related-positions")).toContainText("stop 77578.61");
  await expect(facts.getByTestId("verdict-action")).toHaveText(/Non-actionable · Add no new position/);
  await expect(facts.getByTestId("evidence-warning")).toContainText("Blocks: Bear Researcher · NO_POSITION_CLAIM");
});

test("a run from before snapshots shows 'No snapshot'", async ({ page }) => {
  const facts = await openDebate(page, { ...evidence(), evidence_status: "NO_SNAPSHOT", snapshot: null,
                                         snapshot_version: null, snapshot_versions: [] });
  await expect(facts.getByTestId("no-snapshot")).toHaveText("No snapshot");
});
