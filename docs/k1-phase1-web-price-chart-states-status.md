# K1 Phase 1 Web: PriceChartCard Empty/Stale States

**Status:** PR OPEN (branch `feat/k1-web-price-chart-states`, base `origin/main` `fbc80f5`). **No live deploy, no main merge.**

**Task:** `01a1207d-d1d2-71b3-a145-3386306df371` (K1 Phase1 web: PriceChartCard empty/stale states).
**Backend counterpart (non-overlapping, owned by Antigravity):** `01a12071` — `packages/agents` only. No shared file changes.

---

## 1. What changed

`apps/web/src/components/brain/BrainWidgets.tsx` — `PriceChartCard` only:

- **Empty result** (`/api/v1/bars` returns no bars): renders **`No stored bars for <symbol>.`**
  (`data-testid="brain-widget-price-chart-empty"`), replacing the previous ambiguous
  "Not enough bars to draw." for the empty case. Single-bar responses still show
  "Not enough bars to draw."; the HTTP-error path is unchanged.
- **Stale chart**: the last stored bar's close time is compared against `now`; when it closed
  **more than 2 full timeframes ago** (`age > 2 × intervalMs(timeframe)`), an amber note renders
  under the chart (`data-testid="brain-widget-price-chart-stale"`):
  `Stored bars are stale — last bar {age} ago (>2×{timeframe}); live REST prices are in the discussion.`
  This makes the **stored-chart vs live-REST-discussion distinction** explicit: the card shows
  analytics-lake **stored** bars (`/api/v1/bars`, single-source 1m-derived resampler); live REST
  prices discussed by the Brain live in the conversation, not in this chart.
- New helpers: `intervalMs(timeframe)` (1m/15m/1h/4h/…/1d) and `formatAge(ms)` («12h», «1d 5h»).
- Data-fetching approach untouched (existing `useEffect` + `fetch`, no Next API changes); the card now
  keeps the raw bar rows (so last-bar close time is available) instead of only parsed closes.

## 2. Files changed

| File | Change |
|---|---|
| `apps/web/src/components/brain/BrainWidgets.tsx` | `PriceChartCard` empty + stale states, helpers |
| `apps/web/e2e/k1_price_chart_states.spec.ts` | **new** — 3 Playwright tests (empty / fresh / stale) |
| `apps/web/package.json` | add the new spec to the `e2e:smoke` gate list |

## 3. Tests (focused, Playwright)

`apps/web/e2e/k1_price_chart_states.spec.ts` drives the Brain Home SSE stream with a mocked
`price_chart` widget (BTCUSDT · 4h) and varies the `/api/v1/bars*` payload:

1. **empty** → `No stored bars for BTCUSDT.`, no chart `img`, no React render errors;
2. **fresh** (last bar closed ~2h ago) → chart renders, stale note absent;
3. **stale** (last bar closed 12h ago = 3×4h > 2×4h) → amber note shows `12h ago`, `>2×4h`
   and the `live REST` / stored-distinction text.

Harness mirrors `brain-home.spec.ts` (offline fixture, mocked bars/spend/sessions/stream SSE),
including the React hydration-error guard.

Run (in the worktree, against an isolated build):
`pnpm exec playwright test e2e/k1_price_chart_states.spec.ts` → **3 passed**; adjacent suites
`brain-home.spec.ts` + `chart_units.spec.ts` → passed (regression check). `tsc --noEmit` and
`eslint` clean on changed files; `pnpm build` succeeds.

## 4. Scope & coordination

- Owned files only: `BrainWidgets.tsx`, the new e2e spec, one line in `package.json` scripts, this note.
- **No** scanner/ingest/API changes, **no** DB migrations, **no** live deploy, **no** main merge.
- Non-overlapping with backend Phase1 (`01a12071`): that track owns `packages/agents/...` and its own
  status doc; no shared file edits. The `show_price_chart` widget contract (kind/symbol/timeframe) is
  unchanged, so the two tracks integrate after merge.

## 5. Exact head / CI / next step

- **Branch** `feat/k1-web-price-chart-states` → **PR #48** https://github.com/sayegh9/cryptonite/pull/48
- **Base:** `origin/main` `fbc80f5` · MERGEABLE · final PR head reported to the lead in the task status message
- **CI (green on final PR head):** `pytest` · `e2e-smoke` (9 specs incl. new) · `web` build · `ruff` · `verify` · CodeRabbit (rate-limited pass)
- Operator next step (not performed): merge, then the normal web rebuild/deploy path; no DB/ingest action.