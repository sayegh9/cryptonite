# Cryptonite web app: design critique (2026-10-12)

Method: `design:design-critique` framework (first impression, usability, hierarchy, consistency, accessibility), applied to every workspace and sub-view of the web app at `http://localhost:3000`. Desktop viewport 1440×900, plus the built-in browser pane's native 800px width for the header. Stage: refinement (feature-complete UI, many states still unpopulated).

## Update: authenticated review (after operator sign-in)

Every populated state was re-checked after signing in as operator (session confirmed via `/api/v1/auth/me`). Several findings in the logged-out review were artifacts of that state or of slow first loads. The corrections come first, then what the signed-in review adds.

### Corrections to the logged-out review

- **Markets is not permanently broken.** It takes about 10 seconds to fill (top movers, volume leaders, USDT heatmap). The real problem is the silence during the wait: no progress text and no timeout.
- **Alerts fills in about 10 seconds** with its real empty state ("No alerts set. Create one above."). The skeleton is the wait.
- **Portfolio:** the Execution Guard read UNKNOWN only while loading; it now reads ACTIVE. Emergency Flatten All was disabled only while logged out; it is enabled now.
- **Research Lab:** Target Strategy was empty only while logged out; it now shows RSI Oversold Reversal, and the Run button is correctly enabled.
- **Security & Env** was blank while logged out. It now shows environment health: keys configured, execution mode DEMO, kill switch STANDBY.
- **Brain context rail** said "No active mandate" before sign-in. It now lists both L1 mandates, two paper positions and AI spend of $0.10 of a $25 cap.
- **Mandate editor dropdown:** the options do list both mandates. Not a bug.
- **Kill switch ARMED:** the green ARMED is correct now (`is_latched` is false). The earlier ARMED-beside-error case was real while logged out: `status` stays `null` when the request fails, so the panel renders ARMED (`KillSwitchPanel.tsx:90–124`). The code path still exists, so Priority 2 below stands.

### New findings from the signed-in review

1. **A signed-in operator sees the login form.** `app/page.tsx:98` starts `operatorAuthenticated` as `false` and only sets it after `/auth/me` returns. On this load that call took about 7 seconds, so the Operator Auth modal, with username and TOTP pre-filled, covered the Operator page for the whole time. Show a neutral "Verifying session…" state instead.
2. **Settings tells a signed-in operator to sign in.** `/settings/agents` returns 200 with `{}`, so the panel treats "no agent configurations" as "not loaded" (`SettingsPanel.tsx:200`). Separate empty from unauthorised, and say which. The panel loads only on mount and on the login event, so it never retries.
3. **The global mode badge contradicts the page it sits on.** Across pages it reads "status unknown" (Markets, Portfolio, Terminal), "kill switch unknown" (Settings, Research Lab), and "approvals required" (Brain, Mission Control, Operator). On Portfolio it says "status unknown" while the Execution Guard tile says ACTIVE. For the indicator that says whether trading is safe, one source should drive it everywhere.
4. **Loading is slow everywhere.** One session fired 133 API requests; 25 took over a second and the slowest took 7.3 seconds. The kill-switch status endpoint was polled 22 times, because every mounted copy of the panel polls every 5 seconds, and all workspaces mount at once. The chart bars endpoint took about 5 seconds for five bars, so charts show "Loading market data…" for 10 seconds or more. One Terminal load showed a red "prices unavailable: 500" in the watchlist; the next call returned 200.
5. **The PAPER and DEMO kill switches look identical.** On Operator, two full-width red **TRIP KILL-SWITCH** buttons stack under matching ARMED cards, one for PAPER and one for DEMO. Tripping the wrong environment is an easy mistake.
6. **Flatten and Trip sit side by side.** On Portfolio, **Emergency Flatten All** (orange) and **Trip Kill Switch** (red) are adjacent at similar weight, and both are destructive.
7. **Pending inbox cards have no verdict.** Two Pending cards (XRPUSDT, WLDUSDT) show `NO_PRICE` and "No verdict yet" with "Audit Full Transcript (0 turns)". The approval queue should hold only items that are ready for a decision.
8. **Three vocabularies for kill-switch state.** ARMED (Operator), STANDBY (Settings) and "kill switch unknown" (header). Use one.
9. **Clipping at the pane's 800px width.** The Terminal's Operator Control clips the SELL button and margin text. History & Compare clips its Action column and wraps "legacy metrics" badges into tall rows. Research Lab tabs wrap onto two lines.
10. **Two names for each strategy.** Research Lab shows "RSI Oversold Reversal"; the comparison table shows `rsi-reversal`, `supertrend`, `ema-cross`. Show one display name.

### What works once populated

Portfolio positions table (entry, mark, unrealised P&L, Close actions), Brain context rail (mandates, positions, spend cap), Mandate editor, Settings environment health with its read-only `.env` note, Markets heatmap, History & Compare comparison footer.

### Updated priorities

1. **One source of session state.** Every component reads one authenticated store, re-checks on load, and re-fetches when it changes. This fixes the Operator modal flash, the stale Settings and Brain messages, and the header contradictions.
2. **Unknown must not look healthy.** Fix `KillSwitchPanel` (a null status shows UNKNOWN, never ARMED) and `ActionInbox` (no "Inbox Zero" beside an error).
3. **Faster first paint.** Mount workspaces lazily, de-duplicate the kill-switch poll, speed up `/bars`, and replace the 10-second skeletons with progress text and a timeout.
4. **Separate and label the PAPER and DEMO kill switches**, and separate Flatten from Trip.
5. **Copy and vocabulary.** One display name per strategy, one kill-switch state vocabulary, and distinct empty and unauthorised messages.
6. **Layout at 800–1024px.** Fix the clipped Buy/Sell and Action controls and the wrapping tabs.

The accessibility findings and the AI Brain information-architecture recommendation from the logged-out review still stand.

## Session conditions (logged-out review)

- **Not signed in as operator.** The console logged about 86 `401 Unauthorized` responses. Every operator-gated surface rendered its unauthenticated state: Mission Control data and kill-switch status, mandates, portfolio positions, Live vs Backtest, Brain inbox and positions, agent settings. I did not enter operator credentials or a TOTP code, so the populated states of those views were not reviewed.
- **The live stream flapped** between `STREAM ONLINE` and `STREAM OFFLINE · reconnecting`. Markets and Alerts stayed on skeleton blocks on every visit.
- **Measurements** (font sizes, contrast, target sizes) come from read-only DOM inspection on the Terminal and Portfolio pages. Mobile widths and screen-reader behaviour were not tested.

## Overall impression

The dark, monospace, trading-desk look suits the audience, and the always-visible `PAPER` badge is a strong safety signal. The biggest problem is that the UI tells the operator things it does not know. Several safety-critical panels show "all clear" when their data failed to load, and money-moving controls stay enabled. Fix that before any visual polish.

## Screen-by-screen

| # | View | What I found | Severity |
|---|---|---|---|
| 1 | **Mission Control · Overview** (`?ws=mission`) | Health cards all read "status unavailable". The kill-switch card shows a green **ARMED · EXECUTOR OPERATIONAL** directly under a red "Authentication required" error (`KillSwitchPanel.tsx:90–124`). Across loads the page says "Sign in as operator…", "Mission control data unavailable", and shows a loading state, so it gives conflicting messages. The red **TRIP KILL-SWITCH** button is enabled. | 🔴 |
| 2 | **Mission Control · Mandate editor** (`&view=mandates`) | The form is fully editable under a "Sign in as operator" banner. The TOTP warning appears before any input. Labels such as "L2 veto window" and "Perpetuals (Q5 …)" assume insider knowledge. The Create button is small and low-contrast. | 🟡 |
| 3 | **Mission Control · Autonomy & Evals** (`&view=autonomy`) | "Create a mandate first." has no link to the editor. The Shadow Models note shows SQL table names and an env var (`AI_SHADOW_ALLOW_PAID`) inside the operator UI (`ShadowPanel.tsx:172`). | 🟡 |
| 4 | **Terminal** (`?ws=terminal`, default 1×2) | Works when the stream is up. The chart toolbar is crowded: a "Run Debate" pill wraps to two lines beside symbol, chart type, indicators and timeframes. When the stream drops, the chart shows "Loading market data…" with no timeout. | 🟡 |
| 5 | **Terminal · 2×2 layout + side panels** | Four charts plus three side panels leave each chart about 190px wide, which is barely usable. The Trade panel's **SUBMIT BUY** is enabled while the Operator Control panel reads "Loading kill-switch status…". | 🔴 |
| 6 | **Terminal · AI Debate, Trade sidebars, Brain drawer** | The sidebars open as expected. The Brain drawer covers the right-hand chart. The drawer's header text is very small. | 🟢 |
| 7 | **Scanner** (`?ws=terminal&view=scanner`) | Identical to Terminal. The tab only scrolls the bottom studio into view, and that studio is about 140px tall ("No signals generated yet"). It reads as a duplicate destination. | 🟡 |
| 8 | **Markets** (`?ws=markets`) | Grey skeleton blocks only, on every visit. No heading, message, retry or empty state. It looks broken. | 🔴 |
| 9 | **Portfolio** (`?ws=portfolio`) | Good honest copy in Session Drawdown: "The enforced limit is not available (…), so the budget used cannot be shown." Stat tiles show "—" and "UNKNOWN". The top-right has three buttons of similar weight: **Deploy Strategy** (enabled), **Emergency Flatten All** (disabled, with no reason shown), **Trip Kill Switch** (enabled). The Asset / Strategy exposure toggle works. The journal and ledger tabs and filters are present. | 🟡 |
| 10 | **Research Lab** (`?ws=research`) | The left column is well organised: execution profiles are selectable cards with plain-language descriptions. Problems: "Run Quantitative Backtest" is enabled with an empty Target Strategy; implementation terms like "DefaultFillModel" leak into the card; the instrument select truncates "BTCUSDT.BINANCE". All five sub-tabs (Tearsheet & Curves, Trades Log, Monthly Returns, History & Compare, Live vs Backtest) show empty states. History & Compare says "Select at least one completed backtest above", but nothing is above it (`MultiRunComparator.tsx:190`). Live vs Backtest shows a plain red "Authentication required". | 🟡 |
| 11 | **AI Brain · Brain home** (`?ws=brain&view=home`) | The best screen in the app. It has a clear empty state with three concrete prompts, and a context rail that explains authority in plain words ("No active mandate. Autonomy: L1 Copilot…"). Navigation is redundant: the nav tab "AI Brain", a "Brain \| Debates" toggle and the header "Brain" button all reach the same area. | 🟢 |
| 12 | **AI Brain · Debates** (`&view=debates`) | The three-column layout is dense. A first-time guide opens; Escape closes it, but its X button has no accessible name. The Action Inbox shows **Inbox Zero (All Clear)** directly under a red "debates unavailable: 401" banner (`ActionInbox.tsx:425`), which is false reassurance. A "Sample action card" has live-styled **Approve & Stage** and **Reject** buttons, placed beside real controls. The raw HTTP status "401" is shown to the user. The accessibility tree shows about 13 buttons with no accessible name. Four separate entry points start a debate (chart "Run Debate", "Run Debate Now" in the AI panel, "Trigger BTCUSDT Debate" in the header, and the Brain quick actions). | 🔴 |
| 13 | **Alerts** (`?ws=alerts`) | With the stream down, the list stays on skeleton bars. The New Alert inputs are placeholder-only ("BTCUSDT", "Price") with no labels. The empty copy "No alerts set. Create one above." and the footer "Alerts evaluate every 15s against live Binance prices" set expectations well. | 🟡 |
| 14 | **Settings** (`?ws=settings`) | Three nested navigation columns (section → agent → content) leave most of the screen empty. The AI Agents message "Agent configurations unavailable (not loaded). Sign in as operator or check the API." is clear, but "check the API" means nothing to an operator. **Security & Env** shows a completely blank content pane with no loading state, error or message. The five agent sub-tabs were not checked one by one. | 🟡 |
| 15 | **Operator login** (`?ws=operator`) | A focused, well-labelled modal. In the zoomed capture the Username field appears to hold "admin", and the TOTP placeholder "000000" looks like typed text. Placeholders and values should look different. I could not confirm from the accessibility tree whether "admin" is a value or a placeholder. | 🟢 |
| — | **Header at 800px pane width** | The horizontal tab scroller clips "Portfolio" behind the left scroll chevron. | 🟡 |

## Visual hierarchy and consistency

- **Four treatments for missing data.** Skeleton blocks (Markets, Alerts, Portfolio positions), "—" dashes (Portfolio stats), plain "unavailable" text (Settings, Mission Control) and pulsing "Loading…" text (Terminal, kill switch). Use one pattern: a loading state with a timeout, then an error state that names what failed, when, and offers a retry.
- **Inconsistent error voice.** Some copy is plain ("Sign in as operator…"), some is raw ("debates unavailable: 401", a red "Authentication required").
- **High-risk controls lack a clear hierarchy.** Deploy (solid blue), Emergency Flatten (outlined amber, disabled with no reason) and Trip Kill Switch (solid red) sit together with similar weight. Trip Kill Switch should stay reachable, but its state should be clear.
- **Type is small.** About 92% of visible text on the Terminal (140 of 153 text nodes) and 86% on Portfolio (62 of 72) is 12px or smaller. Dense is fine for a trading desk; this is too small for the key figures.
- **Naming overlaps.** "Scanner" is both a top tab and a sub-view of Terminal. "AI Brain" contains "Brain" and "Debates" views, and the header also has a "Brain" button.

## Usability

- **Unknown states read as healthy** (see rows 1 and 12). This is the most serious problem in the app.
- **Submit controls are not gated on state.** `OrderEntryPanel.tsx:268` disables SUBMIT only while loading, on a reduce-only error, or when quantity is zero. It does not check the session, kill-switch status or mandate state. The backend may refuse these requests, but the UI invites the action.
- **Internal language on operator screens.** SQL tables, env vars, "API", "DefaultFillModel", "Q5", "L2 veto window", and a dead reference to "above". These make the product hard to operate without reading the code.

## Accessibility

Measured with read-only DOM inspection:

- **Text size:** 140 of 153 visible text nodes on the Terminal (about 92%) are 11–12px. On Portfolio, 62 of 72 (about 86%).
- **Contrast:** The Terminal's only text below 4.5:1 is the **BUY / SUBMIT BUY** label at 3.65:1 (`OrderEntryPanel.tsx`). On Portfolio, the only failing text is the disabled "Emergency Flatten All" at 3.58:1. Disabled controls are exempt from WCAG contrast, so that is a note, not a failure.
- **Target size:** 28 interactive elements on the Terminal and 6 on Portfolio are smaller than 24×24px (WCAG 2.2 SC 2.5.8, AA).
- **Names and labels:** About 13 buttons in the Debates view have no accessible name. The guide's close button has no name. Alerts and Mandate inputs rely on placeholders alone.
- **Keyboard:** Escape closes the Brain guide. Full keyboard traversal was not tested.

## What works well

- The `PAPER` badge is always on, and the Brain's authority line ("only inside active mandates") states the rule in plain words.
- Honest partial-failure copy in the Portfolio drawdown tile.
- Research Lab execution profiles as cards with plain descriptions.
- Brain home empty state: three concrete prompts and a context rail.
- Alerts footer states the evaluation cadence.
- Terminal layout controls (1×1, 1×2, 2×2), the Trade sidebar and the Asset / Strategy toggle all respond.
- The operator login modal is clear and focused.
- A coherent dark palette and type system across all workspaces.

## Priority recommendations

1. **Make unknown look unknown (safety; do first).** In `components/operator/KillSwitchPanel.tsx` (lines 90–124), treat a missing or errored status as UNKNOWN: amber or grey, "Kill-switch status unavailable · retry", never ARMED. In `components/brain/ActionInbox.tsx` (around line 425), do not render "Inbox Zero" while `syncError` is set; show "Can't load proposals" with a retry. The Portfolio guards tile already shows UNKNOWN correctly, so reuse that pattern.
2. **Gate money-moving controls on known state.** In `components/terminal/OrderEntryPanel.tsx` (line 268), disable SUBMIT unless the session is authenticated, kill-switch status is known and not latched, and (for AI orders) a mandate is ACTIVE. Show the reason next to the button. Give Emergency Flatten All a visible reason when disabled. Disable Run Quantitative Backtest until a strategy is selected. Keep Trip Kill Switch always reachable.
3. **One empty, loading and error pattern.** Replace skeletons that never resolve (Markets, Alerts, Portfolio positions) with a timeout that leads to an explicit state: what failed, when, and a retry. Markets should not look broken.
4. **Declutter the Terminal.** Keep one debate entry point (in the AI panel) and remove the per-chart "Run Debate" pill. Default to 1×2, and collapse the side panels when 2×2 is chosen. Raise data labels to 12px minimum and body text to 14px.
5. **Remove internal language from operator screens.** `components/mission/ShadowPanel.tsx:172` (SQL tables and env var), `components/SettingsPanel.tsx:200` ("check the API"), `components/ResearchLab/MultiRunComparator.tsx:190` (dead "above" reference), and the mandate editor labels ("Q5", "L2 veto window").
6. **Simplify the AI Brain information architecture.** One AI Brain entry, with Home and Debates as sub-views. Drop either the header "Brain" button or the segmented toggle, and rename "Debates" to describe what the view is.
7. **Accessibility quick wins.** Name the ~13 unnamed buttons in Debates and the guide's close button. Give Alerts and Mandate inputs visible labels. Raise the 28 undersized Terminal targets to 24px. Lift the BUY label above 4.5:1. Keep the "Sample action card" out of the live inbox, or label it as a demo with its buttons disabled.
8. **Narrow-width header.** Use a priority-plus-overflow "More" menu instead of a clipped horizontal scroller.

## Limits and next steps

- Re-run the review after an operator sign-in (the user signs in; the reviewer does not enter the password or TOTP). The populated states of Mission Control, Portfolio, Brain inbox and Live vs Backtest are the main gap.
- Check mobile widths and keyboard traversal.
- The file references were read from the working tree on the current branch and were not run.

## Changes made (2026-10-12)

Written uncommitted in the `main` checkout on 2026-10-12; saved to branch `wip/web-ui-redesign` and finished on
2026-10-13 against main `8ccae28` (after #70, #72–#77, #76/#78). `pnpm typecheck`, `pnpm lint` and `pnpm build` pass.
The full Playwright suite (139 tests) passes against a production build whose API proxy points at a closed port, as in CI.

Finishing changes (2026-10-13):
- The kill-switch store treats a status without a boolean `is_latched` as UNKNOWN, keeping #70's rule.
- The kill-switch heading keeps the accessible name "Execution Kill-Switch · PAPER" that #70's tests use.
- Mission Control: a failed mandate or approval load says "could not be loaded" instead of "No mandates" or
  "Nothing waiting for you"; the store records the approvals outcome (`approvalsOk`). New spec:
  `e2e/mission_unknown_not_empty.spec.ts`.
- `research_lab.spec.ts` looks for the "Strategy" column header by role, since strategy names now show in headers.

### Unknown never looks healthy
- Kill-switch panels show ARMED or LATCHED only from a confirmed answer. A failed or timed-out request shows UNKNOWN, with the reason, the last confirmed time, and a retry (`KillSwitchPanel.tsx`, `lib/stores/useKillSwitchStore.ts`).
- The Action Inbox shows "Can't load proposals" with a retry when the load fails, never "Inbox Zero" (`ActionInbox.tsx`).
- Alerts shows a failed load as an error, not "No alerts set" (`AlertsPanel.tsx`).
- Portfolio's Execution Guard reads Checking…, ARMED, LATCHED, or UNKNOWN. It goes UNKNOWN when its last reading is over 15 s old (`PortfolioConsole.tsx`).
- Order entry is blocked unless the operator session is confirmed and the PAPER kill switch is known and not latched. The reason is shown beside the button. BUY uses emerald-700 so its white label passes contrast (`OrderEntryPanel.tsx`).
- Research Lab's Run button is disabled, with a reason, until a strategy is selected (`ResearchLab.tsx`).

### One session answer
- `lib/stores/useSessionStore.ts` (new) holds checking, authenticated, anonymous, or unreachable. `/auth/me` decides. A 401 from another endpoint triggers a re-check, not a sign-out.
- The operator console shows its content unless the session is known to be signed out. The login form appears only then. A signed-in operator no longer sees it while the check runs (about 7 s on one load).
- Settings, the inbox, and the Brain rail load unless the session is known to be signed out. Settings says the API returned no agent configurations when the set is empty, instead of blaming sign-in.
- Login: the TOTP field starts empty (it was pre-filled "000000"), and an empty code is omitted rather than replaced (`OperatorLoginModal.tsx`).
- Header badge: a loading state reads "loading" and a refused request reads "sign in to see status" (`lib/mission/modeBanner.ts`).

### Loading
- One kill-switch poller per account, shared by every panel (it was one per panel).
- Requests have timeouts (8–15 s). Loading placeholders say "taking longer than usual" after 8 s: Markets, Alerts, chart, positions, Settings, kill switch.
- Positions: the header count and the table use the same condition, so the table never shows skeleton bars beside a count.

### Kill-switch presentation
- PAPER and DEMO are distinct: a cyan pill and "TRIP KILL-SWITCH", a violet pill and an outlined "TRIP DEMO KILL-SWITCH".
- Portfolio's Flatten and Trip sit in their own group. A caption says what each does and why Flatten is off.
- One vocabulary: Settings' "STANDBY" is now "ARMED", and "ACTIVE (TRADING HALTED)" is "LATCHED · TRADING HALTED".

### Inbox
- NO_PRICE (a finished debate with no price data) is out of Pending and sits under "No trade", labelled NO PRICE DATA. The frontend status type did not include it, which made the comparisons wrong.
- Running debates say "Debate running: no verdict yet."
- The tab bar wraps. The sample card is titled "Example card · not a real proposal"; its controls were already not buttons.

### Terminal
- The per-chart "Run Debate" pill is removed. The AI panel's "Run Debate Now" stays the entry point.
- 2×2 steps the Last Price and AI Debate panels aside. Turning them back on stays the operator's choice.
- Scanner view: the charts collapse and the scanner studio fills the workspace. The charts stay mounted, so the layout survives.
- The Trade sidebar keeps a minimum width and scrolls rather than clipping SELL.
- Chart toolbar, layout, and panel chips are 24px tall.
- Watchlist price errors say the last values are kept and it will retry.

### Research Lab
- Strategy names are readable: `rsi-reversal` shows as "RSI reversal" when no name is given. The slug stays in the tooltip.
- History & Compare: the Action column stays visible while the table scrolls, "legacy metrics" no longer wraps, "above" became "in the table", and a run with no slug no longer breaks the view.
- "DefaultFillModel" is gone from the profile description, and the instrument select has room for its value.

### Mission Control
- Mandate labels say what each setting does. The TOTP note is neutral and explains why the create button is off.
- Shadow Models copy no longer names SQL tables or env vars.
- Autonomy's empty state links to the mandate editor.

### Navigation
- Below 1100px, Scanner, Operator, Alerts, and Settings move into a "More" icon menu beside the tabs. Above 1100px the header is unchanged.
- The duplicate Brain drawer button is hidden on the AI Brain page.
- The AI Brain sub-view labels are unchanged, because the e2e suite pins "Debates".

### Accessibility and type
- Icon-only controls named (close and pagination buttons). Alert, login, and reset-form inputs labelled. The Strategy Dashboard loading region has a role.
- `text-2xs` is 12px (was 11px).

### Not fixed
1. **API Redis timeouts (most important).** In the last 30 minutes the API logged 203 `Timeout reading from redis:6379` and 28 HTTP 500s. The Redis container reports healthy with no restarts. Most of the slow loads, and the intermittent "prices unavailable: 500", come from this. The cause is in the API's Redis use, not in the UI. I did not investigate further.
2. **`/api/v1/bars` reads the whole 1-minute history on every request.** It runs `DISTINCT ON` over every parquet file for the symbol, then keeps the last `limit` bars (about 5 s for five bars). A bounded read is the fix, but it changes what happens with stale data, and the resampler requires complete windows. That needs a decision and the API's tests.
3. **Three kill-switch sources for the same PAPER switch**: `/ai/status`, `/paper/kill-switch`, and `/control/kill-switch/status`. They can disagree. The UI shows each one honestly, but they should be a single endpoint.
4. **Not re-measured:** the count of undersized controls (it went down with the toolbar changes), mobile widths, and the agent fleet's "NO HEARTBEAT" badge, which still clips at narrow widths.
