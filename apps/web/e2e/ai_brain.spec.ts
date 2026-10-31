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
    await expect(page.getByText("Portfolio manager").first()).toBeVisible();
    await expect(page.getByText("Analysts").first()).toBeVisible();
    await expect(page.getByText("Bull & Bear").first()).toBeVisible();
    await expect(page.getByText("Risk panel").first()).toBeVisible();
    await expect(page.getByText("Execution", { exact: true }).first()).toBeVisible();
    await expect(page.getByText("Lead Orchestrator")).toHaveCount(0);
    await expect(page.getByText("NO HEARTBEAT")).toHaveCount(0);

    // 5. Verify Zone 2: the Brain chat (exactly one mounted, K7)
    await expect(page.getByTestId("brain-conversation")).toHaveCount(1);
    await expect(page.locator("#brain-input")).toBeVisible();
    await expect(page.locator("text=Lead Orchestrator Canvas")).toHaveCount(0);
    // F2: the Debates view keeps History and New conversation (Brain Home, the only one mounted)
    await expect(page.getByTestId("brain-home")).toHaveCount(1);
    await expect(page.getByTestId("brain-home").getByRole("button", { name: "History", exact: true })).toBeVisible();
    await expect(page.getByTestId("brain-home").getByRole("button", { name: "New", exact: true })).toBeVisible();

    // 6. Verify Zone 3: Action Inbox
    await expect(page.locator("text=Action & Decision Inbox")).toBeVisible();
    await expect(page.locator("text=Example card · not a real proposal")).toHaveCount(0); // no sample data in the live inbox

    // 7. The symbol chip prefills the Brain input
    await page.getByTestId("brain-symbol-chip").click();
    await expect(page.locator("#brain-input")).toHaveValue(/^Context: BTCUSDT 1h\. /);

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
    // The approve dry run: one ACTIVE mandate allows ETH; its budget (not the account's equity) is the sizing base
    await page.route(`**/api/v1/agents/debates/${RUN_ID}/sizing`, (route) => route.fulfill({ json: sizedIn(ETH_SPOT, 20) }));
    await page.route(`**/api/v1/agents/debates/${RUN_ID}/approve`, async (route) => {
      approveBody = route.request().postDataJSON();
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ status: "approved", run_id: RUN_ID, allocation_pct: 2, mandate_name: "ETH spot", budget_usd: 200 }),
      });
    });

    await page.goto("/");
    await page.locator("button", { hasText: "AI Brain" }).click();
    await page.getByRole("group", { name: "AI Brain view" }).getByRole("button", { name: "Debates" }).click();

    await expect(page.locator("text=PENDING_APPROVAL")).toBeVisible({ timeout: 5000 });
    // The slider starts at the AI's proposal: a share of the mandate's $10,000 budget, not of the account's equity
    await expect(page.getByText("AI proposed 5%")).toBeVisible();
    await expect(page.getByTestId("inbox-size-usd")).toHaveText("≈ $500");
    await expect(page.getByTestId("inbox-size-mandate")).toHaveText("in mandate ETH spot ($10,000 budget)");
    await expect(page.getByTestId("approval-room")).toHaveText("$2,000");

    await page.getByLabel(/Allocation: \d+% of mandate budget/).fill("2");
    await expect(page.getByTestId("inbox-size-usd")).toHaveText("≈ $200");

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

  test("a debate the safety gate refused is a finished no trade: not pending, labelled Not run", async ({ page }) => {
    const RUN_ID = "9a7e0000-2222-4333-8444-555555555555";
    await page.addInitScript(() => {
      localStorage.setItem("csrf_token", "test-csrf");
      localStorage.setItem("cryptonite:ai-brain-tour-seen", "true");
    });
    await page.route("**/api/v1/paper/**", async (route) => {
      const url = route.request().url();
      const body = url.includes("/paper/accounts")
        ? [{ account_id: "PAPER-ACCOUNT-001", venue: "PAPER", currency: "USDT", starting_balance: "10000", cash_balance: "10000", total_equity: "10000", realized_pnl: "0", daily_realized_pnl: "0", daily_loss_latched: false }]
        : url.includes("/paper/kill-switch") ? { id: 1, is_latched: true } : [];
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
    });
    await page.route("**/api/v1/agents/debates*", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          { runId: RUN_ID, instrumentId: "XRPUSDT.BINANCE", status: "GATE_REFUSED", decision: null, timestamp: Date.now(), turns: [] },
        ]),
      });
    });

    await page.goto("/");
    await page.locator("button", { hasText: "AI Brain" }).click();
    await page.getByRole("group", { name: "AI Brain view" }).getByRole("button", { name: "Debates" }).click();

    // Finished, so it is not in the pending tab; the No trade tab holds it with its reason in words
    await expect(page.getByText("Not run (safety gate)")).toHaveCount(0);
    await page.getByRole("button", { name: /^No trade/ }).click();
    await expect(page.getByText("Not run (safety gate)")).toBeVisible({ timeout: 5000 });
    await expect(page.getByTestId("approve-debate-btn")).toHaveCount(0);
  });

  test("Clean inactive and completed proposals removes a GATE_REFUSED run", async ({ page }) => {
    const REFUSED_ID = "9a7e0000-5555-4666-8777-888888888888";
    const REJECTED_ID = "9a7e0000-6666-4777-8888-999999999999";
    await page.addInitScript(() => {
      localStorage.setItem("csrf_token", "test-csrf");
      localStorage.setItem("cryptonite:ai-brain-tour-seen", "true");
    });
    await page.route("**/api/v1/paper/**", async (route) => {
      const url = route.request().url();
      const body = url.includes("/paper/accounts")
        ? [{ account_id: "PAPER-ACCOUNT-001", venue: "PAPER", currency: "USDT", starting_balance: "10000", cash_balance: "10000", total_equity: "10000", realized_pnl: "0", daily_realized_pnl: "0", daily_loss_latched: false }]
        : url.includes("/paper/kill-switch") ? { id: 1, is_latched: true } : [];
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
    });
    // "Clear History" only appears when a completed, rejected or no-trade run exists, so the list holds one of each
    await page.route("**/api/v1/agents/debates*", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          { runId: REFUSED_ID, instrumentId: "XRPUSDT.BINANCE", status: "GATE_REFUSED", decision: null, timestamp: Date.now(), turns: [] },
          { runId: REJECTED_ID, instrumentId: "BTCUSDT.BINANCE", status: "REJECTED", decision: null, timestamp: Date.now(), turns: [] },
        ]),
      });
    });

    await page.goto("/");
    await page.locator("button", { hasText: "AI Brain" }).click();
    await page.getByRole("group", { name: "AI Brain view" }).getByRole("button", { name: "Debates" }).click();

    await page.getByRole("button", { name: /^No trade/ }).click();
    await expect(page.getByText("Not run (safety gate)")).toBeVisible({ timeout: 5000 });

    await page.getByRole("button", { name: "Clear History" }).click();
    await expect(page.getByText("Cleaned inactive and completed proposals")).toBeVisible({ timeout: 5000 });

    // The refused run is gone from the store, not just hidden: no No trade count, no card, and no history left to clean
    await expect(page.getByText("Not run (safety gate)")).toHaveCount(0);
    await expect(page.getByRole("button", { name: /^No trade/ })).not.toContainText(/\d/);
    await expect(page.getByRole("button", { name: "Clear History" })).toHaveCount(0);
  });

  test("Clear History shows for a lone GATE_REFUSED run and clearing removes it", async ({ page }) => {
    const REFUSED_ID = "9a7e0000-7777-4888-8999-aaaaaaaaaaaa";
    await page.addInitScript(() => {
      localStorage.setItem("csrf_token", "test-csrf");
      localStorage.setItem("cryptonite:ai-brain-tour-seen", "true");
    });
    await page.route("**/api/v1/paper/**", async (route) => {
      const url = route.request().url();
      const body = url.includes("/paper/accounts")
        ? [{ account_id: "PAPER-ACCOUNT-001", venue: "PAPER", currency: "USDT", starting_balance: "10000", cash_balance: "10000", total_equity: "10000", realized_pnl: "0", daily_realized_pnl: "0", daily_loss_latched: false }]
        : url.includes("/paper/kill-switch") ? { id: 1, is_latched: true } : [];
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
    });
    // The only run is refused: Clear History is the one way to take it out of the history
    await page.route("**/api/v1/agents/debates*", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          { runId: REFUSED_ID, instrumentId: "XRPUSDT.BINANCE", status: "GATE_REFUSED", decision: null, timestamp: Date.now(), turns: [] },
        ]),
      });
    });

    await page.goto("/");
    await page.locator("button", { hasText: "AI Brain" }).click();
    await page.getByRole("group", { name: "AI Brain view" }).getByRole("button", { name: "Debates" }).click();

    await page.getByRole("button", { name: /^No trade/ }).click();
    await expect(page.getByText("Not run (safety gate)")).toBeVisible({ timeout: 5000 });

    await page.getByRole("button", { name: "Clear History" }).click();
    await expect(page.getByText("Cleaned inactive and completed proposals")).toBeVisible({ timeout: 5000 });

    await expect(page.getByText("Not run (safety gate)")).toHaveCount(0);
    await expect(page.getByRole("button", { name: /^No trade/ })).not.toContainText(/\d/);
    await expect(page.getByRole("button", { name: "Clear History" })).toHaveCount(0);
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
    await page.route(`**/api/v1/agents/debates/${RUN_ID}/sizing`, (route) => route.fulfill({ json: sizedIn(ETH_SPOT, 20) }));
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
    await page.route("**/api/v1/agents/debates/*/sizing", (route) => route.fulfill({ status: 500, contentType: "application/json", body: "{}" }));

    await page.goto("/");
    await page.locator("button", { hasText: "AI Brain" }).click();
    await page.getByRole("group", { name: "AI Brain view" }).getByRole("button", { name: "Debates" }).click();

    await expect(page.getByTestId("brain-conversation")).toHaveCount(1);
    const canvas = page.locator("main");
    await expect(canvas.getByText(/RSI|CRO approved|Setup approved/)).toHaveCount(0);

    // Unknown mandate size: shown as unavailable and Approve is disabled
    await expect(page.getByTestId("inbox-size-usd")).toHaveText("mandate size unavailable");
    await expect(page.getByTestId("approve-debate-btn")).toBeDisabled();
  });

  // --- AMBIGUOUS_MANDATE: two ACTIVE mandates allow the coin; the Inbox picks one (as a Brain card does, #101) -------

  const PICK_RUN = "2026a021-2222-4333-8444-555555555555";
  const AMBIGUOUS_DETAIL =
    "AMBIGUOUS_MANDATE: more than one ACTIVE mandate allows ETHUSDT.BINANCE; nothing was queued; choose the mandate in the Inbox";
  const choice = (over: Record<string, unknown>) => ({
    id: "m-old", name: "BTC-ETH spot L1", autonomy_level: 1, instruments: ["BTCUSDT.BINANCE", "ETHUSDT.BINANCE"],
    max_position_pct: 10, capital_budget_usd: 10000, room_pct: 10, allocation_pct: 10, holds: false, blocked: null,
    tidy: "REMOVE_INSTRUMENT", ...over,
  });
  const THREE_ASSETS = choice({
    id: "m-new", name: "3 assets", instruments: ["BTCUSDT.BINANCE", "ETHUSDT.BINANCE", "WLDUSDT.BINANCE"],
    max_position_pct: 20, capital_budget_usd: 20000, room_pct: 20,
  });
  const ambiguous = (candidates: unknown[]) => ({
    ok: false, code: "AMBIGUOUS_MANDATE", detail: AMBIGUOUS_DETAIL, remedy: { kind: "CHOOSE_MANDATE", candidates },
  });

  /** The 2026-10-21 ETH debate awaiting approval; `sizing` answers each dry run in turn (the last one repeats) */
  async function openPickDebate(page: import("@playwright/test").Page, sizing: unknown[]) {
    const approveBodies: unknown[] = [];
    const mandateCalls: { method: string; path: string; body: unknown }[] = [];
    let reads = 0;
    await page.addInitScript(() => {
      localStorage.setItem("csrf_token", "test-csrf");
      localStorage.setItem("cryptonite:ai-brain-tour-seen", "true");
    });
    await page.route("**/api/v1/paper/**", (route) => route.fulfill({ json: route.request().url().includes("/paper/kill-switch") ? { id: 1, is_latched: false } : [] }));
    await page.route("**/api/v1/agents/debates*", (route) => route.fulfill({
      json: [{ runId: PICK_RUN, instrumentId: "ETHUSDT.BINANCE", status: "PENDING_APPROVAL", decision: "BUY (70% Conviction) • test", timestamp: Date.now(), turns: [] }],
    }));
    await page.route(`**/api/v1/agents/debates/${PICK_RUN}/decision`, (route) => route.fulfill({ json: { run_id: PICK_RUN, allocation_pct: 10 } }));
    await page.route(`**/api/v1/agents/debates/${PICK_RUN}/sizing`, (route) => route.fulfill({ json: sizing[Math.min(reads++, sizing.length - 1)] }));
    await page.route(/\/api\/v1\/ai\/mandates(\/.*)?$/, (route) => {
      const req = route.request();
      if (req.method() === "GET") return route.fulfill({ json: { mandates: [] } });
      mandateCalls.push({ method: req.method(), path: new URL(req.url()).pathname, body: req.postDataJSON() });
      return route.fulfill({ json: { id: "m-old" } });
    });
    await page.route(`**/api/v1/agents/debates/${PICK_RUN}/approve`, (route) => {
      const body = route.request().postDataJSON();
      approveBodies.push(body);
      if (!body?.mandate_id) return route.fulfill({ status: 409, json: { detail: AMBIGUOUS_DETAIL } });
      return route.fulfill({ json: { status: "approved", run_id: PICK_RUN, allocation_pct: body.allocation_pct, mandate_id: body.mandate_id, mandate_name: "3 assets", budget_usd: 2000 } });
    });
    await page.goto("/");
    await page.locator("button", { hasText: "AI Brain" }).click();
    await page.getByRole("group", { name: "AI Brain view" }).getByRole("button", { name: "Debates" }).click();
    await expect(page.locator("text=PENDING_APPROVAL")).toBeVisible({ timeout: 5000 });
    return { approveBodies, mandateCalls, reads: () => reads };
  }

  test("two mandates on the coin: the Inbox picks one, sized by its budget, and approves in it", async ({ page }) => {
    const { approveBodies, mandateCalls } = await openPickDebate(page, [ambiguous([choice({}), THREE_ASSETS])]);
    const picker = page.getByTestId("inbox-choose-mandate");
    await expect(picker).toContainText("2 ACTIVE mandates allow ETHUSDT. Trade this order in:");
    // The first that can take the whole 10% is chosen: BTC-ETH spot L1 has exactly 10% of room left
    await expect(picker.getByLabel(/^BTC-ETH spot L1 \(L1, 10% limit of \$10,000\) · sized to 10% \(\$1,000\)/)).toBeChecked();
    await expect(page.getByTestId("inbox-size-usd")).toHaveText("≈ $1,000");
    await expect(page.getByTestId("approval-room")).toHaveText("$1,000");

    await picker.getByLabel(/^3 assets \(L1, 20% limit of \$20,000\) · sized to 10% \(\$2,000\)/).check();
    await expect(page.getByTestId("inbox-size-usd")).toHaveText("≈ $2,000");
    await expect(page.getByTestId("inbox-size-mandate")).toHaveText("in mandate 3 assets ($20,000 budget)");
    await expect(page.getByTestId("approval-room")).toHaveText("$4,000");
    // Choosing changes no mandate: the overlap box is offered, unchecked
    await expect(picker.getByLabel(/Also stop the overlap, so 3 assets is the only ACTIVE mandate on ETHUSDT/)).not.toBeChecked();

    await page.getByTestId("approve-debate-btn").click();
    await expect(page.getByTestId("confirm-debate-btn")).toHaveText("Confirm approve at 10% in 3 assets");
    await page.getByTestId("confirm-debate-btn").click();
    await expect.poll(() => approveBodies.length).toBe(1);
    expect(approveBodies).toEqual([{ allocation_pct: 10, mandate_id: "m-new" }]);
    expect(mandateCalls).toEqual([]);
    await expect(page.getByText(/Order requested at 10% of mandate 3 assets \(≈ \$2,000\)/)).toBeVisible();
  });

  test("stopping the overlap takes the coin off the other mandate before the approve", async ({ page }) => {
    const { approveBodies, mandateCalls } = await openPickDebate(page, [ambiguous([choice({}), THREE_ASSETS])]);
    const picker = page.getByTestId("inbox-choose-mandate");
    await picker.getByLabel(/^3 assets/).check();
    await expect(picker).toContainText("remove ETHUSDT from BTC-ETH spot L1. This takes room away, so it needs no TOTP.");
    await picker.getByLabel(/Also stop the overlap/).check();
    await page.getByTestId("approve-debate-btn").click();
    await page.getByTestId("confirm-debate-btn").click();
    await expect.poll(() => approveBodies.length).toBe(1);
    expect(mandateCalls).toEqual([{
      method: "PATCH", path: "/api/v1/ai/mandates/m-old",
      body: { instruments: ["BTCUSDT.BINANCE"], source: `debate:${PICK_RUN}` },
    }]);
    expect(approveBodies).toEqual([{ allocation_pct: 10, mandate_id: "m-new" }]);
  });

  test("the mandate that holds the coin is the only choice; the other is disabled with the reason", async ({ page }) => {
    await openPickDebate(page, [ambiguous([
      choice({ name: "Holder", holds: true, tidy: null, room_pct: 5 }),
      THREE_ASSETS, // listed second, blocked by the holder
    ].map((c, i) => (i === 1 ? { ...(c as object), blocked: "Holder holds ETHUSDT.BINANCE, so only it can add to it" } : c)))]);
    const picker = page.getByTestId("inbox-choose-mandate");
    await expect(picker.getByLabel(/^Holder .*sized to 5% \(\$500\), adds to its ETHUSDT/)).toBeChecked();
    await expect(picker.getByLabel(/^3 assets .*only it can add to it/)).toBeDisabled();
    await expect(page.getByTestId("inbox-size-usd")).toHaveText("≈ $1,000");
    await expect(page.getByTestId("approval-resize-note")).toHaveText("Larger than the mandate's room: it will be resized to fit $500.");
  });

  test("an approve refused as AMBIGUOUS_MANDATE reloads the dry run and shows the picker", async ({ page }) => {
    // The dry run found one mandate; a second one then allowed ETH before the operator approved
    const { approveBodies, reads } = await openPickDebate(page, [sizedIn(ETH_SPOT, 10), ambiguous([choice({}), THREE_ASSETS])]);
    await expect(page.getByTestId("inbox-choose-mandate")).toHaveCount(0);
    await page.getByTestId("approve-debate-btn").click();
    await page.getByTestId("confirm-debate-btn").click();
    await expect(page.getByTestId("inbox-action-error")).toHaveText(AMBIGUOUS_DETAIL);
    await expect(page.getByTestId("inbox-choose-mandate")).toContainText("2 ACTIVE mandates allow ETHUSDT.");
    expect(reads()).toBe(2);
    expect(approveBodies).toEqual([{ allocation_pct: 10 }]);
    await expect(page.locator("text=PENDING_APPROVAL")).toBeVisible();
  });
});

const ETH_SPOT = { id: "m-eth", name: "ETH spot", autonomy_level: 1, capital_budget_usd: 10000, max_position_pct: 25 };

/** The approve dry run when one ACTIVE mandate takes the BUY, with `roomPct` of its budget left */
function sizedIn(mandate: typeof ETH_SPOT, roomPct: number) {
  return {
    ok: true, side: "BUY", mandate, room_pct: roomPct, room_usd: (mandate.capital_budget_usd * roomPct) / 100,
    requested_pct: 5, allocation_pct: Math.min(5, roomPct), budget_usd: (mandate.capital_budget_usd * Math.min(5, roomPct)) / 100,
    sell_quantity: null,
  };
}
