import type { Page, Route } from "@playwright/test";
import { test, expect } from "./fixtures/offline";

/**
 * Brain conversation history: every conversation is kept on the server and can be found, opened and continued,
 * renamed, archived and restored. A failed load is reported, never shown as "no conversations".
 */

const OLD = "7a1e0000-0000-4000-8000-000000000001";
const TODAY = "7a1e0000-0000-4000-8000-000000000002";
const now = Date.now();
const iso = (msAgo: number) => new Date(now - msAgo).toISOString();

type Row = {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  archived: boolean;
  message_count: number;
  preview: { role: "user" | "assistant"; content: string };
};

function fixtures(): Row[] {
  return [
    {
      id: TODAY, title: "ETH funding check", created_at: iso(60_000), updated_at: iso(60_000), archived: false,
      message_count: 2, preview: { role: "assistant", content: "Funding is flat across venues." },
    },
    {
      id: OLD, title: "BTC breakout plan", created_at: iso(3 * 86_400_000), updated_at: iso(2 * 86_400_000), archived: false,
      message_count: 6, preview: { role: "user", content: "what invalidates the 60k reclaim?" },
    },
  ];
}

const sse = (events: [string, unknown][]) => events.map(([e, d]) => `event: ${e}\ndata: ${JSON.stringify(d)}\n\n`).join("");

async function mockHistory(page: Page, opts: { failList?: boolean } = {}) {
  const rows = fixtures();
  const calls = { lists: [] as URLSearchParams[], patches: [] as { id: string; body: any; csrf?: string }[], stream: [] as any[] };
  await page.route("**/api/v1/paper/positions*", (route) => route.fulfill({ json: [] }));
  await page.route("**/agent-api/brain/spend", (route) => route.fulfill({ json: { spent_usd: "1.00", cap_usd: "25.00" } }));
  await page.route(/\/agent-api\/brain\/sessions(\?.*)?$/, (route) => {
    const params = new URL(route.request().url()).searchParams;
    calls.lists.push(params);
    if (opts.failList) return route.fulfill({ status: 503, json: { detail: "Brain unavailable" } });
    const q = (params.get("q") || "").toLowerCase();
    const archived = params.get("archived") === "true";
    const sessions = rows
      .filter((r) => r.archived === archived)
      .filter((r) => !q || r.title.toLowerCase().includes(q) || r.preview.content.toLowerCase().includes(q));
    return route.fulfill({ json: { sessions, next_cursor: null } });
  });
  await page.route(/\/agent-api\/brain\/sessions\/[0-9a-f-]+$/, (route: Route) => {
    const id = new URL(route.request().url()).pathname.split("/").pop()!;
    const row = rows.find((r) => r.id === id)!;
    if (route.request().method() === "PATCH") {
      const body = route.request().postDataJSON();
      calls.patches.push({ id, body, csrf: route.request().headers()["x-csrf-token"] });
      Object.assign(row, body);
      return route.fulfill({ json: { session: row } });
    }
    return route.fulfill({
      json: {
        session: { id, title: row.title },
        summary: null,
        messages: [
          { id: 1, role: "user", content: "Plan a BTC breakout entry", widgets: [], trace_id: null, created_at: row.created_at },
          { id: 2, role: "assistant", content: "Wait for a 4H close above 60k.", widgets: [], trace_id: null, created_at: row.updated_at },
        ],
      },
    });
  });
  await page.route("**/agent-api/brain/stream", (route) => {
    const body = route.request().postDataJSON();
    calls.stream.push(body);
    return route.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream" },
      body: sse([
        ["session", { session_id: body.session_id ?? TODAY, trace_id: null }],
        ["token", { text: "Below 58k the setup is void." }],
        ["done", { session_id: body.session_id ?? TODAY, message_id: 3, content: "Below 58k the setup is void." }],
      ]),
    });
  });
  return calls;
}

async function openHistory(page: Page) {
  await page.addInitScript(() => localStorage.setItem("csrf_token", "csrf-test"));
  await page.goto("/");
  await page.getByRole("button", { name: "AI Brain", exact: true }).click();
  await expect(page.getByTestId("brain-home")).toBeVisible();
  await page.getByRole("button", { name: "History" }).click();
  const panel = page.getByTestId("brain-history");
  await expect(panel).toBeVisible();
  return panel;
}

test.describe("Brain conversation history", () => {
  test("lists conversations by day with their last line, and searches them", async ({ page }) => {
    const calls = await mockHistory(page);
    const panel = await openHistory(page);

    await expect(panel.getByRole("region", { name: "Today" })).toContainText("ETH funding check");
    await expect(panel.getByRole("region", { name: "Today" })).toContainText("Funding is flat across venues.");
    const older = panel.getByTestId("brain-history-row").filter({ hasText: "BTC breakout plan" });
    await expect(older).toContainText("You: what invalidates the 60k reclaim?");
    await expect(older.getByLabel("6 messages")).toBeVisible();

    await panel.getByRole("searchbox", { name: "Search conversations" }).fill("breakout");
    await expect(panel.getByTestId("brain-history-row")).toHaveCount(1);
    expect(calls.lists.at(-1)?.get("q")).toBe("breakout");

    await panel.getByRole("searchbox", { name: "Search conversations" }).fill("no such words");
    await expect(panel.getByTestId("brain-history-empty")).toHaveText('No conversation mentions "no such words".');
  });

  test("opens an old conversation and continues it in place", async ({ page }) => {
    const calls = await mockHistory(page);
    const panel = await openHistory(page);

    await panel.getByRole("button", { name: /^BTC breakout plan/ }).click();
    await expect(page.getByText("Wait for a 4H close above 60k.")).toBeVisible();
    await expect(panel.getByRole("button", { name: /^BTC breakout plan/ })).toHaveAttribute("aria-current", "true");

    const input = page.getByPlaceholder(/Ask the Brain/i);
    await input.fill("What invalidates it?");
    await input.press("Enter");
    await expect(page.getByText("Below 58k the setup is void.")).toBeVisible();
    expect(calls.stream[0]).toEqual({ message: "What invalidates it?", session_id: OLD });
  });

  test("renames, archives and restores a conversation", async ({ page }) => {
    const calls = await mockHistory(page);
    const panel = await openHistory(page);

    await panel.getByRole("button", { name: 'Rename "BTC breakout plan"' }).click();
    const name = panel.getByLabel("Conversation name");
    await name.fill("Weekly BTC review");
    await name.press("Enter");
    await expect(panel.getByRole("button", { name: /^Weekly BTC review/ })).toBeVisible();
    expect(calls.patches[0]).toEqual({ id: OLD, body: { title: "Weekly BTC review" }, csrf: "csrf-test" });

    await panel.getByRole("button", { name: 'Archive "Weekly BTC review"' }).click();
    await expect(panel.getByTestId("brain-history-notice")).toHaveText('Archived "Weekly BTC review".');
    await expect(panel.getByTestId("brain-history-row")).toHaveCount(1);
    expect(calls.patches[1]).toEqual({ id: OLD, body: { archived: true }, csrf: "csrf-test" });

    await panel.getByRole("tab", { name: "Archived" }).click();
    await expect(panel.getByTestId("brain-history-row")).toHaveCount(1);
    await panel.getByRole("button", { name: 'Restore "Weekly BTC review"' }).click();
    await expect(panel.getByTestId("brain-history-empty")).toContainText("Nothing archived.");
    expect(calls.patches[2]).toEqual({ id: OLD, body: { archived: false }, csrf: "csrf-test" });
  });

  test("a failed load is reported with a retry, never as no conversations", async ({ page }) => {
    await mockHistory(page, { failList: true });
    const panel = await openHistory(page);
    await expect(panel.getByRole("alert")).toContainText("Conversations could not be loaded.");
    await expect(panel.getByRole("button", { name: "Retry" })).toBeVisible();
    await expect(panel.getByTestId("brain-history-empty")).toHaveCount(0);
  });
});
