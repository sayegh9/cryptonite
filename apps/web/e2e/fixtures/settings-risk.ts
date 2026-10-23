import type { Page } from "@playwright/test";

type Entry = Record<string, unknown>;

const base = (over: Entry): Entry => ({
  default: "0",
  minimum: null,
  maximum: null,
  decimals: 0,
  apply: "hot",
  tier: "T/L",
  writable: true,
  adr: "0006",
  store: "system_settings",
  note: "",
  value: null,
  value_source: "default",
  effective: null,
  tighter: "lower",
  ...over,
});

/** Realistic Risk & Guardrails registry entries (labels and notes as the API sends them) */
export const RISK_ENTRIES: Entry[] = [
  base({ key: "risk.max_daily_loss_pct", label: "Max daily loss (% of equity)", default: "3", minimum: "0.5", maximum: "10", decimals: 1, value: "2.5", value_source: "api", effective: "2.5", note: "Account-level daily loss ceiling; trading halts for the UTC day once the realised plus open loss reaches it." }),
  base({ key: "risk.max_order_equity_pct", label: "Max order size (% of equity)", default: "10", minimum: "1", maximum: "25", decimals: 1, note: "Largest single order as a share of account equity." }),
  base({ key: "risk.cross_mandate_exposure_pct", label: "Cross-mandate exposure cap (% of equity)", default: "40", minimum: "5", maximum: "100", decimals: 0, value: "40", value_source: "api", note: "Combined notional across every active mandate on one symbol." }),
  base({ key: "guard.position_clamp_pct", label: "Position clamp (% of equity)", default: "25", writable: false, tier: "Fixed", adr: "0004", store: "risk_limits.py", value: "25", value_source: "code", note: "No position may exceed this share of equity." }),
  base({ key: "guard.account_backstop_pct", label: "Account backstop (drawdown %)", default: "20", writable: false, tier: "Fixed", adr: "0004", store: "risk_limits.py", value: "20", value_source: "code", note: "Drawdown that latches the kill switch." }),
  base({ key: "guard.min_order_pct", label: "Minimum order (% of equity)", default: "0.1", writable: false, tier: "Fixed", adr: "0004", store: "risk_limits.py", value: "0.1", value_source: "code" }),
  base({ key: "leverage.grade_ceiling_a", label: "Leverage ceiling, grade A setups (x)", default: "5", minimum: "1", maximum: "10", adr: "0009", value: "5", value_source: "api", note: "Highest leverage a grade A setup may propose; lowering is free, raising needs TOTP." }),
  base({ key: "leverage.grade_ceiling_b", label: "Leverage ceiling, grade B setups (x)", default: "3", minimum: "1", maximum: "10", adr: "0009", note: "Highest leverage a grade B setup may propose." }),
  base({ key: "leverage.grade_ceiling_c", label: "Leverage ceiling, grade C setups (x)", default: "2", minimum: "1", maximum: "10", adr: "0009", note: "Highest leverage a grade C setup may propose." }),
  base({ key: "leverage.slippage_allowance_pct", label: "Stop slippage allowance (%)", default: "0.2", minimum: "0", maximum: "1", decimals: 2, adr: "0009", tighter: "higher", note: "Extra distance budgeted between a stop trigger and its fill." }),
  base({ key: "guard.mandate_max_leverage", label: "Mandate max leverage (x)", default: "10", writable: false, tier: "Fixed", adr: "0009", store: "risk_limits.py", value: "10", value_source: "code" }),
  base({ key: "guard.account_max_leverage", label: "Account max leverage (x)", default: "10", writable: false, tier: "Fixed", adr: "0009", store: "risk_limits.py", value: "10", value_source: "code" }),
  base({ key: "guard.perps_level", label: "Perps autonomy level", default: "1", writable: false, tier: "Fixed", adr: "0009", store: "autonomy.py", value: "1", value_source: "code", note: "Legacy fixed display value 1 does not control current routing. Perps follow each mandate's Ask me (L1), Veto window (L2), or Fully automatic (L3) mode; switch OFF forces Ask me. ISOLATED only; leverage follows mandate and account ceilings." }),
  base({ key: "guard.max_risk_per_trade_pct", label: "Max risk per trade (% of budget)", default: "1", writable: false, tier: "Fixed", adr: "0009", store: "risk_limits.py", value: "1", value_source: "code" }),
  base({ key: "guard.liquidation_buffer_pct", label: "Liquidation buffer (%)", default: "30", writable: false, tier: "Fixed", adr: "0009", store: "risk_limits.py", value: "30", value_source: "code" }),
  base({ key: "guard.perp_notional_pct", label: "Perp notional cap (% of equity)", default: "25", writable: false, tier: "Fixed", adr: "0009", store: "risk_limits.py", value: "25", value_source: "code" }),
];

/** Stub an authenticated operator and the settings endpoints, then open Settings -> Risk & Guardrails */
export async function openRiskSettings(page: Page, entries: Entry[] = RISK_ENTRIES) {
  await page.addInitScript(() => localStorage.setItem("cryptonite:ai-brain-tour-seen", "true"));
  const json = (body: unknown) => ({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  await page.route("**/api/v1/auth/me", (route) =>
    route.fulfill(json({ authenticated: true, username: "lead_operator", role: "lead_operator", csrf_token: "test-csrf-token" }))
  );
  await page.route("**/api/v1/settings/registry", (route) => route.fulfill(json({ settings: entries })));
  await page.route("**/api/v1/settings/environment", (route) =>
    route.fulfill(json({
      gemini_key_set: true, binance_key_set: true, environment_mode: "paper", kill_switch_active: false,
      dev_totp_login: false, dev_totp_approvals: false, secure_cookie: true, shadow_allow_paid: false, warnings: [],
    }))
  );
  await page.route("**/api/v1/settings/routes", (route) => route.fulfill(json({ routes: [] })));
  await page.route("**/api/v1/settings/agents", (route) => route.fulfill(json({})));
  await page.route("**/api/v1/settings/audit", (route) => route.fulfill(json({ events: [] })));
  await page.route("**/api/v1/settings/shadow-paid", (route) => route.fulfill(json({ allow_paid: false })));
  await page.goto("/?ws=settings");
  const nav = page.locator("aside").getByRole("button", { name: /Risk & Guardrails/i });
  if (await nav.isVisible()) await nav.click();
  else await page.getByRole("tab", { name: /Risk/i }).click();
  await page.getByTestId("setting-card-risk.max_daily_loss_pct").waitFor();
}
