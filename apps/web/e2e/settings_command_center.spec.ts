import { test, expect } from "./fixtures/offline";

test.describe("Settings Command Center & Header", () => {
  test.beforeEach(async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem("cryptonite:ai-brain-tour-seen", "true"));
  });

  test("Desk Preferences work anonymously & persist theme/density to DOM", async ({ page }) => {
    // Stub /api/v1/auth/me to return anonymous state
    await page.route("**/api/v1/auth/me", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ authenticated: false }),
      })
    );

    await page.goto("/?ws=settings");

    // Verify Desk Preferences section is accessible
    await expect(page.getByRole("heading", { name: "Desk Preferences" })).toBeVisible();

    // Verify Theme Mode options (Light, Dark, System)
    const lightBtn = page.getByRole("button", { name: "Light", exact: true });
    const darkBtn = page.getByRole("button", { name: "Dark", exact: true });
    const systemBtn = page.getByRole("button", { name: "System", exact: true });
    await expect(lightBtn).toBeVisible();
    await expect(darkBtn).toBeVisible();
    await expect(systemBtn).toBeVisible();

    // Verify Interface Density options (Compact, Comfortable)
    const compactBtn = page.getByRole("button", { name: /Compact/i });
    const comfortableBtn = page.getByRole("button", { name: /Comfortable/i });
    await expect(compactBtn).toBeVisible();
    await expect(comfortableBtn).toBeVisible();

    // Verify Audio Alerts & Synthesizer Chimes card
    await expect(page.getByText(/Trading Desk Audio & Synthesized Chimes/i)).toBeVisible();

    // Verify Number & P&L Formatting card
    await expect(page.getByRole("heading", { name: /Number & P&L Formatting/i })).toBeVisible();

    // Click Light theme button -> verify document.documentElement has data-theme="light"
    await lightBtn.click();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "light");

    // Click Dark theme button -> verify document.documentElement has data-theme="dark"
    await darkBtn.click();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");

    // Click Comfortable density button -> verify document.documentElement has data-density="comfortable"
    await comfortableBtn.click();
    await expect(page.locator("html")).toHaveAttribute("data-density", "comfortable");
  });

  test("Header Search (⌘K) opens CommandPalette and jumps to Settings sections", async ({ page }) => {
    // Stub auth to authenticated operator so settings sections load fully
    await page.route("**/api/v1/auth/me", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          authenticated: true,
          username: "lead_operator",
          role: "lead_operator",
          csrf_token: "test-csrf-token",
        }),
      })
    );
    await page.route("**/api/v1/settings/registry", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ settings: [] }),
      })
    );
    await page.route("**/api/v1/settings/environment", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          gemini_key_set: true,
          binance_key_set: true,
          environment_mode: "paper",
          kill_switch_active: false,
          dev_totp_login: false,
          dev_totp_approvals: false,
          secure_cookie: true,
          shadow_allow_paid: false,
          warnings: [],
        }),
      })
    );
    await page.route("**/api/v1/settings/routes", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ routes: [] }),
      })
    );
    await page.route("**/api/v1/settings/agents", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({}),
      })
    );
    await page.route("**/api/v1/settings/audit", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ events: [] }),
      })
    );

    await page.goto("/?ws=terminal");

    // Click the header button getByRole("button", { name: "Search and commands" })
    const headerSearchBtn = page.getByRole("button", { name: "Search and commands" });
    await expect(headerSearchBtn).toBeVisible();
    await headerSearchBtn.click();

    // Verify the CommandPalette dialog opens
    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();

    // Select "Settings: Exit Rules & Stop Loss"
    const exitRulesItem = dialog.getByRole("option", { name: /Settings: Exit Rules & Stop Loss/i });
    await expect(exitRulesItem).toBeVisible();
    await exitRulesItem.click();

    // Verify it navigates to /?ws=settings and activates the Exit Rules section
    await expect(page).toHaveURL(/\?ws=settings/);
    await expect(page.getByRole("heading", { name: /Exit Rules & Constraint Monitor/i })).toBeVisible();
  });

  test("Authenticated Operator Settings Hub (8 Categories, Global Search, Exit Constraint Gauge & Tighter.HIGHER TOTP rule)", async ({ page }) => {
    // Stub authenticated operator & settings endpoints
    await page.route("**/api/v1/auth/me", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          authenticated: true,
          username: "lead_operator",
          role: "lead_operator",
          csrf_token: "test-csrf-token",
        }),
      })
    );

    const registrySettings = [
      {
        key: "LLM_DAILY_BUDGET_USD",
        label: "LLM daily budget (USD)",
        default: "25.00",
        minimum: "1.00",
        maximum: null,
        decimals: 2,
        apply: "hot",
        tier: "T/L",
        writable: true,
        adr: "0002",
        store: "system_settings",
        note: "re-read by the budget guard every 10 s",
        value: "25.00",
        value_source: "default",
        effective: "25.00",
        tighter: "lower",
      },
      {
        key: "exit.stop_loss_pct",
        label: "Hard stop (% loss)",
        default: "5",
        minimum: "0.01",
        maximum: "20",
        decimals: 2,
        apply: "hot",
        tier: "T/L",
        writable: true,
        adr: "0002",
        store: "system_settings",
        note: "read by the exit monitor every cycle",
        value: "5",
        value_source: "default",
        effective: "5",
        tighter: "lower",
      },
      {
        key: "exit.debate_drawdown_pct",
        label: "Exit debate trigger (% loss)",
        default: "2",
        minimum: "0.01",
        maximum: "20",
        decimals: 2,
        apply: "hot",
        tier: "T/L",
        writable: true,
        adr: "0002",
        store: "system_settings",
        note: "must stay below exit.stop_loss_pct",
        value: "2",
        value_source: "default",
        effective: "2",
        tighter: "lower",
      },
      {
        key: "exit.trail_activate_pct",
        label: "Trail activates at (% gain)",
        default: "3",
        minimum: "0.01",
        maximum: "20",
        decimals: 2,
        apply: "hot",
        tier: "T/L",
        writable: true,
        adr: "0002",
        store: "system_settings",
        note: "must be at least exit.trail_pct",
        value: "3",
        value_source: "default",
        effective: "3",
        tighter: "lower",
      },
      {
        key: "exit.trail_pct",
        label: "Trail give-back (%)",
        default: "2",
        minimum: "0.01",
        maximum: "20",
        decimals: 2,
        apply: "hot",
        tier: "T/L",
        writable: true,
        adr: "0002",
        store: "system_settings",
        note: "must not exceed exit.trail_activate_pct",
        value: "2",
        value_source: "default",
        effective: "2",
        tighter: "lower",
      },
      {
        key: "auth.lockout_s",
        label: "Failed-login window (s)",
        default: "300",
        minimum: "60",
        maximum: "3600",
        decimals: 0,
        apply: "hot",
        tier: "T/L",
        writable: true,
        adr: "0007",
        store: "system_settings",
        note: "failed logins are counted over this window; a longer window is the tighter one",
        value: "300",
        value_source: "default",
        effective: "300",
        tighter: "higher",
      },
    ];

    await page.route("**/api/v1/settings/registry", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ settings: registrySettings }),
      })
    );

    await page.route("**/api/v1/settings/environment", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          gemini_key_set: true,
          binance_key_set: true,
          environment_mode: "paper",
          kill_switch_active: false,
          dev_totp_login: false,
          dev_totp_approvals: false,
          secure_cookie: true,
          shadow_allow_paid: false,
          warnings: [],
        }),
      })
    );

    await page.route("**/api/v1/settings/routes", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          routes: [
            {
              role: "fast",
              protected: false,
              editable: true,
              local_allowed: true,
              default: {
                primary_model: "gemini-2.5-flash",
                fallback_model: "mock-local",
                fallback_policy: "local_fallback",
                daily_cap_usd: "10.00",
                max_output_tokens: 2048,
                thinking_budget: 0,
              },
              effective: {
                primary_model: "gemini-2.5-flash",
                fallback_model: "mock-local",
                fallback_policy: "local_fallback",
                daily_cap_usd: "10.00",
                max_output_tokens: 2048,
                thinking_budget: 0,
              },
              stored: null,
              overridden: false,
            },
          ],
        }),
      })
    );

    await page.route("**/api/v1/settings/agents", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          bull: {
            model_name: "gemini-2.5-flash",
            temperature: 0.7,
            system_instructions: "Bull agent",
          },
        }),
      })
    );

    await page.route("**/api/v1/settings/audit", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          events: [
            {
              event_id: "audit-1",
              username: "operator",
              action: "setting.update",
              ip_address: "127.0.0.1",
              details: { key: "auth.lockout_s", new_value: "300" },
              occurred_at: "2026-10-17T20:00:00Z",
            },
          ],
        }),
      })
    );

    await page.route("**/api/v1/settings/shadow-paid", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ allow_paid: false }),
      })
    );

    await page.goto("/?ws=settings");

    // Verify all 8 category navigation buttons are present
    const sidebar = page.locator("aside");
    await expect(sidebar.getByRole("button", { name: /Desk Preferences/i })).toBeVisible();
    await expect(sidebar.getByRole("button", { name: /AI Gateway/i })).toBeVisible();
    await expect(sidebar.getByRole("button", { name: /Exit Rules/i })).toBeVisible();
    await expect(sidebar.getByRole("button", { name: /Risk & Guardrails/i })).toBeVisible();
    await expect(sidebar.getByRole("button", { name: /Scanner & Universe/i })).toBeVisible();
    await expect(sidebar.getByRole("button", { name: /Alerts & Integrations/i })).toBeVisible();
    await expect(sidebar.getByRole("button", { name: /Security/i })).toBeVisible();
    await expect(sidebar.getByRole("button", { name: /System Health/i })).toBeVisible();

    // Navigate to Exit Rules & Monitor
    await sidebar.getByRole("button", { name: /Exit Rules/i }).click();
    await expect(page.getByRole("heading", { name: /Exit Rules & Constraint Monitor/i })).toBeVisible();

    // Verify ExitConstraintGauge shows "All exit rules valid"
    await expect(page.getByText("All exit rules valid")).toBeVisible();

    // Enter an invalid exit.trail_pct > exit.trail_activate_pct
    // trail_activate_pct is 3, so setting trail_pct to 5 triggers the violation
    const trailCard = page.getByTestId("setting-card-exit.trail_pct");
    await trailCard.getByRole("button", { name: /^Edit / }).click();
    const trailInput = trailCard.locator('input[type="number"]');
    await trailInput.fill("5");

    // Verify inline cross-field constraint alert is displayed
    await expect(trailCard.getByRole("alert")).toBeVisible();
    await expect(trailCard.getByText(/Cross-field rule violation.*exit\.trail_pct/i)).toBeVisible();

    // Navigate to Security & Sessions
    await sidebar.getByRole("button", { name: /Security/i }).click();
    await expect(page.getByRole("heading", { name: /Security Governance & Session Policy/i })).toBeVisible();

    // Find auth.lockout_s card
    const lockoutCard = page.getByTestId("setting-card-auth.lockout_s");
    await lockoutCard.getByRole("button", { name: /^Edit / }).click();
    const lockoutInput = lockoutCard.locator('input[type="number"]');

    // Initially 300: no TOTP input
    await expect(lockoutCard.getByPlaceholder("6-digit code")).toHaveCount(0);

    // Increasing from 300 to 600: tighter="higher" means increasing is tightening, so does NOT ask for TOTP
    await lockoutInput.fill("600");
    await expect(lockoutCard.getByPlaceholder("6-digit code")).toHaveCount(0);
    await expect(lockoutCard.getByText(/Your TOTP code/i)).toHaveCount(0);

    // Decreasing from 300 (or 600) to 120: decreasing is loosening, so DOES ask for TOTP
    await lockoutInput.fill("120");
    await expect(lockoutCard.getByPlaceholder("6-digit code")).toBeVisible();
    await expect(lockoutCard.getByText(/Your TOTP code/i)).toBeVisible();

    // Use Global Settings Search to search for "stop_loss_pct" and verify matching results appear
    const filterInput = page.getByRole("textbox", { name: "Filter settings" });
    await expect(filterInput).toBeVisible();
    await filterInput.fill("stop_loss_pct");

    await expect(page.getByRole("heading", { name: /Filtered Settings:/i })).toBeVisible();
    await expect(page.getByText("exit.stop_loss_pct").first()).toBeVisible();
  });
});
