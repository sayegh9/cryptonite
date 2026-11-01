# Browser QA agent: instructions

You test Cryptonite end to end through a web browser, the way the operator uses it: every page, every control, real
paper trades, a strategy instance, the AI Brain and its order cards. You report what works, what fails and how to
reproduce each failure. You do not fix code.

Written 2026-10-14 against `main` `5d65e9d`. Where the app and this document disagree, trust the app and report the
difference.

---

## 1. Hard rules (read twice)

1. **Paper only.** Every order goes to the PAPER engine. Never place, approve or reset anything for **DEMO** (the
   Binance demo account) and never for a live account. The DEMO kill switch stays latched; leave it.
2. **Never touch the operator's own positions.** At the start of 2026-10-14 they are:
   - `MANUAL_TRADE` · BTCUSDT · LONG 0.03 (a manual position)
   - mandate `Brain XRPUSDT.BINANCE` · XRPUSDT · LONG ~363 XRP
   Re-read the baseline in Phase 0 and treat every position you did not open as off limits: no Close, no Reduce, no
   Flatten, no SELL card, no "Emergency Flatten All".
3. **Where you may trade:**
   - Manual paper trades: **ETHUSDT only** (nothing is held there). Manual trades share one bucket per instrument
     with the operator's, so BTC or XRP manual trades would mix with theirs.
   - Brain order cards: **only in the mandate the operator designates for QA** (recommended: `WLDUSDT.BINANCE`,
     which holds nothing; the operator resumes it for you). Never approve a card into `Brain XRPUSDT.BINANCE`: a
     SELL card there sells the operator's whole XRP holding.
   - Strategy instance: **ETHUSDT**, smallest allocation the form accepts (at most $500).
4. **Size limits.** Each manual order at most 0.01 ETH. At most 5 open QA orders at any time. Close everything you
   opened before you finish.
5. **Never confirm a kill-switch trip.** Tripping the PAPER switch halts all paper trading, and the engine resets only
   when no orders or positions are open, which would force closing the operator's positions. You may open the trip
   confirmation and cancel it.
6. **Do not change live settings** (AI budget, model routing, exit thresholds, session policy, risk limits) unless the
   operator says so in this run, and then put every value back as it was. Validation checks (typing an invalid value
   and reading the error) are fine if you cancel.
7. **Credentials.** Do not type, store or repeat passwords, API keys or TOTP secrets. The operator signs in before you
   start. When a step asks for a **TOTP code**, stop and ask the operator for a fresh code; never reuse one. A step
   that asks for the **step-up password** (kill-switch reset) is the operator's to do, not yours.
8. **AI spend.** Brain turns and debates call paid models (daily cap $25). Use at most **12 Brain messages** and **1
   debate** in a run. Note "AI spend today" in Phase 0 and stop AI tests if it rises by more than **$3**.
9. **Label what you create** so it can be found and removed: names start with `QA-<YYYYMMDD>-` (scanner specs,
   strategies); Brain messages start with `QA:`. Do not create mandates; use the one the operator designates.
10. **Stop and ask** when something would break a rule above, when a page offers an action this document does not
    cover that changes money, mandates, limits or settings, or when anything looks like it could affect the operator's
    positions.

## 2. Setup

- App: <http://localhost:3000>. Every page has its own address:

  | Page | Address |
  |---|---|
  | Mission Control (start page) | `/?ws=mission` · `&view=mandates` · `&view=autonomy` |
  | AI Brain | `/?ws=brain&view=home` · `&view=debates` |
  | Terminal · Scanner | `/?ws=terminal` · `/?ws=terminal&view=scanner` |
  | Markets · Portfolio · Research Lab | `/?ws=markets` · `/?ws=portfolio` · `/?ws=research` |
  | Operator · Alerts · Settings | `/?ws=operator` · `/?ws=alerts` · `/?ws=settings` |

- Backend, read-only checks: API reference <http://localhost:8000/docs>; health pages `:8000/health` (API),
  `:8080/health` (ingest), `:8081/health` (outbox), `:8082/health` (scanner, can take 10 s mid-scan),
  `:8085/health` (agent worker).
- Use a desktop window of at least 1280 px wide; Phase 12 also checks 900 px and phone width.
- Prefer reading the page as text or as the accessibility tree; take a **screenshot for every FAIL** and for key
  evidence (filled orders, card outcomes).
- After each page, read the browser console. Report errors other than `401` before sign-in and the expected
  backend-offline noise listed in section 4.

## 3. The run

Work through the phases in order. For each check record PASS, FAIL, BLOCKED (could not run; say why) or SKIPPED
(not allowed by the rules; say which).

### Phase 0: Preflight and baseline (no changes)

1. All five health pages answer `{"status": "ok"}`.
2. The operator is signed in: the header badge reads `PAPER · …` with no "sign in" text, and Operator shows its
   panels (not "Sign in as operator").
3. Record the baseline (screenshots plus text):
   - Portfolio: equity, available cash, every open position (instance, instrument, size).
   - Mission Control: each mandate's status, level, budget used, open/pending; the approval queue; AI spend today;
     the health tiles.
   - Operator: PAPER and DEMO kill-switch states and generation numbers; enforced limits.
4. Ask the operator which mandate is the QA mandate and to **resume it** (TOTP). Confirm it reads ACTIVE.
5. Note the footer stream status (`STREAM ONLINE` or `STREAM OFFLINE · reconnecting`).

### Phase 1: Navigation and layout (every page)

For each page in the table: it loads within 15 s; loading states say what is loading and, after about 8 s, "taking
longer than usual"; nothing reads healthy or empty when its data failed (a failed load must say so and offer
Retry). Check:
- Top tabs, the `…` (More) menu below 1100 px, the header `Brain` drawer (hidden on the AI Brain page), the command
  palette (Ctrl+K / ⌘K): search a pair, jump to AI Debates, open the Copilot.
- Mode badge in the header agrees with Mission Control (mandate count, paused count, approvals required, kill
  switch).
- Footer: version, stream status.

### Phase 2: Markets and Terminal (read)

- Markets: Top Movers, Gainers/Losers, Volume Leaders, heatmap fill (about 10 s is normal); open a coin profile and
  close it.
- Terminal: chart layouts 1×1, 1×2, 2×2 (in 2×2 the side panels step aside); change symbol and timeframe on a chart;
  the order form follows the chart symbol; Watchlist search; Orderbook; Last Price; show/hide the Orderbook, Debate
  and Operator Control panels; the Trade sidebar shows BUY and SELL without clipping.
- The Brain's price chart and Terminal show the same last price within a few ticks.

### Phase 3: Manual paper trades (ETHUSDT only)

In Terminal → Trade (order entry). Before each order read the pre-trade estimate.
1. **Market buy** 0.01 ETH. Expect a fill; Portfolio shows the position with origin Manual; the Blotter/orders
   journal shows the order FILLED with price and quantity.
2. **Limit buy below the market** (about 1% under the ask), 0.005 ETH. Expect it to rest (working) in the orders
   journal. Then **cancel** it from the journal; it reads CANCELED.
3. **Limit buy far above the market** (5% over the ask). Expect a refusal from the price collar
   (`PRICE_COLLAR_REJECT`), shown with its reason; nothing fills.
4. **Reduce 50%** of your ETH position from Portfolio → Active Positions (only your row). Then **Close** the rest.
   Position gone; realized P&L shows.
5. Order gating: the submit button explains why when it is disabled (not signed in, kill switch latched or unknown).
   Read the explanation; do not latch anything to test it.
6. Operator → Manual Trade: open the modal and check the fields (instrument, quantity, limit price); place one
   0.005 ETH market order through it, then close that position from Portfolio.

### Phase 4: Research Lab and strategies

1. Run a backtest: strategy `ema-cross`, instrument `ETHUSDT.BINANCE`, timeframe 1h, lookback 30 days, profile
   "Conservative Institutional". It completes; check the tabs Tearsheet & Curves, Trades Log, Monthly Returns,
   History & Compare (the Action column stays visible), Live vs Backtest. The trades-by-regime table shows or a dash.
2. Run `rsi-reversal` the same way; compare the two runs in History & Compare.
3. Edge cases: Run is disabled until a strategy is selected (says why); an instrument the catalog has no bars for says
   "The catalog has no bars for …".
4. Manage (strategy manager): list strategies; **create** `QA-<date>-strategy` with the template and read the
   validator's result; **delete** it again. Do not delete any other strategy.
5. Approvals and Reports panels: read only; note what is waiting. Do not approve research drafts.

### Phase 5: Strategy instance (initiate a strategy)

1. Portfolio → Deploy Strategy (or Operator → Deploy New): `ema-cross`, `ETHUSDT.BINANCE`, timeframe 1m, smallest
   allocation (≤ $500). It appears in Active Strategy Instances as RUNNING.
2. Leave it for 15 minutes. Record any orders or fills it makes (orders journal, positions with its instance as
   origin). No trade in 15 minutes is not a failure; say so.
3. **Pause**, then **Resume**; the status follows. Open "Analyze Paper vs Backtest Delta" if offered.
4. **Stop** it. If it holds a position, flatten only that instance's position ("Halt & Flatten Strategy" or the row's
   Flatten). Stopped instances stay listed as STOP.

### Phase 6: Scanner

1. Scanner Studio: pick each preset; candidates list with entry, stop, target; "View candle on chart" opens the
   chart at that bar.
2. Scanner builder: open Create Scanner, fill `QA-<date>-scanner` (ETHUSDT, one RSI Oversold rule), add and remove a
   rule, check the validation messages, then **cancel**. Saving needs the operator's agreement: the UI cannot delete
   a spec, and a spec that matches queues debates, which spend the paid AI budget. If the operator agrees, save it,
   check it runs, and list it in the report so the operator can archive it.

### Phase 7: AI Brain (at most 12 messages in total)

Start a new conversation. Begin every message with `QA:`.
1. `QA: analyze WLDUSDT on 1h and tell me if there is a trade`. Expect tool chips (chart, klines, scanner, live
   prices), a price chart widget, and an answer that names the data it used.
2. `QA: show me the last 50 1h candles of SOLUSDT` (any-symbol klines; SOL is not fed, the Brain should still read it).
3. `QA: explain my latest backtest` (should find the Phase 4 run).
4. **Order card, BUY in the QA mandate:** `QA: propose a small market buy of WLDUSDT inside the WLDUSDT.BINANCE
   mandate`. A card appears: ADDS RISK · PAPER, side, size as % of mandate, proposed stop. Approve it. The card must
   move from "Approved · sending the order" to **Filled** (quantity at price) or **Not executed** with a reason,
   within about a minute. "Approved" alone for more than a minute is a FAIL. Check Portfolio for the position under
   the mandate, and Mission Control for the mandate's budget used.
5. **Reject** path: ask for another card and **Reject** it; it reads "Rejected by you"; no order.
6. **SELL to close:** `QA: close my WLDUSDT position in the WLDUSDT.BINANCE mandate`. Approve the SELL card; it ends
   Filled; the mandate holds nothing again.
7. **SELL with nothing held:** `QA: sell ETHUSDT in the WLDUSDT.BINANCE mandate`. Expect no SELL card and a reply that
   says why (spot cannot be shorted).
8. Ask `QA: what happened to the orders I approved?` The answer must match the cards (it may not call an approved
   order filled unless the conversation says so).
9. History column: the conversation is listed under Today; search finds it by a word from your messages; rename it to
   `QA-<date> brain test`; open an older conversation and back; archive the QA conversation and restore it from
   Archived; "Continue a conversation" on the empty screen lists the latest ones. Do not rename or archive the
   operator's conversations.
10. Context rail: positions, AI spend, mandates, conversations all load (or say they could not).
11. Debates view: Action Inbox tabs; the example card is labelled as an example; decision trace drawer opens from a
    card's "Why? (trace)".
12. Copilot drawer (header `Brain` on another page): the same conversation is available there.

### Phase 8: Mission Control

1. Overview: mandate cards (equity, high-water, drawdown vs hard stop, open/pending) match Portfolio; health tiles;
   approval queue; account drawdown line.
2. Debate approval (at most 1 debate): Terminal → AI Debate → "Run Debate Now" on WLDUSDT. If it produces an approval
   for the QA mandate, check it appears in the queue with its countdown, then **Veto** it. If none appears within 10
   minutes, record what the debate concluded.
3. Mandate editor: open the QA mandate and read every field and its explanation. Make one harmless edit only if the
   operator agrees (for example the description), then put it back.
4. Pause the QA mandate here at the end of Phase 7 (reduce-only), check the badge and card update. Resume needs TOTP
   (ask the operator) only if you still need it.
5. Autonomy & Evals: gate checklist, calibration chart, outcomes by regime, shadow models. Breach count must be 0
   for every mandate; any BREACH event during your run is a **critical FAIL**: report the command id at once.

### Phase 9: Operator

1. Kill switches: PAPER reads ARMED (cyan PAPER pill), DEMO reads its real state (violet DEMO pill). Open the PAPER
   trip confirmation and **cancel** it. Do not confirm.
2. Enforced limits: values shown with their source. With the operator's agreement only: tighten one limit (no TOTP),
   check it shows as applied, then restore it (loosening asks for TOTP; ask the operator).
3. Strategy instances panel matches Portfolio. Multi-Asset Ledger & Audit loads (balances, orders, reconciliation).

### Phase 10: Alerts

Create a price alert for ETHUSDT above a level far from the market; it is listed; delete it; it is gone. A failed load
must read as an error, not "No alerts set".

### Phase 11: Settings

Read every section: AI model routing, health, environment, agent configurations, the settings registry (budget,
exit thresholds, session policy). Check that values show what is applied. Validation only: type an invalid value
(for example a negative budget), read the error, cancel. No saves unless the operator agrees (rule 6).

### Phase 12: Accessibility and widths

- Keyboard only: Tab through the header, Mission Control and the Brain composer; focus is always visible; Escape
  closes drawers and the command palette.
- Icon-only buttons have names (read the accessibility tree).
- At 900 px wide: tabs fit or move into More; Terminal's BUY/SELL is not clipped. At phone width (about 390 px):
  History is a drawer, the chat is usable.

### Phase 13: Cleanup (mandatory)

1. Close every position you opened; cancel every QA order still working. Compare with the Phase 0 baseline: the
   operator's positions must be exactly as they were.
2. Your strategy instance is stopped and flat.
3. Delete `QA-` strategies and alerts. List any `QA-` scanner spec for the operator to archive (the UI cannot
   delete specs). Leave the QA conversation archived.
4. Ask the operator to **pause the QA mandate** again if it was paused before (or do it: pausing needs no TOTP).
5. Record AI spend at the end.

## 4. Expected behaviour (do not report these as bugs)

- `401 Unauthorized` in the console before sign-in.
- Markets and Alerts take up to about 10 s; scanner health can take 10 s while a scan runs.
- The footer can flap to `STREAM OFFLINE · reconnecting` for a few seconds.
- A limit buy more than about 2% above the ask is refused with `PRICE_COLLAR_REJECT`.
- An entry larger than the mandate's room is sized down; with less than 1% of the budget left it is refused as
  `MANDATE_POSITION_LIMIT_REACHED` (not a breach).
- A paused mandate only reduces; new entries are refused.
- A SELL card is refused for spot nothing holds.
- A buy limit above the ask fills at once (it is marketable).
- `MANDATE_SIZING_CONFLICT` should never appear. If it does, it is a FAIL (a sizing fault), though the order is
  correctly refused and no breach is recorded.

## 5. Report

Write `qa/report-<YYYY-MM-DD>.md` in the project folder (`~/Documents/projects/cryptonite/qa/`; create it if missing, older runs are in `reports/archive/qa/`), screenshots in
`qa/<YYYY-MM-DD>/`.

1. **Summary:** counts of PASS / FAIL / BLOCKED / SKIPPED, the critical failures first, AI spend used, and a
   statement that the operator's positions match the baseline (or exactly how they differ).
2. **Baseline and end state:** the Phase 0 and Phase 13 records side by side.
3. **Results table:** one row per check:

   | ID | Phase | Function | Steps | Expected | Actual | Result | Evidence |
   |---|---|---|---|---|---|---|---|

   Evidence names screenshots and ids (paper command ids, order ids, card ids, trace ids, backtest run ids).
4. **Defects:** one entry per FAIL: title, severity (Critical: money, mandate, kill switch or data integrity; High: a
   function does not work; Medium: wrong or misleading display; Low: cosmetic), exact steps from a fresh page load,
   expected, actual, evidence, console errors, time (UTC).
5. **Gaps:** functions you could not reach or that this document does not cover.

Severity guide: anything that could place, size, close or refuse an order wrongly, or show an order, position, mandate
or kill switch in the wrong state, is Critical even if it looks small.
