import { test, expect } from "@playwright/test";

test.describe("Cryptonite AI Brain Workspace E2E", () => {
  test.beforeEach(async ({ page }) => {
    // 1. Mock bars API
    await page.route("**/api/v1/bars*", async (route) => {
      const nowSec = Math.floor(Date.now() / 1000);
      const bars = [];
      for (let i = 30; i >= 0; i--) {
        const t = nowSec - i * 60;
        bars.push({
          time: t,
          open: "54000.00",
          high: "54200.00",
          low: "53950.00",
          close: "54150.00",
          volume: "12.5000",
          bar_open_ns: t * 1_000_000_000,
          bar_close_inclusive_ns: (t + 60) * 1_000_000_000 - 1_000_000,
          interval_end_exclusive_ns: (t + 60) * 1_000_000_000,
        });
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          symbol: "BTCUSDT",
          timeframe: "1m",
          bars,
          count: bars.length,
        }),
      });
    });

    // 2. Mock Agent chat API
    await page.route("**/agent-api/chat", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          response: "### 🧠 Orchestrator Synthesis for **BTCUSDT**\n\n- **Market Structure:** Healthy bid support at $54,000.\n- **Verdict:** Bullish momentum confirmed.\n- **Risk Guard:** Maximum allocation $25,000.",
          model: "gemini-2.5-flash",
        }),
      });
    });

    // 3. Mock Debate approve/reject APIs
    await page.route("**/api/v1/agents/debates/*/approve", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ status: "COMPLETED", message: "Outbox order staged" }),
      });
    });

    await page.route("**/agent-api/debates/trigger", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ status: "QUEUED", run_id: "deb-test-123" }),
      });
    });

    await page.route("**/api/v1/agents/debates/*/reject", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ status: "REJECTED" }),
      });
    });
  });

  test("navigates to AI Brain tab and verifies 3-zone architecture, starter chips, action cards and tour", async ({ page }) => {
    await page.goto("/");

    // 1. Click AI Brain tab in header
    const brainTab = page.locator("button", { hasText: "AI Brain" });
    await expect(brainTab).toBeVisible();
    await brainTab.click();

    // The AI Brain workspace opens on Brain Home; the debate hub is the "Debates" view
    await expect(page.getByTestId("brain-home")).toBeVisible();
    await page.getByRole("group", { name: "AI Brain view" }).getByRole("button", { name: "Debates" }).click();

    // 2. If first-time user tour appears, step through it and complete
    const tourGuideHeader = page.locator("text=First-Time User Guide");
    if (await tourGuideHeader.isVisible({ timeout: 2000 }).catch(() => false)) {
      await expect(page.locator("text=Step 1 of 3")).toBeVisible();
      const nextBtn = page.locator("button", { hasText: "Next" });
      await nextBtn.click();
      await expect(page.locator("text=Step 2 of 3")).toBeVisible();
      await nextBtn.click();
      await expect(page.locator("text=Step 3 of 3")).toBeVisible();
      const completeBtn = page.locator("button", { hasText: "Start Using AI Brain" });
      await completeBtn.click();
      await expect(tourGuideHeader).not.toBeVisible();
    }

    // 3. Verify AI Brain Header & the computed mode banner (G1), not a hardcoded "RISK-FREE" label
    await expect(page.locator("text=AI Brain Hub")).toBeVisible();
    await expect(page.getByTestId("mode-banner").nth(1)).toContainText("PAPER ·");
    await expect(page.getByText("RISK-FREE")).toHaveCount(0);

    // 4. Verify Zone 1: Agent Fleet Topology
    await expect(page.locator("text=Agent Fleet Topology")).toBeVisible();
    await expect(page.locator("text=Lead Orchestrator").first()).toBeVisible();
    await expect(page.locator("text=Market Data Scout")).toBeVisible();
    await expect(page.locator("text=Bull / Bear Debaters")).toBeVisible();
    await expect(page.locator("text=Risk Guardian (CRO)")).toBeVisible();
    await expect(page.locator("text=Execution Outbox Relay")).toBeVisible();

    // 5. Verify Zone 2: Lead Orchestrator Canvas & Starter Chips
    await expect(page.locator("text=Lead Orchestrator Canvas")).toBeVisible();
    await expect(page.locator("text=Welcome to the Cryptonite AI Brain")).toBeVisible();
    await expect(page.locator("text=Run BTC Consensus Debate")).toBeVisible();
    await expect(page.locator("text=Audit Capital & Risk")).toBeVisible();

    // 6. Verify Zone 3: Action Inbox
    await expect(page.locator("text=Action & Decision Inbox")).toBeVisible();
    await expect(page.locator("text=Example card · not a real proposal")).toHaveCount(0); // no sample data in the live inbox

    // 7. Test Starter Prompt Chip execution
    const riskChip = page.locator("button", { hasText: "Audit Capital & Risk" });
    await riskChip.click();
    await expect(page.locator("text=Orchestrator Synthesis for")).toBeVisible({ timeout: 5000 });

    // 8. Triggering a debate only queues it: no card is invented locally
    const triggerBtn = page.locator("button", { hasText: "⚡ Trigger BTCUSDT Debate" });
    await triggerBtn.click();
    await expect(page.getByText("Debate queued: BTCUSDT")).toBeVisible({ timeout: 5000 });
    await expect(page.locator("text=PENDING_APPROVAL")).toHaveCount(0);
  });

  test("approves a server PENDING_APPROVAL debate with the operator's allocation_pct", async ({ page }) => {
    const RUN_ID = "11111111-2222-4333-8444-555555555555";
    let approveBody: Record<string, unknown> | null = null;

    await page.addInitScript(() => {
      localStorage.setItem("csrf_token", "test-csrf");
      localStorage.setItem("cryptonite:ai-brain-tour-seen", "true");
    });
    await page.route("**/api/v1/paper/**", async (route) => {
      const url = route.request().url();
      const body = url.includes("/paper/accounts")
        ? [{ account_id: "PAPER-ACCOUNT-001", venue: "PAPER", currency: "USDT", starting_balance: "10000", cash_balance: "10000", total_equity: "10000", realized_pnl: "0", daily_realized_pnl: "0", daily_loss_latched: false }]
        : url.includes("/paper/kill-switch") ? { id: 1, is_latched: false } : [];
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
    });
    await page.route("**/api/v1/agents/debates*", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          { runId: RUN_ID, instrumentId: "ETHUSDT.BINANCE", status: "PENDING_APPROVAL", decision: "BUY (70% Conviction) • test", timestamp: Date.now(), turns: [] },
        ]),
      });
    });
    await page.route(`**/api/v1/agents/debates/${RUN_ID}/decision`, async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ run_id: RUN_ID, allocation_pct: 5 }) });
    });
    await page.route(`**/api/v1/agents/debates/${RUN_ID}/approve`, async (route) => {
      approveBody = route.request().postDataJSON();
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ status: "approved", run_id: RUN_ID, allocation_pct: 2 }),
      });
    });

    await page.goto("/");
    await page.locator("button", { hasText: "AI Brain" }).click();
    await page.getByRole("group", { name: "AI Brain view" }).getByRole("button", { name: "Debates" }).click();

    await expect(page.locator("text=PENDING_APPROVAL")).toBeVisible({ timeout: 5000 });
    // The slider starts at the AI's proposal and shows USD only from the known equity (10,000)
    await expect(page.getByText("AI proposed 5%")).toBeVisible();
    await expect(page.getByTestId("inbox-size-usd")).toContainText("$500");

    await page.getByLabel(/Allocation: \d+% of equity/).fill("2");
    await expect(page.getByTestId("inbox-size-usd")).toContainText("$200");

    const approveBtn = page.getByTestId("approve-debate-btn");
    await expect(approveBtn).toBeEnabled();
    await approveBtn.click();
    // Confirm step (R6.6): nothing is sent until the operator confirms
    expect(approveBody).toBeNull();
    await expect(page.getByTestId("confirm-debate-btn")).toHaveText("Confirm approve at 2%");
    await page.getByTestId("confirm-debate-btn").click();

    await expect.poll(() => approveBody).not.toBeNull();
    expect(approveBody).toEqual({ allocation_pct: 2 });
    expect(approveBody).not.toHaveProperty("targetAllocationUsd");

    // The card shows the server's status (COMPLETED after a confirmed approval)
    await page.locator("button", { hasText: "Staged" }).click();
    await expect(page.locator("text=COMPLETED").first()).toBeVisible({ timeout: 5000 });
  });

  test("approve without a session shows an error, never a local COMPLETED", async ({ page }) => {
    const RUN_ID = "aaaaaaaa-2222-4333-8444-555555555555";
    let approveCalled = false;
    await page.addInitScript(() => {
      localStorage.removeItem("csrf_token");
      localStorage.setItem("cryptonite:ai-brain-tour-seen", "true");
    });
    await page.route("**/api/v1/auth/me", (route) => route.fulfill({ status: 401, contentType: "application/json", body: "{}" }));
    await page.route("**/api/v1/paper/**", async (route) => {
      const url = route.request().url();
      const body = url.includes("/paper/accounts")
        ? [{ account_id: "PAPER-ACCOUNT-001", total_equity: "10000", cash_balance: "10000", starting_balance: "10000", realized_pnl: "0", daily_realized_pnl: "0" }]
        : url.includes("/paper/kill-switch") ? { id: 1, is_latched: false } : [];
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
    });
    await page.route("**/api/v1/agents/debates*", (route) => route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([{ runId: RUN_ID, instrumentId: "BTCUSDT.BINANCE", status: "PENDING_APPROVAL", decision: "BUY", timestamp: Date.now(), turns: [] }]),
    }));
    await page.route(`**/api/v1/agents/debates/${RUN_ID}/decision`, (route) => route.fulfill({ status: 404, contentType: "application/json", body: "{}" }));
    await page.route(`**/api/v1/agents/debates/${RUN_ID}/approve`, async (route) => {
      approveCalled = true;
      await route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
    });

    await page.goto("/");
    await page.locator("button", { hasText: "AI Brain" }).click();
    await page.getByRole("group", { name: "AI Brain view" }).getByRole("button", { name: "Debates" }).click();
    await expect(page.locator("text=PENDING_APPROVAL")).toBeVisible({ timeout: 5000 });
    await page.getByTestId("approve-debate-btn").click();
    await page.getByTestId("confirm-debate-btn").click();

    await expect(page.getByTestId("inbox-action-error")).toContainText("Session expired, sign in again");
    expect(approveCalled).toBe(false);
    await expect(page.locator("text=PENDING_APPROVAL")).toBeVisible();
  });

  test("agent and portfolio failures show errors, never invented analysis or sizes", async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem("cryptonite:ai-brain-tour-seen", "true"));
    await page.route("**/agent-api/**", (route) => route.fulfill({ status: 500, contentType: "application/json", body: "{}" }));
    await page.route("**/api/v1/paper/**", (route) => route.fulfill({ status: 500, contentType: "application/json", body: "{}" }));
    await page.route("**/api/v1/agents/debates*", (route) => route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([{ runId: "bbbbbbbb-2222-4333-8444-555555555555", instrumentId: "BTCUSDT.BINANCE", status: "PENDING_APPROVAL", decision: "BUY", timestamp: Date.now(), turns: [] }]),
    }));
    await page.route("**/api/v1/agents/debates/*/decision", (route) => route.fulfill({ status: 500, contentType: "application/json", body: "{}" }));

    await page.goto("/");
    await page.locator("button", { hasText: "AI Brain" }).click();
    await page.getByRole("group", { name: "AI Brain view" }).getByRole("button", { name: "Debates" }).click();

    await page.locator("button", { hasText: "Audit Capital & Risk" }).click();
    await expect(page.getByTestId("canvas-agent-error")).toContainText("agent unavailable: 500");
    await expect(page.getByRole("button", { name: "Retry" })).toBeVisible();
    const canvas = page.locator("main");
    await expect(canvas.getByText(/RSI|CRO approved|Setup approved/)).toHaveCount(0);

    // Unknown equity: size shows as unavailable and Approve is disabled
    await expect(page.getByTestId("inbox-size-usd")).toHaveText("equity unavailable");
    await expect(page.getByTestId("approve-debate-btn")).toBeDisabled();
  });
});
