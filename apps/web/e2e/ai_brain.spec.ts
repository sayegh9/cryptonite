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

    // 3. Verify AI Brain Header & Simulation Mode banner
    await expect(page.locator("text=AI Brain Hub")).toBeVisible();
    await expect(page.locator("text=SIMULATION MODE (RISK-FREE)")).toBeVisible();

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
    await expect(page.locator("text=Sample Action Card (Interactive Guide)")).toBeVisible();

    // 7. Test Starter Prompt Chip execution
    const riskChip = page.locator("button", { hasText: "Audit Capital & Risk" });
    await riskChip.click();
    await expect(page.locator("text=Orchestrator Synthesis for")).toBeVisible({ timeout: 5000 });

    // 8. Test Demo Debate Trigger & Action Card Generation
    const demoDebateBtn = page.locator("button", { hasText: "⚡ Trigger BTCUSDT Debate" });
    await demoDebateBtn.click();

    // Verify Action Card appeared in Action Inbox
    await expect(page.locator("text=PENDING_APPROVAL")).toBeVisible({ timeout: 5000 });
    const approveBtn = page.locator("[data-testid='approve-debate-btn']");
    await approveBtn.dispatchEvent("click");

    // Verify status update by switching to Staged tab
    const stagedTab = page.locator("button", { hasText: "Staged" });
    await stagedTab.click();
    await expect(page.locator("text=COMPLETED").first()).toBeVisible({ timeout: 5000 });
  });
});
