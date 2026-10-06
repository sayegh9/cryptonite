import type { Page, Route } from "@playwright/test";
import { test, expect } from "./fixtures/offline";

/**
 * G1 truthful mode banner (F-M12): the header label is computed from /api/v1/ai/mandates and
 * /api/v1/ai/status (stubbed here; every other backend call gets the offline fixture's 503), never
 * hardcoded, says "unknown" when those fail or go stale, and never truncates a weakening qualifier.
 * Includes the G1 review scenarios: all states, out-of-order responses, a hung backend, header widths.
 */

function mandate(id: string, over: Record<string, unknown> = {}) {
  return {
    id,
    name: `Mandate ${id.slice(0, 4)}`,
    capital_budget_usd: "10000",
    instruments: ["BTCUSDT.BINANCE"],
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
    equity: "10000",
    hwm: "10000",
    drawdown_pct: "0",
    exposure_usd: "0",
    budget_used_pct: "0",
    positions: [],
    pending_approvals: 0,
    pending_proposal: null,
    ...over,
  };
}

const A = "aaaaaaaa-0000-4000-8000-000000000001";
const B = "bbbbbbbb-0000-4000-8000-000000000002";
const C = "cccccccc-0000-4000-8000-000000000003";
const L2 = { autonomy_level: 2, level_name: "L2 Supervised" };

function status(latched = false, reason: string | null = null) {
  return {
    kill_switch: { latched, reason, latched_by: latched ? "operator" : null },
    halted_mandates: [],
    account: null,
    exit_monitor_up: true,
    notifier_up: true,
    budget_alert: false,
    degraded: [],
  };
}

type Reply = { json: unknown } | { status: number };
const reply = (r: Reply) => ("json" in r ? { json: r.json } : { status: r.status, json: { detail: "stubbed failure" } });
// A route whose request the page already aborted (superseded or timed out) cannot be fulfilled
const fulfill = (route: Route, r: Reply) => route.fulfill(reply(r)).catch(() => undefined);

async function stubApprovals(page: Page) {
  await page.route(/\/api\/v1\/ai\/approvals\?/, (route) =>
    route.fulfill({ json: { now: new Date().toISOString(), approvals: [] } }).catch(() => undefined),
  );
}

async function stub(page: Page, opts: { mandates: Reply; status: Reply }) {
  await stubApprovals(page);
  await page.route("**/api/v1/ai/mandates", (route) => fulfill(route, opts.mandates));
  await page.route("**/api/v1/ai/status", (route) => fulfill(route, opts.status));
}

const header = (page: Page) => page.locator("header").getByTestId("mode-banner");
const headerText = (page: Page) => header(page).getByTestId("mode-banner-text");
const isMission = (url: string) => /\/api\/v1\/ai\/(mandates|status|approvals)(\?|$)/.test(url);

const MIXED = { json: { mandates: [mandate(A), mandate(B, L2), mandate(C, { status: "HALTED", halted_reason: "hard stop" })] } };

// [name, mandates, status, banner text, tone]: the 8 states of the G1 review
const STATES: [string, Reply, Reply, string, string][] = [
  ["normal L1", { json: { mandates: [mandate(A), mandate(B), mandate(C, { status: "ARCHIVED" })] } }, { json: status() },
    "PAPER · approvals required · 2 mandates at L1", "gated"],
  ["mixed levels with a halted mandate", MIXED, { json: status() },
    "PAPER · 1 halted · acts after veto window · 1 mandate at L1, 1 at L2", "acting"],
  ["kill switch latched", { json: { mandates: [mandate(A, { autonomy_level: 3 })] } }, { json: status(true, "drawdown backstop") },
    "PAPER · kill switch latched", "latched"],
  ["no active mandates", { json: { mandates: [mandate(A, { status: "PAUSED" })] } }, { json: status() },
    "PAPER · 1 paused · no active mandates", "idle"],
  ["status 500: kill switch unknown leads", { json: { mandates: [mandate(A)] } }, { status: 500 },
    "PAPER · kill switch unknown · approvals required · 1 mandate at L1", "unknown"],
  ["mandates 500", { status: 500 }, { json: status() }, "PAPER · mandates unknown", "unknown"],
  ["both 502", { status: 502 }, { status: 502 }, "PAPER · status unknown", "unknown"],
  ["signed out 401", { status: 401 }, { status: 401 }, "PAPER · status unknown", "unknown"],
];

test.describe("Mode banner (G1)", () => {
  test.beforeEach(async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem("cryptonite:ai-brain-tour-seen", "true"));
  });

  for (const [name, mandates, st, text, tone] of STATES) {
    test(`state: ${name}`, async ({ page }) => {
      await page.setViewportSize({ width: 1280, height: 800 });
      await stub(page, { mandates, status: st });
      await page.goto("/?ws=markets");
      await expect(headerText(page)).toHaveText(text);
      await expect(header(page)).toHaveAttribute("data-tone", tone);
      // full text always in the tooltip; flags never cut
      expect(await header(page).getAttribute("title")).toContain(text);
      for (const flag of await header(page).getByTestId("mode-banner-flag").all()) {
        expect(await flag.evaluate((el) => el.scrollWidth <= el.clientWidth)).toBe(true);
      }
      await expect(page.getByText(/ZERO.?TRADING AUTHORITY/i)).toHaveCount(0);
    });
  }

  test("tooltips and screen-reader text explain the state", async ({ page }) => {
    await stub(page, { mandates: { json: { mandates: [mandate(A)] } }, status: { status: 500 } });
    await page.goto("/?ws=markets");
    await expect(header(page)).toHaveAttribute("title", /Kill-switch state could not be loaded\. 1 active mandate on PAPER\. Every risk-adding order waits/);
    await expect(header(page).locator(".sr-only")).toContainText("Kill-switch state could not be loaded");
  });

  test("signed out: sign-in hint; latched: the reason", async ({ page }) => {
    await stub(page, { mandates: { status: 401 }, status: { status: 401 } });
    await page.goto("/?ws=markets");
    await expect(header(page)).toHaveAttribute("title", /Sign in as operator/);
    await page.unrouteAll({ behavior: "ignoreErrors" });
    await stub(page, { mandates: { json: { mandates: [mandate(A)] } }, status: { json: status(true, "drawdown backstop") } });
    await page.reload();
    await expect(header(page)).toHaveAttribute("title", /latched: drawdown backstop/);
  });

  test("an older response never overwrites a newer one", async ({ page }) => {
    await stubApprovals(page);
    await page.route("**/api/v1/ai/status", (route) => fulfill(route, { json: status() }));
    let n = 0;
    await page.route("**/api/v1/ai/mandates", async (route) => {
      if (++n === 1) {
        await new Promise((r) => setTimeout(r, 3000)); // slow refresh that started before the promotion
        return fulfill(route, { json: { mandates: [mandate(A)] } });
      }
      return fulfill(route, { json: { mandates: [mandate(A, L2)] } });
    });
    await page.goto("/?ws=markets");
    await expect.poll(() => n).toBe(1);
    // the operator promotes the mandate: the app dispatches the event, a new (fast) refresh answers L2
    await page.evaluate(() =>
      window.dispatchEvent(new CustomEvent("cryptonite:mandate-updated", { detail: { mandateId: "x", change: "promoted" } })),
    );
    await expect(headerText(page)).toHaveText("PAPER · acts after veto window · 1 mandate at L2");
    await page.waitForTimeout(3500); // the older, slower answer would have landed by now
    await expect(headerText(page)).toHaveText("PAPER · acts after veto window · 1 mandate at L2");
  });

  test("header poll, Mission Control poll and events share one in-flight refresh", async ({ page }) => {
    await stubApprovals(page);
    await page.route("**/api/v1/ai/status", (route) => fulfill(route, { json: status() }));
    let open = 0;
    let maxOpen = 0;
    let total = 0;
    await page.route("**/api/v1/ai/mandates", async (route) => {
      total++;
      maxOpen = Math.max(maxOpen, ++open);
      await new Promise((r) => setTimeout(r, 2500));
      open--;
      return fulfill(route, { json: { mandates: [mandate(A)] } });
    });
    await page.goto("/?ws=mission");
    await expect(headerText(page)).toHaveText("PAPER · approvals required · 1 mandate at L1");
    await page.waitForTimeout(11_000); // Mission Control polls every 5 s, the header every 15 s
    expect(maxOpen).toBe(1);
    expect(total).toBeGreaterThanOrEqual(2);
  });

  test("a hung backend times out to 'status unknown' and requests stay bounded", async ({ page }) => {
    await page.clock.install();
    await stubApprovals(page);
    let m = 0;
    let s = 0;
    // first answer OK, then hang forever (the page must give up, not wait)
    await page.route("**/api/v1/ai/mandates", (route) => (++m === 1 ? fulfill(route, { json: { mandates: [mandate(A)] } }) : undefined));
    await page.route("**/api/v1/ai/status", (route) => (++s === 1 ? fulfill(route, { json: status() }) : undefined));
    let pending = 0;
    let maxPending = 0;
    page.on("request", (r) => {
      if (isMission(r.url())) maxPending = Math.max(maxPending, ++pending);
    });
    const done = (r: { url: () => string }) => {
      if (isMission(r.url())) pending--;
    };
    page.on("requestfinished", done);
    page.on("requestfailed", done);

    await page.goto("/?ws=markets");
    await expect(headerText(page)).toHaveText("PAPER · approvals required · 1 mandate at L1");
    await expect(header(page)).toHaveAttribute("data-tone", "gated");

    await page.clock.runFor(15_000); // next header poll: hangs
    await expect(headerText(page)).toHaveText("PAPER · approvals required · 1 mandate at L1");
    await page.clock.runFor(8_500); // past the 8 s timeout
    await expect(headerText(page)).toHaveText("PAPER · status unknown");
    await expect(header(page)).toHaveAttribute("data-tone", "unknown");
    await expect(header(page)).toHaveAttribute("title", /could not be loaded/);

    for (let i = 0; i < 3; i++) await page.clock.runFor(15_000);
    await expect(headerText(page)).toHaveText("PAPER · status unknown");
    expect(m).toBeGreaterThanOrEqual(4);
    expect(maxPending).toBeLessThanOrEqual(3); // one refresh (mandates, approvals, status) at a time
  });

  for (const [w, h] of [[1280, 800], [1024, 768], [390, 844], [375, 667]] as const) {
    test(`layout at ${w}px: qualifiers whole, logo clear, Brain reachable`, async ({ page }) => {
      await page.setViewportSize({ width: w, height: h });
      // worst case: kill switch unknown + a halted mandate + mixed levels
      await stub(page, { mandates: MIXED, status: { status: 500 } });
      await page.goto("/?ws=markets");
      await expect(headerText(page)).toHaveText("PAPER · kill switch unknown · 1 halted · acts after veto window · 1 mandate at L1, 1 at L2");
      await expect(header(page)).toHaveAttribute("data-tone", "unknown");

      const info = await page.evaluate(() => {
        const hdr = document.querySelector("header")!;
        const box = (el: Element) => el.getBoundingClientRect();
        const banner = hdr.querySelector('[data-testid="mode-banner"]')!;
        const logo = hdr.firstElementChild!;
        const brain = hdr.querySelector('button[aria-label="Brain"]')!;
        const flags = [...banner.querySelectorAll<HTMLElement>('[data-testid="mode-banner-flag"]')].map((f) => ({
          whole: f.scrollWidth <= f.clientWidth,
          right: box(f).right,
        }));
        const b = box(banner);
        const l = box(logo);
        const nav = box(hdr.querySelector("nav")!);
        const current = box(hdr.querySelector('nav [aria-current="page"]')!);
        const summary = banner.querySelector<HTMLElement>('[data-testid="mode-banner-summary"]')!;
        return {
          bannerInsideHeader: b.bottom <= box(hdr).bottom,
          currentTabVisible: current.left >= nav.left - 1 && current.right <= nav.right + 1,
          summaryWhole: summary.scrollWidth <= summary.clientWidth,
          vw: window.innerWidth,
          docScroll: document.documentElement.scrollWidth,
          overlapsLogo: !(b.left >= l.right || b.top >= l.bottom || b.bottom <= l.top),
          bannerRight: b.right,
          brain: { left: box(brain).left, right: box(brain).right },
          flags,
        };
      });
      expect(info.docScroll).toBeLessThanOrEqual(info.vw);
      expect(info.overlapsLogo).toBe(false);
      expect(info.bannerRight).toBeLessThanOrEqual(info.vw);
      expect(info.brain.left).toBeGreaterThanOrEqual(0);
      expect(info.brain.right).toBeLessThanOrEqual(info.vw);
      expect(info.bannerInsideHeader).toBe(true);
      expect(info.currentTabVisible).toBe(true);
      // phones have no tooltip: the summary wraps instead of truncating
      if (w < 768) expect(info.summaryWhole).toBe(true);
      expect(info.flags).toHaveLength(2);
      for (const f of info.flags) {
        expect(f.whole).toBe(true);
        expect(f.right).toBeLessThanOrEqual(info.vw);
      }
      await page.getByRole("button", { name: "Brain", exact: true }).click();
      await expect(page.getByRole("dialog", { name: "Brain copilot" })).toBeVisible();
    });
  }

  test("1280px: tabs that do not fit are reachable from a visible scroll affordance", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 800 });
    await stub(page, { mandates: MIXED, status: { json: status() } });
    await page.goto("/?ws=markets");
    const nav = page.getByRole("navigation", { name: "Workspaces" });
    const settings = nav.getByRole("button", { name: "Settings", exact: true });
    const overflows = await nav.evaluate((el) => el.scrollWidth > el.clientWidth);
    if (overflows) {
      const right = page.getByTestId("nav-scroll-right");
      await expect(right).toBeVisible();
      for (let i = 0; i < 6 && !(await settings.evaluate((el) => {
        const n = el.closest("nav")!.getBoundingClientRect();
        const r = el.getBoundingClientRect();
        return r.right <= n.right && r.left >= n.left;
      })); i++) {
        await right.click();
        await page.waitForTimeout(400);
      }
      await expect(page.getByTestId("nav-scroll-left")).toBeVisible();
    }
    await settings.click();
    await expect(page).toHaveURL(/ws=settings/);
  });

  test("AI Brain shows the same computed banner, not RISK-FREE", async ({ page }) => {
    await stub(page, { mandates: { json: { mandates: [mandate(A)] } }, status: { json: status() } });
    await page.goto("/?ws=brain&view=home");
    await expect(page.getByTestId("brain-home")).toBeVisible();
    const banners = page.getByTestId("mode-banner");
    await expect(banners).toHaveCount(2);
    await expect(banners.nth(1).getByTestId("mode-banner-text")).toHaveText("PAPER · approvals required · 1 mandate at L1");
    await expect(page.getByText(/RISK.?FREE|SIMULATION MODE/i)).toHaveCount(0);
  });

  test("Scanner no longer claims zero trading authority", async ({ page }) => {
    await stub(page, { mandates: { json: { mandates: [mandate(A)] } }, status: { json: status() } });
    await page.goto("/?ws=terminal");
    await expect(page.locator("#scanner-studio")).toBeVisible();
    await expect(page.getByText("Candidates can start AI debates that feed active mandates")).toHaveCount(1);
    await expect(page.getByText(/zero.?trading|analytical signals only/i)).toHaveCount(0);
  });
});
