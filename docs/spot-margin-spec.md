# Binance isolated spot-margin facts for P4

Retrieved 2026-10-29. This document records public Binance documentation and the public
Isolated Margin Pairs page. Rates and pair limits are snapshots; Binance changes them.
P4 remains paper-only and does not call Binance with credentials.

## Sources

- Binance, [Tiered Leverage Function on Isolated Margin](https://www.binance.com/en-AU/support/faq/detail/7fbadf3c75914efc90e8b831709b644f), updated 2026-01-22. This gives leverage-tier ratios, but its BTC/USDC borrow-limit table is explicitly an example and not current BTC/USDT data.
- Binance, [Margin Level and Risk Control](https://www.binance.com/en/support/faq/detail/360030493931). Isolated margin level is total asset value divided by liabilities plus outstanding interest; the risk ratios vary by leverage tier.
- Binance, [Isolated Margin Pairs and Tier Data](https://www.binance.com/en/margin-data), retrieved 2026-10-29. The Isolated Margin Pairs tab is public. Isolated Margin Tier Data displayed “Log in to view your tier data” and no table while signed out. Thus the public page does not verify tier-1 borrow caps.
- Binance, [Dynamic Margin Interest FAQ](https://www.binance.com/en-AU/support/faq/detail/360030157812), updated 2026-06-16; first-period rule effective 2026-04-30 08:00 UTC.
- Binance, [Margin liquidation FAQ](https://www.binance.com/en-GB/support/faq/how-are-liquidations-triggered-on-binance-margin-c08f0ef718214f40b651a2018ce1f71b), retrieved 2026-10-29.
- Binance, [Spot trading parameters](https://www.binance.com/en-IN/support/faq/detail/115000594711), updated 2025-12-30, and [Spot trading filters](https://developers.binance.com/en/docs/products/derivatives-trading-options/common-definition), last modified 2026-09-07. Per-symbol PRICE_FILTER, LOT_SIZE, MARKET_LOT_SIZE and notional constraints remain sourced from the existing spot filters, not copied into margin settings.

## Interest accounting

The first charge is simple interest prorated by elapsed seconds from the borrow timestamp to
the next top-of-hour settlement:

`first_interest = principal * hourly_rate * elapsed_seconds / 3600`

For a borrow at `12:20`, the first row ends at `13:00` and charges `40/60` of an hour.
At each later hourly settlement, charge one full hour using the rate in force at that
settlement on the outstanding principal. Do not compound interest into principal.

The FAQ says repayment can happen at any time, the first charge is non-refundable after it
is triggered, repayment prevents future periods from accruing, and each settlement charges
a full hour even if repayment happens partway through that hour. Therefore a repay at
`12:50` after borrowing at `12:20` is charged the first `30/60` hour through repayment
(FAQ does not spell out a pre-settlement repayment example; this prorated-to-repay behavior
is **unverified**). A repay at `13:20` has the first charge through `13:00` plus a full
hour charged at `13:00`; it does not receive a 20-minute refund. For a partial repay,
repayment applies to accrued interest before principal; the FAQ does not fully specify how
to split first-period interest when principal is partially repaid before the first
settlement. Until verified, accrue that first period prorated through the repayment time
and mark it **unverified**. Once a settlement has charged a full hour, that charge is not
refunded; only the remaining principal accrues at subsequent settlements.

Binance specifies rounding interest up to 8 decimal places. P4 calculations retain exact
Decimal values internally and use an explicit ceiling-to-8-decimals rule only where an
exchange-equivalent settled amount is required. The paper ledger is unique by
`(margin_account_id, period_end, asset)` and records `period_start`, `period_end`,
`asset`, `principal`, `hourly_rate`, and `amount`, all Decimal/NUMERIC.

## Current public pair observations

The page was set to Regular User and Hourly Interest. The rates below are the visible
values at retrieval time, expressed as percentages per hour. The page's “Borrow Limit” is
shown as a regular-user limit but does not identify a tier; it must not be treated as the
first-tier cap. Its “Risk-Based Liquidation Ratio” field is not the margin-level
liquidation risk ratio documented in the tier FAQ and is not substituted for it.

| Pair | Public max leverage | Call / liquidation level | Base hourly rate | Quote hourly rate | Regular-user displayed borrow limit, not tier-identified |
|---|---|---|---|---|---|
| BTC/USDT | 10x — **verified** against public pair data | 1.10 / 1.05 — **verified** by 10x tier FAQ | BTC 0.00004933% — **verified snapshot** | USDT 0.00065600% — **verified snapshot** | BTC 80 — **unverified as tier cap**; USDT 450,000 — **unverified as tier cap** |
| ETH/USDT | 10x — **verified** against public pair data | 1.10 / 1.05 — **verified** by 10x tier FAQ | ETH 0.00022700% — **verified snapshot** | USDT 0.00065600% — **verified snapshot** | ETH 1,600 — **unverified as tier cap**; USDT 450,000 — **unverified as tier cap** |
| XRP/USDT | 10x — **verified** against public pair data | 1.10 / 1.05 — **verified** by 10x tier FAQ | XRP 0.00039871% — **verified snapshot** | USDT 0.00065600% — **verified snapshot** | XRP 584,000 — **unverified as tier cap**; USDT 450,000 — **unverified as tier cap** |
| WLD/USDT | 5x — **verified** against public pair data | 1.19 / 1.15 — **verified** by 5x tier FAQ | WLD 0.00140158% — **verified snapshot** | USDT 0.00065600% — **verified snapshot** | WLD 66,600 — **unverified as tier cap**; USDT 119,000 — **unverified as tier cap** |

The tier FAQ gives shared risk ratios by leverage class: tier 1 at 10x has margin-call
ratio 1.10 and liquidation risk ratio 1.05; tier 1 at 5x has 1.19 and 1.15. Public pair
max leverage and FAQ ratios are verified. The displayed borrow limits are verified only
as public regular-user values, not as tier-identified caps; that cap status remains
unverified. Under §8.5, those displayed values seed the paper caps with the exact label
"regular-user displayed limit, not tier-identified". They are conservative paper caps,
not claims about Binance's tier-1 allowance. If a supported side has no displayed limit,
its cap is zero and only a borrow on that side is refused with `MARGIN_BORROW_LIMIT`.
Pairs with no settings row still refuse entries with `MARGIN_PAIR_NOT_SUPPORTED`; exits
remain available.

## Settings and pair support

The settings registry is the sole source of runtime defaults, bounds, and risk tiers. For
each §8.5 supported pair, P4.0b registers:

- `margin.pair.<PAIR>.max_leverage`
- `margin.pair.<PAIR>.call_level`
- `margin.pair.<PAIR>.liquidation_level`
- `margin.pair.<PAIR>.max_borrow_<ASSET>` for base and quote assets
- `margin.interest_hourly.<ASSET>` for assets actually used by supported pairs
- `margin.liquidation_fee_pct`

The published liquidation fee is 2% of liquidated assets. BTCUSDT, ETHUSDT and XRPUSDT
are seeded at 10x with call/liquidation levels 1.10/1.05; WLDUSDT is seeded at 5x with
1.19/1.15. Pair caps use the regular-user displayed limits in the table above. Any
unverified cap can be lowered without TOTP; raising it requires TOTP. Lowering leverage,
call/liquidation protection levels, or the liquidation fee is risk-reducing and free;
raising leverage or lowering protective thresholds/fees requires TOTP. For hourly rates,
a higher rate increases accrued liability and is conservative; lowering it requires TOTP.
Defaults are snapshots and must not be represented as live rates. An unlisted pair has no
settings row and remains unsupported.

## Worked accounting examples

These arithmetic examples validate the accounting identities; they do not authorize an
unsupported pair or supply a missing tier-1 cap. Assume price `100`, no fees, and one
USDT collateral unit unless noted.

- Long: post `10 USDT`, borrow `10 USDT`, and spend all `20 USDT` on `0.2 BTC` at `100`;
  no free quote remains. Assets are `20 USDT`, liability is `10 USDT`, and margin level
  is `20/10 = 2.0` before interest. At tier-1 10x liquidation level `1.05`, the long
  reaches its threshold at `0.2 * price / 10 = 1.05`, or `price = 52.5`.
- Long with free quote: post `20 USDT`, borrow `10 USDT`, buy `0.2 BTC` at `100`, and
  retain `10 USDT` quote. Assets are `20 USDT` of BTC plus `10 USDT` quote against
  `10 USDT` debt, so margin level is `30/10 = 3.0`. Its liquidation equation is
  `(0.2 * price + 10) / 10 = 1.05`, or `price = 2.5`.
- Short: post `10 USDT`, borrow `0.1 BTC`, sell at `100`, leaving `20 USDT` in assets
  against `0.1 BTC` debt worth `10 USDT`. Initial margin level is `20/10 = 2.0`. With
  quote assets fixed at `20 USDT` and BTC debt `0.1`, tier-1 10x liquidation level `1.05`
  is reached at `20/(0.1 * price) = 1.05`, or `price = 190.476190476...`.

The formulas assume the debt and account asset inventory shown, omit interest and fees,
and are not executable sizing examples. P4.2 should use exact Decimal arithmetic and
preserve the account's recorded base/quote quantities when checking these identities.
