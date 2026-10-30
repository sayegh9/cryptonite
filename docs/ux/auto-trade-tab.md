# Auto-Trade workspace: UX and implementation contract

Status: approved 2026-10-30 with lead amendments A1–A9 (`reports/p2-spec-review.md`), applied below

Scope: Auto-Trade list, profile creation, and profile detail; the P2.1 API owns route implementation.

## Product contract

Auto-Trade is the operator workspace for managing paper profiles. It shows what each profile is doing, its current risk and health, and gives one place to create, start, pause, stop, inspect, and edit a profile. Profile detail is the only profile editor after P2.4; Mission Control's editor is retired and its cards link here. Do not change trading, routing, sizing, exit, or gate policy in the UI.

Use the existing dark/light/system desk theme, `--brand-*` tokens, components, and spacing. Do not add a UI library, global design tokens, or a workspace-local theme toggle. Do not calculate risk, P&L, liquidation, or cost estimates in the browser. Show API values and their unavailable states. All mutations go through the existing `/api/v1/ai/mandates/*` routes or the P2.1 `/api/v1/ai/profiles/*` routes, with the existing human-operator session and CSRF pattern. Never create a second write path.

Navigation adds `autotrade` with views `list`, `new`, and `profile`. Keep `DEFAULT_WORKSPACE` as `mission`. Canonical links are `?ws=autotrade&view=list`, `?ws=autotrade&view=new`, and `?ws=autotrade&view=profile&id=<uuid>`; a profile link must also support the `?ws=autotrade&id=<uuid>` form. Enter this workspace through the typed `cryptonite:switch-workspace` event (`dispatchSwitchWorkspace`), not direct cross-workspace state mutation.

### Shared labels and display rules

| Data | Display |
|---|---|
| Execution modes | **Ask me**, **Veto window**, **Fully automatic** (these exact labels) |
| Environment | **PAPER** or **LIVE**, from the API. PAPER is the only selectable environment while `LIVE_ENABLED` is false. If an older LIVE profile is returned, show its LIVE badge and an unavailable notice; do not silently relabel it. |
| Lifecycle | **Running**, **Paused**, **Stopping**, **Stopped**, **Halted**, **Archived**. Keep a HALTED profile's positions and lock state visible. |
| Health | Score `0–100` plus the three component scores and “Updated … ago”. No color-only meaning; pair color with text. |
| P&L | Label currency and period. Distinguish realized and unrealized values; use the API's daily-loss target and progress. |
| Missing data | Use **Unavailable** with a retry or reason. Never show zero for missing/unavailable data. |
| Unknown R | Exclude null R from average R and report `Avg R over N of M audited exits`; show unknown-R count within audited exits and the profile fill count separately. Never imply audited outcomes cover every fill. |

## List view

### States and copy

| State | UI and copy |
|---|---|
| Initial loading | Skeleton rows/cards with accessible status text **Loading profiles…**. Keep the page title **Auto-Trade**. |
| Empty success | Heading **No profiles yet**. Body **Create a paper profile to set its markets, risk limits, and trading mode.** Primary button **Create profile**. |
| One or many profiles | Render every profile returned by the API, sorted by most recently updated, newest first as a stable tie-breaker. `GET /mandates` never returns ARCHIVED profiles; STOPPED ones are shown. |
| First-load error | Heading **Profiles unavailable**. Body **We couldn't load your profiles. Check your connection and try again.** Button **Retry**. If operator access is denied, replace the body with **Sign in as an operator to manage profiles.** and offer **Sign in** using the app's existing login flow. |
| Refresh error with cached data | Keep the last successful data and show **Couldn't refresh profiles. Showing the last loaded data.** Button **Retry**. Include the last successful refresh time. |
| One failed action | Keep the card state unchanged until API confirmation. Show the API message when safe, otherwise **Couldn't {action} {profile}. Try again.** and **Retry**. |

### Profile card

Every card contains, in this order: profile name and lifecycle pill; environment badge; mode and switch state; today's realized and unrealized P&L with the API's P&L scale; last action in plain words and its time; health score with freshness/error/breaker breakdown; market, coin count, style, direction, leverage, and dry-run indicator; primary action. When a daily target is set, the scale runs from daily loss limit on the left, through zero, to the target on the right; otherwise show the loss-limit side from zero. Mark the current P&L and label the realized and unrealized amounts. Do not render the scale when the daily limit is unset or unavailable. On a loss-limit breach display **Daily loss limit reached** and the API lifecycle state.

Mode/switch copy:

- Switch on: **Mode: {Ask me | Veto window | Fully automatic}**.
- Switch off with VETO/AUTO configured: **Auto-trade switch off. New entries require approval.** The persisted profile mode remains visible.
- Dry run: **Dry run: orders are simulated.** Never describe a dry-run order as a fill.
- Paused: show the API pause reason, if present, as **Paused: {reason}**; otherwise **Paused**.
- Stopping with open positions: **Stopping: no new entries. {N} open positions run on their exits.** Do not imply that positions are already flat or being closed.
- Halted: **Halted: {reason}. Review positions before resuming.**

Actions are lifecycle-safe and available only when the API permits them: **Open profile**, **Start** (STOPPED only), **Resume** (PAUSED, HALTED, STOPPING; `POST /mandates/{id}/resume`, always TOTP), **Pause**, **Stop**, **Archive** (STOPPED, or PAUSED/HALTED when flat). Start confirmation is titled **Start {PROFILE_NAME}?** and summarizes mode, environment, and dry-run state; buttons **Cancel** and **Start profile**. Resume confirmation is titled **Resume {PROFILE_NAME}?**, names the mode, and says **Open positions continue under this profile.**; it always opens the TOTP prompt. Pause success: **{PROFILE_NAME} paused. Existing positions remain open.** Stop confirmation is titled **Stop {PROFILE_NAME}?** with body **No new entries. Open positions stay open and close on their own stops and targets.** and an unchecked checkbox **Also close open positions now** (sends `close=true`; risk-reducing, no TOTP). Buttons **Cancel** and **Stop profile**. Stop success: **Stop requested. Open positions will close on their stops and targets.** or, with the checkbox, **Stop requested. Closing open positions now.** Archive confirmation: **Archive {PROFILE_NAME}?** / **It disappears from the list. Its history is kept.** / **Cancel** / **Archive profile**; no TOTP. A pause does not imply positions were closed. A disabled action has a visible explanation and accessible description.

### Stop all

Place **Stop all** beside **Create profile**. Enable it only if at least one profile is ACTIVE/RUNNING or PAUSED. On activation, show a modal headed **Stop all profiles?** Copy: **This stops new entries on {N} running or paused profiles. Open positions stay open and close on their own stops and targets, unless you choose to close them now. The master switch is unchanged.** Checkbox **Also close open positions now**, unchecked by default; it sends `{"close": true}`. Buttons: **Cancel** and **Stop {N} profiles**. No TOTP is requested. While pending, disable both buttons and announce **Stopping profiles…**.

The API returns a result for each eligible profile. Show a result list with **Stopped**, **Already stopping**, or the API-provided failure reason for each profile. Partial success copy: **{S} profiles stopped; {F} couldn't be stopped. Review the results and retry the remaining profiles.** Full success: **Stop requested for {N} profiles. Open positions will close on their stops and targets.** (or **…Closing open positions now.** when the checkbox was set) Do not claim every profile is flat. The request affects only ACTIVE and PAUSED profiles, does not touch the master switch or kill switch, and leaves HALTED/STOPPED/ARCHIVED profiles alone.

## Create profile wizard

The wizard is four keyboard-navigable steps with a persistent step indicator: **1 Markets and capital**, **2 Style and direction**, **3 Risk and leverage**, **4 Mode and review**. Show **Step {n} of 4**. Step 1 uses **Continue**; steps 2–3 use **Back** and **Continue**; step 4 uses **Back** and **Create and start**. Back preserves all entered TOTP-free values. Cancel returns to the list; if the draft has changed, confirm with **Discard profile draft?** / **Your changes will be lost.** / **Keep editing** / **Discard draft**. A preset is a starting point only; every field remains reviewable and editable. Display field-level API errors next to the field and retain all values on failure.

### Step 1 — Markets and capital

Fields: profile name; coins/instruments; market set (`SPOT`, `SPOT_MARGIN`, `PERP`); capital budget. Show estimate data from `/ai/profiles/estimate?coins=&style=&direction=`: feed capacity from `INGEST_MAX_SYMBOLS`, requested coins already in feed, coins outside feed, estimated debates/day, and estimated LLM USD/day with the estimate method and source period. Copy:

- Name placeholder **e.g. BTC swing paper**; error **Enter a profile name.**
- Coin help **Choose coins the live feed can cover.**
- Locked coin conflict (HTTP 409): **{COIN} is used by profile {PROFILE_NAME}.** Link **Open profile**. Do not silently remove or reassign the coin.
- Not in feed: **{COIN} is not in the live feed. Onboard it in Scanner Universe before starting this profile.** Action **Open Scanner Universe** dispatches the existing settings navigation event. Keep the coin and mark it blocked; do not drop it.
- Feed capacity: **The live feed supports {CAPACITY} symbols. {COUNT} selected symbols are outside the feed.**
- Estimate loading: **Estimating feed coverage and daily cost…**; unavailable: **Estimate unavailable. You can save this draft, but feed coverage and daily cost are unknown.**
- Capital field label **Capital budget (USD)**; errors **Enter a capital budget greater than zero.** and the API's validation message when present.

### Step 2 — Style and direction

Fields: `style` (`SWING`, `INTRADAY`) and `direction` (`LONG`, `SHORT`, `BOTH`). Explain the frame mapping from the schema: SWING uses 1h/4h; INTRADAY uses 15m/1h. Offer field presets as cards with **Use preset**; after applying, show the resulting strategy preset IDs from `preset_ids_for(style, direction)` and per-coin replay stats from `/ai/profiles/presets/stats`. These IDs are informational and are not separate UI-selected strategies. For each coin/preset show trade count, win rate, average R, range if supplied, replay source/generated time, and quality status. Never hide negative or thin results. Components render only what `/presets/stats` returns; no replay number appears in web source (the table below is context and test-fixture data).

Warnings use these exact templates:

- Thin (`trades < 20`): **Thin replay: {N} trades. Treat these results as limited evidence.**
- Negative (`trades >= 20` and `avg_r < 0`): **Negative replay: {AVG_R} average R over {N} trades. This replay lost on average.**
- Missing data: **No replay data for {COIN}.** For MUBARAK specifically: **No lake data for MUBARAK; no replay result is available.**
- Stats endpoint unavailable: **Replay stats unavailable. You can continue, but results won't be shown.**

Replay facts that must remain visible wherever the relevant presets appear (from `preset-replay-p4.json`):

| Preset and coin | Trades | Win rate | Average R | Quality/copy |
|---|---:|---:|---:|---|
| `swing-squeeze-breakdown-short-1h` · WLDUSDT | 53 | 58.5% | +0.48R | Positive; 53 trades |
| `swing-squeeze-breakdown-short-1h` · XRPUSDT | 60 | 48.3% | +0.22R | Positive; 60 trades |
| `swing-squeeze-breakdown-short-1h` · ETHUSDT | 56 | 46.4% | +0.17R | Positive; 56 trades |
| `swing-squeeze-breakdown-short-1h` · BTCUSDT | 49 | 22.4% | -0.63R | Negative |
| `scalp-multi-tf` · BTCUSDT / ETHUSDT | 0 each | — | — | Thin; dormant in the six-month replay |
| `scalp-multi-tf` · XRPUSDT / WLDUSDT | 0 each | — | — | No replay trades / no 15m data in the replay |
| `scalp-multi-tf-short` · BTCUSDT / ETHUSDT | 1 each | 0% / 100% | -1.75R / +3.69R | Thin; one trade each |
| MUBARAK, any preset | — | — | — | No lake data; no replay result |

Other presets in the replay are mostly negative. The table is a warning/context, not a recommendation. Keep raw stats visible and show `generated_at` and `source_command` from the stats response.

### Step 3 — Risk and leverage

Fields map directly to `ProfileFields`, with these labels: `max_risk_per_trade_pct` **Risk per trade (%)**; `daily_loss_limit_pct` **Daily loss limit (%)**; `max_drawdown_pct` **Maximum drawdown (%)**; `max_leverage` **Maximum leverage**; `max_open_positions` **Maximum open positions**; `max_debates_per_day` **Maximum debates per day**; `exit_tp_r` **Take profit at (R)**; `exit_tp1_scale_pct` **Take profit 1 scale-out (%)**; `exit_trail_activate_pct` **Activate trailing exit at (%)**; `exit_trail_pct` **Trailing distance (%)**; `exit_max_hold_hours` **Maximum holding time (hours)**; `notify_level` **Notifications** (`All`, `Important only`, `None`); `breaker_max_loss_streak` **Losses before breaker**; `breaker_max_reject_streak` **Rejected entries before breaker**. Defaults follow the schema: risk 1%; blank loss limit uses the backend default `min(3 × risk, max_drawdown)` and the returned value is then shown; drawdown 20%; leverage 1×; positions 3; debates/day 6; TP 2R; TP1 scale 50%; trailing and max hold unset; notifications Important; breaker limits 4 losses and 5 rejects. When changing risk, state **Daily loss limit must be at least risk per trade and no greater than maximum drawdown.**

For SPOT_MARGIN/PERP there is no plan-preview route in P2. Explain leverage in plain words from the field values only: **Up to {L}× leverage. Each trade still risks at most {RISK}% of capital at its stop; leverage changes how close liquidation is.** Liquidation price and distance appear per position on the Positions tab from the existing position view. Never calculate liquidation or loss numbers in the browser.

Map schema validation codes to plain copy:

| Code | Copy |
|---|---|
| `SHORT_NEEDS_MARGIN_OR_PERP` | **Short profiles need Spot Margin or Perpetuals. Add one of these markets or choose Long.** |
| `LEVERAGE_NEEDS_MARGIN_OR_PERP` | **Leverage above 1× needs Spot Margin or Perpetuals.** |
| `DAILY_LOSS_REQUIRED` | **Set a daily loss limit before starting.** |
| `DAILY_LOSS_BELOW_RISK` | **Daily loss limit must be at least risk per trade.** |
| `DAILY_LOSS_ABOVE_DRAWDOWN` | **Daily loss limit cannot exceed maximum drawdown.** |
| `TRAIL_ABOVE_ACTIVATION` | **Trail distance cannot exceed its activation threshold.** |
| `LIVE_NOT_AVAILABLE` | **Live trading is not available yet. Choose Paper.** |
| `AUTOTRADE_DISABLED` | **The auto-trade switch is off, so this profile can't start in {MODE}. Choose Ask me, or start it after turning the switch on in Settings.** |
| `PROFILE_INVALID` | List each returned code with the copy above. |
| `TOTP_REQUIRED` | Open the TOTP prompt. |
| 409 coin lock | **{COIN} is used by profile {PROFILE_NAME}.** |
| 409 `MANDATE_PAUSED` / `MANDATE_HALTED` … already covers | **{PROFILE_NAME} is paused or halted and already covers {COIN}. Resume or edit it instead.** |

### Step 4 — Mode and review

Mode choices and copy:

- **Ask me** — **Review and approve each new entry.**
- **Veto window** — **New entries are placed after {veto_window_s} seconds unless you veto them.**
- **Fully automatic** — **New entries can be placed automatically when the auto-trade switch is on.**

Show a complete plain-words summary of all `ProfileFields`, selected coins, feed coverage/estimate status, preset IDs, market, capital, risk limits, exits, notifications, paper environment, and dry-run setting. Initial start recommendation is the checkbox **Start in dry run** (checked by default for a new profile); helper **Simulate orders first. You can turn dry run off later.** Read the master switch (`master_switch` from any `/summary`, or the existing autotrade settings read). When it is off and the chosen mode is Veto window or Fully automatic, show before submit: **The auto-trade switch is off, so this profile can't start in {MODE}. Choose Ask me, or create it now and start it after turning the switch on in Settings.**

Final buttons: **Back**, **Save draft**, and **Create and start**. **Save draft** only saves the TOTP-free draft locally; success: **Draft saved on this device.** `POST /mandates` always requires TOTP, so **Create and start** opens the TOTP prompt first, every time, then submits once to the profile creation route, which creates the stopped profile and attempts start through `profile_lifecycle.start`; do not add a second frontend write. The 201 response carries `started` and `start_error {code, detail}`. When `started` is true, navigate to detail and show **Profile started in dry run.** or **Profile started.** When `started` is false, keep the created profile, show **Profile created, but couldn't start: {reason}. Review the profile and try Start again.**, then navigate to its detail page.

### TOTP prompt

Ask for TOTP when the API identifies a risk-adding operation. This includes creating a profile (always); starting it in VETO/AUTO; resuming a paused, halted or stopping profile (always); adding markets or instruments; widening direction; raising mode, leverage, capital budget, position size/limit, per-trade risk, or drawdown; increasing debate capacity; loosening breakers, hold limits, conviction/R:R or daily limits; turning dry run off; and any risk-adding edit. Reuse the existing MandateEditor prompt pattern; do not persist, log, or place the code in the draft/store. Prompt title **Confirm this risk change**. Detail: **{CHANGE} increases risk, so it needs your current TOTP code.** Input label **TOTP code**, `autocomplete="one-time-code"`, numeric, six digits. Inline invalid-code copy: **That code wasn't accepted. Check the current code in your authenticator app and try again.** Buttons **Cancel** and **Confirm and start** (or **Save changes** for edits). Disable confirmation until six digits are entered and while the request is pending. Keep the user on the same step after an API refusal.

## Profile detail

Header shows profile name, lifecycle, environment, mode/switch, dry-run, health score, and primary lifecycle action. Tabs are **Timeline**, **Performance**, **Positions**, **Settings**. Preserve tab and profile in the URL. On first open, show a detail skeleton and announce **Loading profile…**. Load each tab independently; the active tab shows its own loading status, and one tab's error must not blank other loaded data. Poll list data at no more than 10 seconds and visible detail data at no more than 5 seconds; pause polling while the tab is hidden. Existing `useMandateStore` refreshes server state but does not subscribe to profile decision events; use polling for P2 unless an established profile event source is added. Keep last good data visible with stale time and a retry affordance.

### Timeline

Use `GET /ai/profiles/{id}/decisions?cursor=&limit=`. Page newest first; provide **Load older decisions**. Display timestamp, stage icon plus text (`GATE`, `DEBATE`, `ROUTE`, `APPROVAL`, `ORDER`, `FILL`, `EXIT`, `LIFECYCLE`, `BREAKER`, `SESSION`; an unknown stage renders as its text), outcome, instrument and reason. Do not make meaning icon-only. Empty: **No decisions yet.** Error: **Timeline unavailable.** / **Retry**.

Current decision writers do not emit an `ERROR` or `FAILED` `ai_profile_decisions.outcome`. Observed outcomes include `GATE: ACCEPTED|REFUSED`, `DEBATE: BUY|HOLD|SELL|REDUCE|CLOSE`, `ROUTE: NO_TRADE|ENQUEUED|DRY_RUN_ROUTED|APPROVAL_CREATED|VETO_WINDOW`, `ORDER: DRY_RUN`, `FILL: FILLED`, lifecycle `STARTED|PAUSED|STOPPING|STOPPED|ARCHIVED`, breaker `PAUSED`, and `EXIT` values `STOP|TRAIL|TP1|TP|MANUAL` or the supplied exit reason. `GATE: REFUSED` and `ROUTE: NO_TRADE` are policy/risk decisions, not errors; do not mark them red or count them as errors. `record_decision` itself logs a failed audit insert as a warning and returns false, but does not persist that failure as an outcome.

### Performance

Use `GET /ai/profiles/{id}/performance?range=7d|30d|all`. Include the equity curve from `ai_mandate_equity`, all tagged profile fills, audited exit outcomes, win rate, average R, fees, funding, `paper_margin_interest`, max drawdown, and the R-unknown count. The API lists all fills but cannot durably attribute every close fill to an EXIT decision; show its `outcome_coverage` note with the outcome metrics. These metrics are explicitly scoped to EXIT audit decisions and must not be presented as complete fill outcomes. Display **Avg R over {known} of {total} audited exits** and show the fill count separately. Offer ranges **7 days**, **30 days**, **All time**. Keep currency, period, and paper/live context beside every metric. If there are no fills, show **No profile fills in this period.** Error: **Performance unavailable.** / **Retry**.

### Positions

Use the existing `GET /ai/mandates/{id}` profile view (or the equivalent profile-detail payload) for its `positions` array; do not add a browser-side positions query. Show instrument, side, quantity, entry/mark, unrealized P&L, and market. For margin, show borrowed base/quote and accrued interest only when returned; otherwise label those details **Borrow details unavailable**. For perps, show leverage and liquidation details only when returned. Empty: **No open positions.** Error: **Positions unavailable.** / **Retry**. A STOPPING/HALTED profile can still have positions; show them and any close-in-progress status.

### Settings

Edit the same `ProfileFields` described in wizard step 3 plus name, coins/markets, capital, `direction`, `style`, `environment` (read-only Paper), mode and `dry_run`. Use the existing `SettingCard` inline-edit pattern. Risk reductions can save directly; risk increases invoke the TOTP prompt. Use `PATCH /ai/mandates/{id}` and the existing start/pause/stop routes. Success copy **Changes saved.** Failure copy **Changes weren't saved. Review the highlighted fields and try again.** Confirmation when discarding an inline edit: **Discard changes?** / **Keep editing** / **Discard changes**. The old Mission Control `MandateEditor` is retired after this single editor ships; the Mission Control card action becomes **Open in Auto-Trade**.

### Summary and health score

Use `GET /ai/profiles/{id}/summary` for today's realized plus unrealized P&L, daily loss limit/progress, open-position count, last decision, breaker state/reason, dry-run/mode/master-switch state, and score inputs. The score is computed by the API; the UI displays it. Formula (amendment A4):

```text
freshness = 40 if every profile instrument's live quote is fresh
            20 if some are fresh
             0 if none are (spot-margin instruments use their spot quote)
recent_errors = max(0, 30 - 10 * qualifying errors in the last 24 hours)
breaker = 30 if clear, 0 if active
health = clamp(freshness + recent_errors + breaker, 0, 100)
```

The qualifying persisted error source is a `debate_runs.status = 'FAILED'` row from the last 24 hours joined by `run_id` to that profile's `ai_profile_decisions` row with `stage = 'GATE'` and `outcome = 'ACCEPTED'`. This accepted-gate row is written before model work and carries the profile's `mandate_id`; the failed debate row itself may not retain that ID. Count at most one failure per linked run. Ordinary policy outcomes such as `NO_TRADE`, `GATE: REFUSED`, and `REJECTED` are not errors. A failed run without a linked accepted-gate decision cannot be attributed to a profile and is not counted. Do not infer errors from missing decisions or failed audit inserts. Show **Last decision {time} ago** as information (unscored). A STOPPED profile returns `score: null`: show **Not running** instead of a score. Show the breakdown as **Market data {N}/40**, **Recent failures {N}/30**, **Breaker {N}/30**, and the profile's active breaker reason. If the API cannot return these profile-scoped failed runs, show score unavailable rather than substituting a different error source.

## Four field presets

These are starting configurations, not strategy slugs. Apply the fields below, then derive displayed replay IDs exclusively from `preset_ids_for(style, direction)`. Preserve schema defaults for fields not listed. Each preset uses PAPER. The wizard computes `daily_loss_limit_pct` as shown so a running profile passes `profile_errors`; all values are editable and shown in the step-4 summary.

| Preset | `ProfileFields` values |
|---|---|
| **Conservative swing** | `markets={SPOT}`, `style=SWING`, `direction=LONG`, `max_leverage=1`, `max_risk_per_trade_pct=0.5`, `daily_loss_limit_pct=1.5`, `max_drawdown_pct=20`; replay IDs = `preset_ids_for(SWING, LONG)` → `swing-trend-pullback-1h`, `swing-squeeze-breakout-1h`, `swing-sniper-pullback-15m`. |
| **Leveraged trend** | `markets={PERP}`, `style=SWING`, `direction=BOTH`, `max_leverage=3`, `max_risk_per_trade_pct=1`, `daily_loss_limit_pct=3`, `max_drawdown_pct=20`; replay IDs = `preset_ids_for(SWING, BOTH)` → all six SWING long and short IDs in schema order. |
| **Intraday active** | `markets={SPOT}`, `style=INTRADAY`, `direction=LONG`, `max_leverage=1`, `max_risk_per_trade_pct=0.5`, `daily_loss_limit_pct=1.5`, `max_debates_per_day=12`; replay IDs = `preset_ids_for(INTRADAY, LONG)` → `scalp-multi-tf`, `trend-pullback`, `bollinger-breakout`. Flag the thin/dormant `scalp-multi-tf` results. |
| **Short-only hedge** | `markets={SPOT_MARGIN, PERP}`, `style=SWING`, `direction=SHORT`, `max_leverage=2`, `max_risk_per_trade_pct=0.5`, `daily_loss_limit_pct=1.5`, `max_drawdown_pct=20`; replay IDs = `preset_ids_for(SWING, SHORT)` → `swing-trend-pullback-short-1h`, `swing-squeeze-breakdown-short-1h`, `swing-sniper-pullback-short-15m`. Put the replay table above beside this choice, including negative BTC and positive ETH/XRP/WLD results. |

Unlisted profile fields retain `ProfileFields` defaults: `max_open_positions=3`, `max_debates_per_day=6` except Intraday active's 12, `exit_tp_r=2`, `exit_tp1_scale_pct=50`, trailing/max hold unset, `dry_run=true` in the first-start wizard, `notify_level=IMPORTANT`, `breaker_max_loss_streak=4`, and `breaker_max_reject_streak=5`. Do not mutate schema defaults to implement the presets.

## Keyboard and accessibility flow

- Workspace navigation is in the existing app navigation order. The Auto-Trade entry has visible text **Auto-Trade**, a clear focus ring, and selected state conveyed with `aria-current`.
- On workspace entry, focus stays on the navigation-triggered page heading; announce **Auto-Trade**. Do not steal focus during polling.
- List: Tab reaches **Create profile**, **Stop all**, then each card's **Open profile**, lifecycle actions, and links in DOM order. Card actions have profile-specific accessible names (for example **Pause BTC swing paper**). Enter/Space activates buttons; Escape closes dialogs and returns focus to their trigger.
- Wizard: focus moves to the step heading after Next/Back. Left/right arrows move between mode radio choices and market/preset radio groups; Tab moves between groups. The stepper is a labelled ordered list and does not trap focus. Invalid submission focuses the first invalid field and announces its error. `Shift+Tab` returns through the previous control without losing values.
- TOTP: opening focuses the six-digit field; label and error are programmatically associated; Escape/Cancel returns focus to the initiating action; invalid response keeps focus in the code field.
- Stop all: opening focuses the dialog heading or first explanatory text, then Cancel, then the destructive button. Focus is trapped while modal; Escape is equivalent to Cancel; after completion focus returns to **Stop all** and a live region announces the outcome.
- Detail tabs use a tablist with arrow-key navigation, Home/End support, `aria-selected`, and associated tab panels. On 390px screens the tab row scrolls horizontally without hiding keyboard focus. Timeline pagination is keyboard reachable.
- Respect reduced motion and zoom. Status, warnings, breaker state, and health are never communicated by color alone. Tables have headers; chart summaries provide equivalent text values.

## Layout contract

Use existing app containers and tokens; no new global design system. At 1440px, use the existing centered workspace width, a 12-column content grid, and 24px gaps: list cards flow in two columns with Stop all/Create profile in the header; detail uses an 8-column main area and 4-column summary/risk rail; wizard content is centered and capped at 960px. Keep a persistent step header and primary/secondary actions in the footer. Do not allow tables or content to exceed the workspace width.

At 390px, use one column with 16px page gutters and 16px section gaps. Header actions wrap; primary action stays full-width when space is tight. Cards stack their metrics and actions, keeping status and environment visible at the top. Wizard is one column, one field per row, with sticky bottom Back/Next actions that do not cover focused inputs or the mobile keyboard. Detail summary stacks above horizontally scrollable tabs; performance charts scale to viewport and expose their text summary; performance tables become labelled stacked rows. No horizontal page overflow. At both widths, keep the user’s current theme and existing responsive navigation behavior.

## `useAutoTradeStore` contract for P2.2–P2.4

Implement one Zustand store in `apps/web/src/lib/stores/useAutoTradeStore.ts`. Persist only `view`, `selectedProfileId`, and TOTP-free `wizardDraft`; never persist profile/API data, API errors, estimates, decisions, or TOTP. Reset the draft after successful create or explicit discard. Server data is fetched again on mount. Keep independent loading/error/updated-at state per list, summary, decisions, performance, positions, presets, and estimate; a failed refresh must not replace last good data with an empty result.

Suggested exported state and exact consumer contract:

```ts
type AutoTradeView = "list" | "new" | "profile";
type ProfileTab = "timeline" | "performance" | "positions" | "settings";
type PerformanceRange = "7d" | "30d" | "all";

interface AutoTradeState {
  view: AutoTradeView;
  selectedProfileId: string | null;
  activeTab: ProfileTab;
  profiles: ProfileSummary[];
  listStatus: "idle" | "loading" | "ready" | "error";
  listError: ApiError | null;
  listUpdatedAt: number | null;
  summaryById: Record<string, Loadable<ProfileSummary>>;
  decisionsById: Record<string, CursorPage<ProfileDecision>>;
  performanceById: Record<string, Loadable<ProfilePerformance>>;
  positionsById: Record<string, Loadable<ProfilePositions>>;
  presets: Loadable<PresetStats>;
  estimate: Loadable<ProfileEstimate>;
  wizardDraft: ProfileDraft | null; // ProfileFields + name/coins/capital/mode; never TOTP
  mutationById: Record<string, "idle" | "pending" | "error">;
  actionError: ApiError | null;
  setView(view: AutoTradeView): void;
  selectProfile(id: string | null): void;
  setActiveTab(tab: ProfileTab): void;
  setWizardDraft(draft: ProfileDraft | null): void;
  applyPreset(preset: "conservative-swing" | "leveraged-trend" | "intraday-active" | "short-only-hedge"): void;
  loadProfiles(opts?: { after?: number }): Promise<void>;
  loadSummary(id: string): Promise<void>;
  loadDecisions(id: string, opts?: { cursor?: string; append?: boolean }): Promise<void>;
  loadPerformance(id: string, range: PerformanceRange): Promise<void>;
  loadPositions(id: string): Promise<void>;
  loadPresetStats(): Promise<void>;
  estimateProfile(opts: { coins: string[]; style: Style; direction: Direction }): Promise<void>;
  createProfile(draft: ProfileDraft, totpCode?: string): Promise<ProfileSummary>;
  patchProfile(id: string, patch: ProfilePatch, totpCode?: string): Promise<ProfileSummary>;
  startProfile(id: string, totpCode?: string): Promise<ProfileSummary>;
  pauseProfile(id: string, reason?: string): Promise<ProfileSummary>;
  stopProfile(id: string): Promise<ProfileSummary>;
  stopAllProfiles(): Promise<StopAllResult>;
  clearActionError(): void;
}
```

Expose selectors from the module for consistent consumers: `selectProfileById(state, id)`, `selectProfileSummary(state, id)`, `selectProfileDecisions(state, id)`, `selectProfilePerformance(state, id)`, `selectProfilePositions(state, id)`, `selectSelectedProfile(state)`, `selectIsProfileLoading(state, id)`, and `selectWizardDraft(state)`. Components may use the same selectors with `useAutoTradeStore(selector)`. Mutations use typed API client functions and CSRF; TOTP is a one-call argument and is never copied into state. Successful writes update/refetch only the affected profile and list; stop-all reconciles each returned profile result and then refreshes the list. Polling calls the same load actions and stops when `document.visibilityState !== "visible"`.

P2.1 is responsible for the concrete API response types. Keep those types in the API client and adapt them at the store boundary; do not let components call raw `fetch` or make up server fields.

## P2.0 acceptance checklist

- Empty/loading/error/stale/list-card states, all four wizard steps and validation, detail tabs, Stop all, and TOTP are specified with copy.
- Presets map to the real schema fields and call `preset_ids_for(style, direction)`; replay values/warnings and the thin/missing cases are present.
- Health score uses the agreed formula and only `FAILED` debate runs linked to a profile's accepted GATE decision as recent failures; ordinary refusals are excluded.
- Keyboard flow, 1440px and 390px layouts, store actions/selectors, API boundaries, polling and live-vs-paper behavior are implementable without new UI or trading policy.
