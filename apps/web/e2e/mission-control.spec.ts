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

    await page.getByRole("button", { name: "Approve: BUY ETHUSDT 6% of mandate ($600)" }).click();
    await expect(rows.nth(0).getByRole("status")).toContainText("Approved · command queued · cmd-9f8e");
    await page.getByRole("button", { name: "Veto: Close BTCUSDT (exit debate)" }).click();
    await expect(page.getByTestId("approval-row").filter({ hasText: "Close BTCUSDT" }).getByRole("status")).toHaveText("Vetoed");

    expect(calls.decisions).toEqual([
      { id: "aaaaaaaa-0000-4000-8000-000000000001", decision: "APPROVE", csrf: "csrf-test" },
      { id: "bbbbbbbb-0000-4000-8000-000000000002", decision: "VETO", csrf: "csrf-test" },
    ]);
    expect(reactErrors).toEqual([]);
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
    await openMissionControl(page);
    const banner = page.getByTestId("mission-degraded-banner");
    await expect(banner).toBeVisible();
    await expect(banner).toHaveAttribute("role", "alert");
    await expect(banner).toContainText("DEGRADED");
    await expect(banner).toContainText("PAPER kill switch latched");
    await expect(banner).toContainText("Exit monitor down");
    await expect(banner).toContainText("Notifier down");
    expect(reactErrors).toEqual([]);
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
    // Perps stay locked below L3 (O1)
    await expect(editor.getByRole("checkbox", { name: "allow perps" })).toBeDisabled();

    // A new mandate always needs TOTP and starts at L1
    await editor.getByLabel("Mandate", { exact: true }).selectOption("");
    await editor.getByLabel("Name").fill("Majors");
    await editor.getByLabel("Instruments (comma separated; spot until L3)").fill("btcusdt, ETHUSDT");
    await expect(editor.getByRole("button", { name: "Create mandate (starts at L1)" })).toBeDisabled();
    await editor.getByLabel("TOTP code").fill("654321");
    await editor.getByRole("button", { name: "Create mandate (starts at L1)" }).click();
    await expect.poll(() => calls.creates.length).toBe(1);
    expect(calls.creates[0].csrf).toBe("csrf-test");
    expect(calls.creates[0].body).toMatchObject({ name: "Majors", instruments: ["BTCUSDT.BINANCE", "ETHUSDT.BINANCE"], totp_code: "654321" });
    expect(calls.creates[0].body).not.toHaveProperty("allow_perps");
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
    await expect(page.getByTestId("calibration-note")).toContainText("A7.2");

    const confirm = page.getByRole("form", { name: "Confirm promotion" });
    await expect(confirm.getByRole("button", { name: "Confirm promotion" })).toBeDisabled();
    await confirm.getByLabel("TOTP code").fill("112233");
    await confirm.getByRole("button", { name: "Confirm promotion" }).click();
    await expect(page.getByTestId("autonomy-earned")).toHaveText("L2 Supervised");
    expect(calls.confirms).toEqual([{ proposal_id: 77, totp_code: "112233" }]);
    expect(reactErrors).toEqual([]);
  });
});
