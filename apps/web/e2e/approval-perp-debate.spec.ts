import { test, expect, type Page } from "@playwright/test";

/**
 * Mission Control approval queue for a debate's perp entry (PerpApprovalDetails, ADR 0009 D3): the perp card fields,
 * the TOTP that approving a perp entry needs, and the spot and veto paths that need none. Same mocked /api/v1/ai
 * backend as mission-control.spec.ts.
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

function approval(
  id: string,
  action: "ADD_RISK" | "REDUCE_RISK",
  summary: string,
  secondsLeft: number,
  over: Record<string, unknown> = {},
) {
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
    ...over,
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

const LONG_SUMMARY = "LONG BTCUSDT-PERP.BINANCE MARKET ISOLATED, dry run 3x (mandate maximum), margin 2.5% of mandate ($250) · grade B";
const SHORT_SUMMARY = "SHORT ETHUSDT-PERP.BINANCE MARKET ISOLATED, dry run 2x (risk cap), margin 2.0% of mandate ($200) · grade A";
const SPOT_SUMMARY = "BUY BTCUSDT 0.01 @ $60,000";
const PERP_ID = "eeeeeeee-0000-4000-8000-000000000005";
const SHORT_ID = "ffffffff-0000-4000-8000-000000000006";
const SPOT_ID = "dddddddd-0000-4000-8000-000000000007";
const VETO_ID = "bbbbbbbb-0000-4000-8000-000000000008";

const PERP_PAYLOAD = {
  instrument_id: "BTCUSDT-PERP.BINANCE",
  side: "BUY",
  perp_side: "LONG",
  stop_loss: 57000,
  take_profit: 66000,
  setup_grade: "B",
  grade: { grade: "B", reasons: ["4h, 1h and 15m aligned", "reward 2.0x the risk", "liquidity unknown"] },
  perp_plan: {
    side: "BUY",
    ok: true,
    leverage: 3,
    binding_ceiling: "mandate maximum",
    liquidation_price: "40312.5",
    loss_at_stop_usd: "41.25",
    loss_at_stop_pct: "0.4125",
  },
};

test.describe("Mission Control: debate perp approvals", () => {
  test("a debate perp entry shows the perp card fields", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    await mockBackend(page, {
      approvals: [approval(PERP_ID, "ADD_RISK", LONG_SUMMARY, 595, { needs_totp: true, payload: PERP_PAYLOAD })],
    });
    await openMissionControl(page);

    const row = page.getByTestId("approval-row").first();
    await expect(row).toContainText(LONG_SUMMARY);
    await expect(row.getByTestId("perp-side")).toHaveText("LONG · ISOLATED");
    await expect(row.getByTestId("perp-grade")).toContainText("B");
    await expect(row.getByTestId("perp-grade")).toContainText("liquidity unknown");
    await expect(row.getByTestId("perp-leverage")).toContainText("3x");
    await expect(row.getByTestId("perp-leverage")).toContainText("mandate maximum");
    await expect(row.getByTestId("perp-liquidation")).toContainText("40,312.5");
    await expect(row.getByTestId("perp-loss")).toContainText("$41.25");
    await expect(row.getByTestId("perp-loss")).toContainText("0.41% of budget");
    expect(reactErrors).toEqual([]);
  });

  test("approving a perp entry asks for TOTP and sends it", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    const calls = await mockBackend(page, {
      approvals: [approval(PERP_ID, "ADD_RISK", LONG_SUMMARY, 595, { needs_totp: true, payload: PERP_PAYLOAD })],
    });
    await openMissionControl(page);

    const row = page.getByTestId("approval-row").first();
    await row.getByRole("button", { name: `Approve: ${LONG_SUMMARY}` }).click();

    const totp = page.getByTestId("approval-totp");
    await expect(totp).toBeVisible();
    await expect(totp).toHaveAttribute("aria-label", /^TOTP code to approve:/);
    const confirm = page.getByRole("button", { name: `Confirm approve: ${LONG_SUMMARY}` });
    await expect(confirm).toBeDisabled();
    await totp.fill("12345");
    await expect(confirm).toBeDisabled(); // six digits are required
    await totp.fill("123456");
    await expect(confirm).toBeEnabled();
    await confirm.click();

    await expect(row.getByRole("status")).toContainText("Approved · command queued");
    expect(calls.decisions).toEqual([{ id: PERP_ID, decision: "APPROVE", totp_code: "123456", csrf: "csrf-test" }]);
    expect(reactErrors).toEqual([]);
  });

  test("a SHORT shows SHORT", async ({ page }) => {
    await mockBackend(page, {
      approvals: [
        approval(SHORT_ID, "ADD_RISK", SHORT_SUMMARY, 595, {
          needs_totp: true,
          payload: {
            ...PERP_PAYLOAD,
            instrument_id: "ETHUSDT-PERP.BINANCE",
            side: "SELL",
            perp_side: "SHORT",
            setup_grade: "A",
            grade: { grade: "A", reasons: ["4h, 1h and 15m aligned"] },
            perp_plan: { ...PERP_PAYLOAD.perp_plan, side: "SELL", leverage: 2, binding_ceiling: "risk cap" },
          },
        }),
      ],
    });
    await openMissionControl(page);

    const row = page.getByTestId("approval-row").first();
    await expect(row.getByTestId("perp-side")).toHaveText("SHORT · ISOLATED");
    await expect(row.getByTestId("perp-leverage")).toContainText("2x");
    await expect(row.getByTestId("perp-leverage")).toContainText("risk cap");
  });

  test("a spot approval has no perp block and no TOTP", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    const calls = await mockBackend(page, {
      approvals: [
        approval(SPOT_ID, "ADD_RISK", SPOT_SUMMARY, 595, {
          payload: { symbol: "BTCUSDT", side: "BUY", quantity: 0.01, price: 60000 },
        }),
      ],
    });
    await openMissionControl(page);

    const row = page.getByTestId("approval-row").first();
    await expect(page.getByTestId("perp-approval")).toHaveCount(0);

    await row.getByRole("button", { name: `Approve: ${SPOT_SUMMARY}` }).click();
    await expect(page.getByTestId("approval-totp")).toHaveCount(0);
    const confirm = page.getByRole("button", { name: `Confirm approve: ${SPOT_SUMMARY}` });
    await expect(confirm).toBeEnabled();
    await confirm.click();

    await expect(row.getByRole("status")).toContainText("Approved · command queued");
    expect(calls.decisions).toEqual([{ id: SPOT_ID, decision: "APPROVE", csrf: "csrf-test" }]);
    expect(reactErrors).toEqual([]);
  });

  test("veto of a perp entry needs no TOTP", async ({ page }) => {
    const reactErrors = collectReactErrors(page);
    const calls = await mockBackend(page, {
      approvals: [approval(VETO_ID, "ADD_RISK", LONG_SUMMARY, 595, { needs_totp: true, payload: PERP_PAYLOAD })],
    });
    await openMissionControl(page);

    const row = page.getByTestId("approval-row").first();
    await row.getByRole("button", { name: `Veto: ${LONG_SUMMARY}` }).click();
    await expect(page.getByTestId("approval-totp")).toHaveCount(0);
    await page.getByRole("button", { name: `Confirm veto: ${LONG_SUMMARY}` }).click();

    await expect(row.getByRole("status")).toHaveText("Vetoed");
    expect(calls.decisions).toEqual([{ id: VETO_ID, decision: "VETO", csrf: "csrf-test" }]);
    expect(reactErrors).toEqual([]);
  });
});
