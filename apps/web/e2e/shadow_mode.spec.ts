import { test, expect, type Page } from "@playwright/test";

/**
 * F2 shadow mode panel (Mission Control → Autonomy & Evals), read-only, with stubbed APIs: shadow routes
 * with their config, agreement / latency / errors per shadow model, the role filter and an unavailable API.
 */

const M1 = "11111111-1111-4111-8111-111111111111";

const MANDATE = {
  id: M1, name: "Alt-momentum", capital_budget_usd: "10000", instruments: ["BTCUSDT.BINANCE"], max_position_pct: "10",
  max_drawdown_pct: "20", allow_perps: false, max_leverage: 1, strategies: [], autonomy_level: 1, level_name: "L1 Copilot",
  status: "ACTIVE", active_hours_utc: null, veto_window_s: 600, halted_reason: null, halted_at: null,
  level_since: "2026-09-01T00:00:00Z", equity: "10420", hwm: "10600", drawdown_pct: "1.70", exposure_usd: "1200",
  budget_used_pct: "12", positions: [], pending_approvals: 0, pending_proposal: null,
};

const AUTONOMY = {
  mandate_id: M1, name: "Alt-momentum", status: "ACTIVE", level: 1, level_name: "L1 Copilot", level_since: "2026-08-25T00:00:00Z",
  requested_level: null, proposal: null,
  gate: { level: 1, target_level: 2, passed: false, checks: [], metrics: {} },
  events: [],
};

const stats = (role: string, shadow_model: string, extra: Record<string, unknown> = {}) => ({
  role, shadow_model, primary_models: ["ollama:gemma4:12b-it-qat"], n: 52, ok: 48, errors: 2, skipped: 2,
  skip_reasons: { SHADOW_BUDGET_DEGRADED: 2 }, key_field: "worth_debate",
  agreement: { n: 48, agree: 42, rate: 0.875 },
  latency_ms: { primary_p50: 700, primary_p95: 1900, shadow_p50: 420, shadow_p95: 1250 },
  cost_usd: { shadow: "0.000000" },
  outcomes: {
    horizon: "24h", labelled: 6, agreement_rate: 0.5, debated: { wins: 3, losses: 3, neutral: 0 },
    shadow_would_skip: { n: 3, wins: 0, losses: 3, neutral: 0 },
  },
  ...extra,
});

const REPORT = {
  available: true, days: 7, role: null, allow_paid: false,
  routes: [
    { role: "triage", primary_model: "ollama:gemma4:12b-it-qat", fallback_model: "google:gemini-2.5-flash-lite", fallback_policy: "DEGRADE",
      priority: "debate", shadow_model: "ollama:qwen3.5:4b", shadow_sample_rate: 0.25, shadow_paid: false, active: true,
      updated_at: "2026-10-05T10:00:00+00:00" },
    { role: "debate.verdict", primary_model: "google:gemini-2.5-pro", fallback_model: null, fallback_policy: "FAIL_CLOSED",
      priority: "debate", shadow_model: "google:gemini-2.5-flash", shadow_sample_rate: 0.1, shadow_paid: true, active: false,
      updated_at: null },
  ],
  models: [
    stats("debate.verdict", "google:gemini-2.5-flash", {
      n: 3, ok: 0, errors: 0, skipped: 3, skip_reasons: { PAID_SHADOW_NOT_ALLOWED: 3 }, key_field: "recommendation",
      agreement: { n: 0, agree: 0, rate: null },
      latency_ms: { primary_p50: null, primary_p95: null, shadow_p50: null, shadow_p95: null }, outcomes: null,
    }),
    stats("triage", "ollama:qwen3.5:4b"),
  ],
  roles: [],
};

async function mockMission(page: Page, shadow: (url: URL) => { status?: number; json: unknown }) {
  await page.route("**/api/v1/ai/status", (route) =>
    route.fulfill({
      json: {
        kill_switch: { latched: false, reason: null, latched_by: null }, halted_mandates: [],
        account: { equity: "100420", hwm: "101000", drawdown_pct: "0.57", backstop_pct: "20" },
        exit_monitor_up: true, notifier_up: true, budget_alert: false, degraded: [],
      },
    }),
  );
  await page.route("**/api/v1/ai/mandates", (route) => route.fulfill({ json: { mandates: [MANDATE] } }));
  await page.route(/\/api\/v1\/ai\/approvals\?/, (route) => route.fulfill({ json: { now: new Date().toISOString(), approvals: [] } }));
  await page.route("**/api/v1/ai/mandates/*/autonomy", (route) => route.fulfill({ json: AUTONOMY }));
  await page.route("**/api/v1/ai/evals", (route) =>
    route.fulfill({ json: { runs: [], calibration: null, calibration_note: "0 labelled decisions so far." } }),
  );
  await page.route(/\/api\/v1\/ai\/outcomes\/by-regime/, (route) => route.fulfill({ status: 500, json: { detail: "down" } }));
  await page.route("**/api/v1/control/kill-switch/status*", (route) =>
    route.fulfill({ json: { environment: "PAPER", account_id: "PAPER-ACCOUNT-001", is_latched: false } }),
  );
  await page.route(/\/api\/v1\/ai\/shadow/, (route) => {
    const res = shadow(new URL(route.request().url()));
    return route.fulfill({ status: res.status ?? 200, json: res.json });
  });
}

async function openAutonomy(page: Page) {
  await page.addInitScript(() => localStorage.setItem("csrf_token", "csrf-test"));
  await page.goto("/");
  await page.getByRole("button", { name: "Mission Control", exact: true }).click();
  await expect(page.getByTestId("mission-control")).toBeVisible();
  await page.getByRole("tab", { name: "Autonomy & Evals" }).click();
}

test.describe("F2 shadow mode panel", () => {
  test("lists shadow routes with config, agreement, latency and errors; read-only", async ({ page }) => {
    const calls: URL[] = [];
    const writes: string[] = [];
    page.on("request", (r) => {
      if (r.url().includes("/api/v1/") && r.method() !== "GET") writes.push(`${r.method()} ${r.url()}`);
    });
    await mockMission(page, (url) => {
      calls.push(url);
      const role = url.searchParams.get("role");
      if (!role) return { json: REPORT };
      return { json: { ...REPORT, role, models: REPORT.models.filter((m) => m.role === role) } };
    });
    await openAutonomy(page);

    const panel = page.getByTestId("shadow-panel");
    await expect(panel).toBeVisible();
    await expect(page.getByTestId("shadow-route-triage")).toContainText("ollama:qwen3.5:4b at 25.0%");
    await expect(page.getByTestId("shadow-route-triage")).toContainText("active");
    await expect(page.getByTestId("shadow-route-debate.verdict")).toContainText("inactive: paid not allowed");

    const table = page.getByRole("table", { name: "Shadow results" });
    await expect(table.getByRole("row")).toHaveCount(3);
    const triage = page.getByTestId("shadow-row-triage");
    await expect(triage).toContainText("87.5% (42/48)");
    await expect(triage).toContainText("420 ms / 1.3 s");
    await expect(triage).toContainText("vs 700 ms / 1.9 s");
    await expect(page.getByTestId("shadow-outcome-triage")).toContainText("shadow would have skipped 3 (0 won, 3 lost)");
    await expect(page.getByTestId("shadow-row-debate.verdict")).toContainText("—");

    // config is view-only
    await expect(page.getByTestId("shadow-config-triage")).toHaveCount(0);
    await page.getByTestId("shadow-route-triage").getByRole("button", { name: "View config" }).click();
    const config = page.getByTestId("shadow-config-triage");
    await expect(config).toContainText("google:gemini-2.5-flash-lite (DEGRADE)");
    await expect(config).toContainText("ollama:qwen3.5:4b (free)");
    await expect(config.locator("input, textarea, select")).toHaveCount(0);

    // the role filter queries the API with ?role=
    await panel.getByLabel("Role").selectOption("triage");
    await expect(table.getByRole("row")).toHaveCount(2);
    await expect(page.getByTestId("shadow-route-debate.verdict")).toHaveCount(0);
    expect(calls.some((u) => u.searchParams.get("role") === "triage" && u.searchParams.get("days") === "7")).toBe(true);

    await panel.getByLabel("Window").selectOption("30");
    await expect.poll(() => calls.some((u) => u.searchParams.get("days") === "30")).toBe(true);
    expect(writes).toEqual([]);
  });

  test("empty and unavailable states", async ({ page }) => {
    let available = true;
    await mockMission(page, () =>
      available
        ? { json: { available: true, days: 7, role: null, allow_paid: false, routes: [], models: [], roles: [] } }
        : { status: 500, json: { detail: "down" } },
    );
    await openAutonomy(page);
    await expect(page.getByTestId("shadow-routes-empty")).toHaveText("No route has a shadow model.");
    await expect(page.getByTestId("shadow-results-empty")).toBeVisible();

    available = false;
    await page.getByTestId("shadow-panel").getByLabel("Window").selectOption("1");
    await expect(page.getByTestId("shadow-panel").getByRole("alert")).toBeVisible();
    await expect(page.getByRole("table", { name: "Shadow results" })).toHaveCount(0);
  });
});
