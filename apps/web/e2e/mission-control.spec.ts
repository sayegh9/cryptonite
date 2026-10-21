import { test, expect, type Page } from "@playwright/test";

/**
 * Mission Control (blueprint A5.6) with a mocked /api/v1/ai backend:
 * approve / veto flow with countdowns, halted state with a TOTP resume, degraded banner,
 * the Mandate editor's TOTP rule, and a TOTP-confirmed promotion in Autonomy & Evals.
 */

const M1 = "11111111-1111-4111-8111-111111111111";
const M2 = "22222222-2222-4222-8222-222222222222";

function mandate(over: Record<string, unknown> = {}) {
  return {
    id: M1,
    name: "Alt-momentum",
    capital_budget_usd: "10000",
    instruments: ["BTCUSDT.BINANCE", "ETHUSDT.BINANCE"],
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
    equity: "10420",
    hwm: "10600",
    drawdown_pct: "1.70",
    exposure_usd: "1200",
    budget_used_pct: "12",
    positions: [],
    pending_approvals: 2,
    pending_proposal: null,
    ...over,
  };
}

function approval(id: string, action: "ADD_RISK" | "REDUCE_RISK", summary: string, secondsLeft: number) {
  return {
    id,
    mandate_id: M1,
    mandate: "Alt-momentum",
    action_class: action,
    command_type: action === "ADD_RISK" ? "SUBMIT_ORDER" : "CLOSE_POSITION",
    summary,
    source: "debate",
    level: 1,
    timeout_policy: action === "ADD_RISK" ? "DENY" : "PROCEED",
    status: "PENDING",
    expires_at: new Date(Date.now() + secondsLeft * 1000).toISOString(),
    seconds_left: secondsLeft,
    decided_via: null,
    decided_by: null,
    decided_at: null,
    command_id: null,
    trace_id: "a91f0000-0000-4000-8000-000000000042",
    payload: {},
    created_at: new Date().toISOString(),
  };
}

const OK_STATUS = {
  kill_switch: { latched: false, reason: null, latched_by: null },
  halted_mandates: [],
  account: { equity: "100420", hwm: "101000", drawdown_pct: "0.57", backstop_pct: "20" },
  exit_monitor_up: true,
  notifier_up: true,
  budget_alert: false,
  degraded: [],
};

function collectReactErrors(page: Page): string[] {
  const errors: string[] = [];
  const reactError = /hydrat|did not match|Maximum update depth|Minified React error|Cannot update a component/i;
  page.on("pageerror", (err) => errors.push(`pageerror: ${err.message}`));
  page.on("console", (msg) => {
    if (msg.type() === "error" && reactError.test(msg.text())) errors.push(msg.text());
  });
  return errors;
}

async function mockBackend(page: Page, opts: { status?: unknown; mandates?: unknown[]; approvals?: unknown[] } = {}) {
  const calls = { decisions: [] as any[], resumes: [] as any[], creates: [] as any[], confirms: [] as any[] };
  let approvals = opts.approvals ?? [];
  let mandates = opts.mandates ?? [mandate()];
  await page.route("**/api/v1/ai/status", (route) => route.fulfill({ json: opts.status ?? OK_STATUS }));
  await page.route("**/api/v1/ai/mandates", (route) => {
    if (route.request().method() === "POST") {
      const body = route.request().postDataJSON();
      calls.creates.push({ body, csrf: route.request().headers()["x-csrf-token"] });
      const created = mandate({ id: M2, name: body.name, capital_budget_usd: body.capital_budget_usd, instruments: body.instruments });
      mandates = [...mandates, created];
      return route.fulfill({ status: 201, json: created });
    }
    return route.fulfill({ json: { mandates } });
  });
  await page.route(/\/api\/v1\/ai\/approvals\?/, (route) =>
    route.fulfill({ json: { now: new Date().toISOString(), approvals } }),
  );
  await page.route("**/api/v1/ai/approvals/*/decision", (route) => {
    const id = new URL(route.request().url()).pathname.split("/")[5];
    const body = route.request().postDataJSON();
    calls.decisions.push({ id, ...body, csrf: route.request().headers()["x-csrf-token"] });
    approvals = approvals.filter((a: any) => a.id !== id); // decided: no longer pending on the server
    const status = body.decision === "APPROVE" ? "APPROVED" : "VETOED";
    return route.fulfill({ json: { approval_id: id, status, command_id: status === "APPROVED" ? "cmd-9f8e7d6c" : null, replayed: false } });
  });
  await page.route(`**/api/v1/ai/mandates/*/resume`, (route) => {
    calls.resumes.push(route.request().postDataJSON());
    mandates = mandates.map((m: any) => ({ ...m, status: "ACTIVE", halted_reason: null }));
    return route.fulfill({ json: mandates[0] });
  });
  await page.route("**/api/v1/ai/mandates/*/autonomy", (route) =>
    route.fulfill({
      json: {
        mandate_id: M1,
        name: "Alt-momentum",
        status: "ACTIVE",
        level: 1,
        level_name: "L1 Copilot",
        level_since: "2026-08-25T00:00:00Z",
        requested_level: 2,
        proposal: { id: 77, from_level: 1, to_level: 2, created_at: "2026-09-30T09:00:00Z" },
        gate: {
          level: 1,
          target_level: 2,
          passed: true,
          checks: [
            { name: "days_at_level", required: ">= 30 d", actual: "36.0 d", passed: true },
            { name: "closed_trades", required: ">= 30", actual: "41", passed: true },
            { name: "max_drawdown_pct", required: "<= 10%", actual: "6.2", passed: true },
            { name: "mandate_breaches", required: "0", actual: "0", passed: true },
            { name: "sortino_vs_btc_hold", required: "> BTC hold (after fees)", actual: "1.80 vs 0.90", passed: true },
            { name: "expectancy_lower_bound", required: "> 0", actual: "0.12%", passed: false, informational: true, interval: ["-0.05%", "0.29%"] },
            { name: "sortino_diff_lower_bound", required: "> 0", actual: "0.90", passed: true, informational: true, interval: ["0.10", "1.70"] },
          ],
          metrics: {},
        },
        events: [{ id: 1, kind: "PROPOSED", from_level: 1, to_level: 2, reason: "gate for L2 passed", actor: "system", created_at: "2026-09-30T09:00:00Z" }],
      },
    }),
  );
  await page.route("**/api/v1/ai/mandates/*/autonomy/confirm", (route) => {
    calls.confirms.push(route.request().postDataJSON());
    return route.fulfill({
      json: {
        mandate_id: M1, name: "Alt-momentum", status: "ACTIVE", level: 2, level_name: "L2 Supervised", level_since: "2026-09-30T10:00:00Z",
        requested_level: null, proposal: null,
        gate: { level: 2, target_level: 3, passed: false, checks: [{ name: "calibration_error_pp", required: "within ±10 pp", actual: "not measured", passed: false }], metrics: {} },
        events: [],
      },
    });
  });
  await page.route("**/api/v1/ai/evals", (route) =>
    route.fulfill({
      json: {
        runs: [{ id: "r1", set_version: "v2", model: "google:gemini-2.5-flash-lite", passed: true, metrics: {}, created_at: "2026-09-25T00:00:00Z" }],
        calibration: null,
        calibration_note: "Calibration is measured from A7.2 (outcome labels); until then the L3 gate fails closed.",
      },
    }),
  );
  // Fail fast instead of proxying to :8000 (a half-open listener there hangs the request past the expect timeout)
  await page.route("**/api/v1/ai/outcomes/by-regime*", (route) => route.fulfill({ status: 503, json: { detail: "offline" } }));
  await page.route("**/api/v1/control/kill-switch/status*", (route) =>
    route.fulfill({ json: { environment: "PAPER", account_id: "PAPER-ACCOUNT-001", is_latched: false } }),
  );
  return calls;
}

async function openMissionControl(page: Page) {
  await page.addInitScript(() => localStorage.setItem("csrf_token", "csrf-test"));
  await page.goto("/");
  await page.getByRole("button", { name: "Mission Control", exact: true }).click();
  await expect(page.getByTestId("mission-control")).toBeVisible();
}

test.describe("Mission Control", () => {
  test("a mandate card lists its perps with leverage and distance to liquidation", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    await mockBackend(page, {
      mandates: [mandate({
        allow_perps: true, max_leverage: 7, perp_exposure_usd: "1800",
        positions: [{
          instrument_id: "BTCUSDT-PERP.BINANCE", side: "LONG", quantity: "0.03", entry_price: "60000", current_price: "54000",
          leverage: 7, margin_mode: "ISOLATED", isolated_margin: "257.14", notional_usd: "1620", liquidation_price: "52000",
          liquidation_distance_pct: "3.7",
        }],
      })],
    });
    await openMissionControl(page);
    const perps = page.getByTestId("mandate-perps").first();
    await expect(perps).toContainText("Perps · exposure $1,800");
    const row = perps.getByTestId("mandate-perp-position");
    await expect(row).toContainText("BTCUSDT-PERP LONG 7x ISOLATED · $1,620");
    await expect(row).toContainText("liq 52,000 · 3.7% away");
    await expect(row.getByText("liq 52,000 · 3.7% away")).toHaveClass(/text-leverage-liquidation/);
    expect(reactErrors).toEqual([]);
  });

  test("approve and veto pending approvals with countdowns", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    const calls = await mockBackend(page, {
      approvals: [
        approval("aaaaaaaa-0000-4000-8000-000000000001", "ADD_RISK", "BUY ETHUSDT 6% of mandate ($600)", 595),
        approval("bbbbbbbb-0000-4000-8000-000000000002", "REDUCE_RISK", "Close BTCUSDT (exit debate)", 45),
      ],
    });
    await openMissionControl(page);

    const card = page.getByTestId("mandate-card");
    await expect(card).toContainText("Alt-momentum");
    await expect(card.getByTestId("mandate-level")).toHaveText("L1 Copilot");
    await expect(page.getByRole("meter", { name: "Drawdown vs hard stop — Alt-momentum" })).toHaveAttribute(
      "aria-valuetext",
      "1.70% of the 20% hard stop",
    );
    await expect(page.getByRole("meter", { name: "Budget used — Alt-momentum" })).toHaveAttribute("aria-valuetext", /\$1,200 of \$10,000/);

    const rows = page.getByTestId("approval-row");
    await expect(rows).toHaveCount(2);
    // Risk-adding and risk-reducing differ by label, not only colour; each has a live countdown
    await expect(rows.nth(0)).toContainText("ADDS RISK · Alt-momentum · L1");
    await expect(rows.nth(0)).toContainText("At timeout: no (nothing happens)");
    await expect(rows.nth(0).getByRole("timer")).toHaveText(/⏱ 09:5\d/);
    await expect(rows.nth(1)).toContainText("REDUCES RISK");
    await expect(rows.nth(1)).toContainText("At timeout: proceeds");
    // under 60 s: announced to screen readers
    await expect(page.getByTestId("approval-announcer")).toContainText("reduce-risk approval");

    // R6.6: every decision has a confirm step; Esc backs out without deciding
    await page.getByRole("button", { name: "Approve: BUY ETHUSDT 6% of mandate ($600)" }).click();
    await expect(page.getByRole("button", { name: "Confirm approve: BUY ETHUSDT 6% of mandate ($600)" })).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("button", { name: "Confirm approve: BUY ETHUSDT 6% of mandate ($600)" })).toHaveCount(0);
    expect(calls.decisions).toEqual([]);
    await page.getByRole("button", { name: "Approve: BUY ETHUSDT 6% of mandate ($600)" }).focus();
    await page.keyboard.press("Enter");
    await expect(page.getByRole("button", { name: "Confirm approve: BUY ETHUSDT 6% of mandate ($600)" })).toBeFocused();
    await page.keyboard.press("Enter"); // the confirm button has focus
    await expect(rows.nth(0).getByRole("status")).toContainText("Approved · command queued · cmd-9f8e");
    await page.getByRole("button", { name: "Veto: Close BTCUSDT (exit debate)" }).click();
    await page.getByRole("button", { name: "Confirm veto: Close BTCUSDT (exit debate)" }).click();
    await expect(page.getByTestId("approval-row").filter({ hasText: "Close BTCUSDT" }).getByRole("status")).toHaveText("Vetoed");

    expect(calls.decisions).toEqual([
      { id: "aaaaaaaa-0000-4000-8000-000000000001", decision: "APPROVE", csrf: "csrf-test" },
      { id: "bbbbbbbb-0000-4000-8000-000000000002", decision: "VETO", csrf: "csrf-test" },
    ]);
    expect(reactErrors).toEqual([]);
  });

  test("approval shows quote, slippage, mandate room, worst-case loss and past decisions (R6.4)", async ({ page }) => {
    const a = {
      ...approval("cccccccc-0000-4000-8000-000000000003", "ADD_RISK", "BUY ETHUSDT 6% of mandate ($600)", 500),
      payload: { instrument_id: "ETHUSDT.BINANCE", symbol: "ETHUSDT", side: "BUY", quantity: 0.2, price: 3000, allocation_pct: 6, stop_loss_pct: 5 },
      similar_decisions: [
        { created_at: "2026-09-28T10:00:00Z", recommendation: "BUY", return_pct: 0.012 },
        { created_at: "2026-09-20T10:00:00Z", recommendation: "BUY", return_pct: -0.008 },
      ],
    };
    await page.route("**/api/v1/market/prices*", (route) => route.fulfill({ json: [{ symbol: "ETHUSDT", last_price: 3006 }] }));
    await mockBackend(page, { approvals: [a] });
    await openMissionControl(page);

    const ctx = page.getByTestId("approval-row").first().getByTestId("approval-context");
    await expect(ctx.getByTestId("approval-quote")).toContainText("$3,006");
    await expect(ctx.getByTestId("approval-slippage")).toHaveText("+20.0 bps against");
    await expect(ctx.getByTestId("approval-room")).toHaveText("$8,800"); // 10,000 budget − 1,200 exposure
    await expect(ctx.getByTestId("approval-worst-loss")).toHaveText("-$30"); // 0.2 × 3000 × 5%
    await expect(ctx.getByTestId("approval-resize-note")).toContainText("May be resized");
    await expect(ctx.getByTestId("approval-similar")).toContainText("+1.20%");
    await expect(ctx.getByTestId("approval-similar")).toContainText("-0.80%");
  });

  test("approval context shows unknowns when the quote and history are unavailable", async ({ page }) => {
    await page.route("**/api/v1/market/prices*", (route) => route.fulfill({ status: 500, json: {} }));
    await mockBackend(page, {
      approvals: [{ ...approval("dddddddd-0000-4000-8000-000000000004", "ADD_RISK", "BUY BTCUSDT", 500), payload: { symbol: "BTCUSDT", side: "BUY", quantity: 0.01, price: 60000 } }],
    });
    await openMissionControl(page);
    const ctx = page.getByTestId("approval-context");
    await expect(ctx.getByTestId("approval-quote")).toHaveText("unavailable");
    await expect(ctx.getByTestId("approval-slippage")).toHaveText("—");
    await expect(ctx.getByTestId("approval-worst-loss")).toContainText("no stop in proposal");
    await expect(ctx.getByTestId("approval-similar")).toHaveCount(0);
  });

  test("halted mandate: banner, reason, and a resume that needs TOTP", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    const halted = mandate({
      status: "HALTED",
      halted_reason: "HARD_STOP: drawdown 20.10% from HWM 10600.00",
      drawdown_pct: "20.10",
      level_name: "L1 Copilot",
    });
    const calls = await mockBackend(page, {
      mandates: [halted],
      status: { ...OK_STATUS, halted_mandates: [{ id: M1, name: "Alt-momentum", reason: "HARD_STOP: drawdown 20.10% from HWM 10600.00", halted_at: "2026-09-30T08:00:00Z" }] },
    });
    await openMissionControl(page);

    const banner = page.getByTestId("mission-halted-banner");
    await expect(banner).toBeVisible();
    await expect(banner).toContainText("HALTED · 1 mandate take no new risk");
    await expect(banner).toContainText("Alt-momentum");
    const card = page.getByTestId("mandate-card");
    await expect(card.getByTestId("mandate-status")).toHaveText("HALTED");
    await expect(card.getByTestId("mandate-halted-reason")).toContainText("HARD_STOP");
    await expect(card.getByRole("button", { name: /Pause/ })).toHaveCount(0);

    await card.getByRole("button", { name: "Resume… 🔐" }).click();
    const form = card.getByRole("form", { name: "Resume Alt-momentum" });
    await expect(form.getByRole("button", { name: "Resume" })).toBeDisabled();
    await form.getByLabel("TOTP").fill("123456");
    await form.getByRole("button", { name: "Resume" }).click();
    await expect.poll(() => calls.resumes.length).toBe(1);
    expect(calls.resumes[0]).toEqual({ totp_code: "123456" });
    expect(reactErrors).toEqual([]);
  });

  test("degraded banner names what is degraded", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    await mockBackend(page, {
      status: {
        ...OK_STATUS,
        kill_switch: { latched: true, reason: "ACCOUNT_BACKSTOP_DRAWDOWN", latched_by: "risk:account_backstop" },
        exit_monitor_up: false,
        notifier_up: false,
        degraded: [
          "PAPER kill switch latched: ACCOUNT_BACKSTOP_DRAWDOWN",
          "Exit monitor down: AI positions have no deterministic stops and new AI orders are refused",
          "Notifier down: no Telegram approvals or Critical alerts",
        ],
      },
    });
    await page.route("**/agent-api/brain/spend", (route) => route.fulfill({ status: 503, json: { detail: "offline" } }));
    await openMissionControl(page);
    const banner = page.getByTestId("mission-degraded-banner");
    await expect(banner).toBeVisible();
    await expect(banner).toHaveAttribute("role", "alert");
    await expect(banner).toContainText("DEGRADED");
    await expect(banner).toContainText("PAPER kill switch latched");
    await expect(banner).toContainText("Exit monitor down");
    await expect(banner).toContainText("Notifier down");

    // R6.3 health tiles: reported heartbeats are up/down, unreported ones are unknown, never "up"
    await expect(page.getByTestId("health-exit-monitor")).toHaveAttribute("data-health", "down");
    await expect(page.getByTestId("health-notifier")).toHaveAttribute("data-health", "down");
    await expect(page.getByTestId("health-learner")).toHaveAttribute("data-health", "unknown");
    await expect(page.getByTestId("health-learner")).toContainText("not reported");
    await expect(page.getByTestId("health-sentiment")).toHaveAttribute("data-health", "unknown");
    await expect(page.getByTestId("health-local-tier")).toContainText("not probed yet");
    await expect(page.getByTestId("health-ai-spend")).toContainText("unavailable");
    expect(reactErrors).toEqual([]);
  });

  test("health shows AI spend vs budget and fresh heartbeats", async ({ page }) => {
    await page.route("**/agent-api/brain/spend", (route) => route.fulfill({ json: { spent_usd: "3.10", cap_usd: "25.00" } }));
    await mockBackend(page, {
      status: { ...OK_STATUS, learner_up: true, sentiment_last_ok: Math.floor(Date.now() / 1000) - 60, local_tier: { healthy: true, models: ["qwen"], error: null, checked_at: null } },
    });
    await openMissionControl(page);
    await expect(page.getByTestId("health-ai-spend")).toContainText("$3.10 / $25.00");
    await expect(page.getByTestId("health-exit-monitor")).toHaveAttribute("data-health", "up");
    await expect(page.getByTestId("health-learner")).toHaveAttribute("data-health", "up");
    await expect(page.getByTestId("health-sentiment")).toHaveAttribute("data-health", "up");
    await expect(page.getByTestId("health-local-tier")).toHaveAttribute("data-health", "up");
  });

  test("exit monitor tile says which price the stops run on and flags blind positions", async ({ page }) => {
    const coverage = (sources: ("STREAM" | "REST" | null)[], blind: Record<string, number>) => ({
      checked_at: Math.floor(Date.now() / 1000),
      positions: sources.map((source, i) => ({
        instrument_id: ["BTCUSDT.BINANCE", "XRPUSDT.BINANCE"][i], source, blind_since: source === null ? 1 : null,
      })),
      blind_s_24h: blind,
    });
    await mockBackend(page, { status: { ...OK_STATUS, stop_coverage: coverage(["STREAM", "REST"], { "XRPUSDT.BINANCE": 150 }) } });
    await openMissionControl(page);
    const tile = page.getByTestId("health-exit-monitor");
    await expect(tile).toHaveAttribute("data-health", "up");
    await expect(tile).toContainText("backup price: XRPUSDT");
    await expect(tile).toContainText("blind 3m / 24h");

    await mockBackend(page, { status: { ...OK_STATUS, stop_coverage: coverage(["STREAM", null], {}) } });
    await openMissionControl(page);
    await expect(tile).toHaveAttribute("data-health", "down");
    await expect(tile).toContainText("stops blind: XRPUSDT");
  });

  test("mandate editor asks for TOTP only when a change adds risk", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    const calls = await mockBackend(page);
    await openMissionControl(page);
    await page.getByRole("tab", { name: "Mandate editor" }).click();

    const editor = page.getByRole("region", { name: "Mandate editor" });
    // An existing mandate: tightening needs no TOTP, loosening does
    await editor.getByLabel("Mandate", { exact: true }).selectOption(M1);
    await expect(editor.getByTestId("mandate-totp")).toHaveCount(0);
    await editor.getByLabel("Budget (USD)").fill("8000");
    await expect(editor.getByTestId("mandate-totp")).toHaveCount(0);
    await editor.getByLabel("Budget (USD)").fill("12000");
    await expect(editor.getByTestId("mandate-totp")).toContainText("a larger budget");
    // Perps are an L1 Copilot feature (ADR 0009 D3): open at L1; turning them on from 1x starts at the 3x default and
    // needs TOTP
    await editor.getByLabel("Budget (USD)").fill("10000");
    const perps = editor.getByRole("checkbox", { name: "allow perps" });
    await expect(perps).toBeEnabled();
    await perps.check();
    await expect(editor.getByLabel("Max leverage")).toHaveValue("3");
    await expect(editor.getByTestId("mandate-totp")).toContainText("perps");
    await perps.uncheck();

    // A new mandate always needs TOTP and starts at L1
    await editor.getByLabel("Mandate", { exact: true }).selectOption("");
    await editor.getByLabel("Name").fill("Majors");
    await editor.getByLabel("Instruments (comma separated; a perp is SYMBOL-PERP and needs perps allowed)").fill("btcusdt, ETHUSDT");
    await expect(editor.getByRole("button", { name: "Create mandate (starts at L1)" })).toBeDisabled();
    await editor.getByLabel("TOTP code").fill("654321");
    await editor.getByRole("button", { name: "Create mandate (starts at L1)" }).click();
    await expect.poll(() => calls.creates.length).toBe(1);
    expect(calls.creates[0].csrf).toBe("csrf-test");
    expect(calls.creates[0].body).toMatchObject({ name: "Majors", instruments: ["BTCUSDT.BINANCE", "ETHUSDT.BINANCE"], totp_code: "654321" });
    // A new mandate may start with perps (it starts at L1, ADR 0009 D3); left unticked it is spot only
    expect(calls.creates[0].body).toMatchObject({ allow_perps: false, max_leverage: 1, max_risk_per_trade_pct: "1" });
    expect(reactErrors).toEqual([]);
  });

  test("autonomy: gate checklist and a promotion confirmed with TOTP", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    const calls = await mockBackend(page);
    await openMissionControl(page);
    await page.getByRole("tab", { name: "Autonomy & Evals" }).click();

    await expect(page.getByTestId("autonomy-earned")).toHaveText("L1 Copilot");
    await expect(page.getByTestId("autonomy-requested")).toHaveText("L2");
    await expect(page.getByTestId("gate-check")).toHaveCount(5);
    await expect(page.getByRole("table", { name: "Promotion gate checklist" })).toContainText("1.80 vs 0.90");
    // R4.4: informational checks are marked, show their interval, and do not fail the gate
    const info = page.getByTestId("gate-check-info");
    await expect(info).toHaveCount(2);
    await expect(info.first()).toContainText("informational");
    await expect(info.first()).toContainText("90% CI [-0.05%, 0.29%]");
    await expect(info.first()).toContainText("below (not blocking)");
    await expect(page.getByText("PASSED", { exact: true })).toBeVisible();
    await expect(page.getByTestId("calibration-note")).toContainText("A7.2");

    const confirm = page.getByRole("form", { name: "Confirm promotion" });
    await expect(confirm.getByRole("button", { name: "Confirm promotion" })).toBeDisabled();
    await confirm.getByLabel("TOTP code").fill("112233");
    await confirm.getByRole("button", { name: "Confirm promotion" }).click();
    await expect(page.getByTestId("autonomy-earned")).toHaveText("L2 Supervised");
    expect(calls.confirms).toEqual([{ proposal_id: 77, totp_code: "112233" }]);
    expect(reactErrors).toEqual([]);
  });
  test("autonomy: calibration reliability chart with a table view", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    await mockBackend(page);
    const bins = [
      { lo: 0, hi: 0.2, n: 0, mean_confidence: null, hit_rate: null },
      { lo: 0.2, hi: 0.4, n: 6, mean_confidence: 0.31, hit_rate: 0.33 },
      { lo: 0.4, hi: 0.6, n: 9, mean_confidence: 0.52, hit_rate: 0.44 },
      { lo: 0.6, hi: 0.8, n: 7, mean_confidence: 0.7, hit_rate: 0.57 },
      { lo: 0.8, hi: 1, n: 2, mean_confidence: 0.85, hit_rate: 0.5 },
    ];
    await page.route("**/api/v1/ai/evals", (route) =>
      route.fulfill({
        json: {
          runs: [],
          calibration: { "debate.verdict": { n: 24, min_samples: 20, ece_pp: 8.4, bins } },
          calibration_note: "Verdict conviction vs 24h outcome over 24 labelled decisions: expected calibration error 8.4 pp.",
        },
      }),
    );
    await openMissionControl(page);
    await page.getByRole("tab", { name: "Autonomy & Evals" }).click();

    const chart = page.getByTestId("reliability-debate.verdict");
    await expect(chart).toContainText("24 labelled");
    await expect(chart).toContainText("ECE 8.4 pp");
    await expect(chart.getByRole("img")).toHaveAttribute("aria-label", /52% stated, 44% right, n 9/);
    const table = page.getByRole("table", { name: "Reliability table for debate.verdict" });
    await expect(table.getByRole("row")).toHaveCount(6); // header + 5 bins, empty bins included
    await expect(table).toContainText("60–80%");
    expect(reactErrors).toEqual([]);
  });

  // --- ADR 0010: entry filters and the daily session ---------------------------------------------------------------

  function session(over: Record<string, unknown> = {}) {
    return {
      session_date: "2026-10-21",
      resets_at: "2026-10-22T00:00:00+00:00",
      verdict: "ACTIVE",
      start_equity: "10000",
      pnl: "120",
      peak_pnl: "150",
      loss_latched_at: null,
      target_hit_at: null,
      lock_fired_at: null,
      updated_at: "2026-10-21T10:00:00+00:00",
      daily_loss_limit_pct: "3",
      daily_profit_target_pct: "2",
      profit_lock_pct: "50",
      loss_limit_usd: "300",
      target_usd: "200",
      lock_usd: "100",
      ...over,
    };
  }

  test("mandate card: session meter with the verdict, and a banner when entries are refused", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    const active = mandate({ daily_loss_limit_pct: "3", daily_profit_target_pct: "2", profit_lock_pct: "50", session: session() });
    const latched = mandate({
      id: M2,
      name: "Perp-BTC",
      daily_loss_limit_pct: "3",
      session: session({ verdict: "LOSS_LATCHED", pnl: "-310", loss_latched_at: "2026-10-21T09:00:00+00:00", target_usd: null, lock_usd: null, daily_profit_target_pct: null }),
    });
    const plain = mandate({ id: "33333333-3333-4333-8333-333333333333", name: "No-limits", session: session({ loss_limit_usd: null, target_usd: null, lock_usd: null }) });
    await mockBackend(page, { mandates: [active, latched, plain] });
    await openMissionControl(page);

    const first = page.getByRole("article", { name: "Mandate Alt-momentum" });
    const meter = first.getByRole("meter", { name: "Session P&L — Alt-momentum" });
    await expect(meter).toHaveAttribute("aria-valuetext", "+$120 today · loss limit −$300 · target +$200 · lock +$100");
    await expect(meter).toHaveAttribute("aria-valuemin", "-300");
    await expect(meter).toHaveAttribute("aria-valuemax", "200");
    await expect(first.getByTestId("mandate-session-verdict")).toHaveText("Session active");
    await expect(first.getByTestId("mandate-session")).toContainText("resets 00:00 UTC");

    const second = page.getByRole("article", { name: "Mandate Perp-BTC" });
    await expect(second.getByTestId("mandate-session-verdict")).toHaveText("Daily loss limit · flattened");
    await expect(second.getByRole("meter", { name: "Session P&L — Perp-BTC" })).toHaveAttribute("aria-valuenow", "-300");
    // a mandate with no daily limit shows no meter
    await expect(page.getByRole("article", { name: "Mandate No-limits" }).getByTestId("mandate-session")).toHaveCount(0);

    const banners = page.getByTestId("mission-session-banner");
    await expect(banners).toHaveCount(1);
    await expect(banners).toContainText("DAILY LOSS LIMIT · Perp-BTC: daily loss limit reached");
    await expect(banners).toContainText("new entries resume 00:00 UTC");
    expect(reactErrors).toEqual([]);
  });

  test("a target-met session says positions are kept", async ({ page }) => {
    await mockBackend(page, {
      mandates: [mandate({ daily_profit_target_pct: "2", session: session({ verdict: "TARGET_MET", pnl: "210", target_hit_at: "2026-10-21T11:00:00+00:00" }) })],
    });
    await openMissionControl(page);
    await expect(page.getByTestId("mandate-session-verdict")).toHaveText("Target met · no new entries");
    await expect(page.getByTestId("mission-session-banner")).toContainText("DAILY TARGET MET · Alt-momentum: daily profit target reached: positions are kept");
  });

  test("mandate editor: entry filters and session limits, loosening needs TOTP", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    const current = mandate({ min_conviction: "0.7", min_rr: "2", daily_loss_limit_pct: "3", daily_profit_target_pct: null, profit_lock_pct: "50", session: session() });
    await mockBackend(page, { mandates: [current] });
    const edits: any[] = [];
    await page.route(`**/api/v1/ai/mandates/${M1}`, (route) => {
      if (route.request().method() !== "PATCH") return route.fallback();
      edits.push(route.request().postDataJSON());
      return route.fulfill({ json: current });
    });
    await openMissionControl(page);
    await page.getByRole("tab", { name: "Mandate editor" }).click();
    const editor = page.getByRole("region", { name: "Mandate editor" });
    await editor.getByLabel("Mandate", { exact: true }).selectOption(M1);
    const limits = editor.getByTestId("mandate-session-limits");
    await expect(limits.getByLabel("Min conviction")).toHaveValue("0.7");
    await expect(limits.getByLabel("Min R:R")).toHaveValue("2");
    await expect(limits.getByLabel("Daily loss limit")).toHaveValue("3");
    await expect(limits.getByLabel("Daily profit target")).toHaveValue("");
    await expect(limits.getByLabel("Profit lock")).toHaveValue("50");
    await expect(limits).toContainText("measured from the equity at 00:00 UTC");

    // Tightening (and setting a new target) needs no TOTP
    await limits.getByLabel("Min conviction").fill("0.8");
    await limits.getByLabel("Min R:R").fill("2.5");
    await limits.getByLabel("Daily loss limit").fill("2");
    await limits.getByLabel("Daily profit target").fill("4");
    await limits.getByLabel("Profit lock").fill("60");
    await expect(editor.getByTestId("mandate-totp")).toHaveCount(0);
    await editor.getByRole("button", { name: "Save mandate" }).click();
    await expect.poll(() => edits.length).toBe(1);
    expect(edits[0]).toMatchObject({ min_conviction: "0.8", min_rr: "2.5", daily_loss_limit_pct: "2", daily_profit_target_pct: "4", profit_lock_pct: "60" });
    expect(edits[0].totp_code).toBeUndefined();
    expect(edits[0].clear_limits).toBeUndefined();

    // Loosening: each one names itself in the TOTP box
    await editor.getByLabel("Mandate", { exact: true }).selectOption("");
    await editor.getByLabel("Mandate", { exact: true }).selectOption(M1);
    await limits.getByLabel("Min conviction").fill("0.6");
    await expect(editor.getByTestId("mandate-totp")).toContainText("a lower minimum conviction");
    await limits.getByLabel("Min conviction").fill("0.7");
    await limits.getByLabel("Profit lock").fill("40");
    await expect(editor.getByTestId("mandate-totp")).toContainText("a lower profit lock");
    await limits.getByLabel("Profit lock").fill("50");
    await expect(editor.getByTestId("mandate-totp")).toHaveCount(0);
    // Emptying a set limit switches it off: risk adding, sent as clear_limits with the code
    await limits.getByLabel("Min R:R").fill("");
    await limits.getByLabel("Daily loss limit").fill("");
    await expect(editor.getByTestId("mandate-totp")).toContainText("a lower minimum R:R, a looser daily loss limit");
    await expect(editor.getByRole("button", { name: "Save mandate" })).toBeDisabled();
    await editor.getByLabel("TOTP code").fill("246810");
    await editor.getByRole("button", { name: "Save mandate" }).click();
    await expect.poll(() => edits.length).toBe(2);
    expect(edits[1]).toMatchObject({ clear_limits: ["min_rr", "daily_loss_limit_pct"], totp_code: "246810", min_conviction: "0.7" });
    expect(edits[1].min_rr).toBeUndefined();
    expect(reactErrors).toEqual([]);
  });
});
