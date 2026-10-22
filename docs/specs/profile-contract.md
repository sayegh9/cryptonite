# Profile contract (`cryptonite_schemas.profile`) — P0.2 spec

Pure module: standard library + `pydantic` only, **no sibling package imports** (CLAUDE.md), no I/O. Every later
task imports these names and never redefines them. ADR 0011 is the policy; this file is the exact interface.

## 1. Enums (`StrEnum`, values = names)

```python
class ExecutionMode(StrEnum): ASK; VETO; AUTO
class RunState(StrEnum): RUNNING; PAUSED; STOPPING; STOPPED; HALTED; ARCHIVED
class Market(StrEnum): SPOT; SPOT_MARGIN; PERP
class Direction(StrEnum): LONG; SHORT; BOTH
class TradeSide(StrEnum): LONG; SHORT          # the side of one trade (BOTH is not a trade side)
class Style(StrEnum): SWING; INTRADAY
class Environment(StrEnum): PAPER; LIVE
class NotifyLevel(StrEnum): ALL; IMPORTANT; NONE
class DecisionStage(StrEnum): SCAN; GATE; DEBATE; ROUTE; APPROVAL; ORDER; FILL; EXIT; LIFECYCLE; BREAKER; SESSION
```

## 2. Mode ↔ level, status ↔ run state

```python
MODE_LEVEL: Mapping[ExecutionMode, int] = {ASK: 1, VETO: 2, AUTO: 3}
def mode_of_level(level: int) -> ExecutionMode          # 1/2/3; anything else -> ValueError
def level_of_mode(mode: ExecutionMode) -> int
STATUS_RUN_STATE: Mapping[str, RunState] = {"ACTIVE": RUNNING, "PAUSED": PAUSED, "STOPPING": STOPPING,
                                             "STOPPED": STOPPED, "HALTED": HALTED, "ARCHIVED": ARCHIVED}
def run_state_of(status: str) -> RunState                # unknown -> ValueError
def accepts_entries(status: str) -> bool                 # True only for ACTIVE
def holds_coin_locks(status: str) -> bool                # ACTIVE, PAUSED, STOPPING, HALTED -> True; STOPPED, ARCHIVED -> False
```
HALTED keeps locks (positions may still be open; resume needs TOTP).

## 3. Instruments

Canonical ids are `<SYMBOL>.BINANCE` (spot), `<SYMBOL>-PERP.BINANCE`, `<SYMBOL>-MARGIN.BINANCE`; input may be
lower case or lack the venue (`btcusdt`, `BTCUSDT-PERP`) — normalise with `canonical_instrument`, imported from `cryptonite_schemas.mandate` (same package, allowed), as is
`MAX_PERP_LEVERAGE_CAP`. `mandate.py` must not import `profile.py` in P0.2 (P0.4 may, one direction only).

```python
QUOTE_ASSETS: tuple[str, ...] = ("USDT", "USDC", "FDUSD", "BUSD", "TUSD", "BTC", "ETH", "BNB", "EUR", "TRY")
def market_of(instrument_id: str) -> Market              # suffix -PERP -> PERP, -MARGIN -> SPOT_MARGIN, else SPOT
def underlying_spot(instrument_id: str) -> str           # "BTCUSDT-MARGIN.BINANCE" -> "BTCUSDT.BINANCE"
def instrument_for(spot_or_any_id: str, market: Market) -> str   # inverse: build the id for the market
def symbol_of(instrument_id: str) -> str                 # "BTCUSDT-PERP.BINANCE" -> "BTCUSDT"
def base_asset(instrument_id: str) -> str                # strip the LONGEST matching quote suffix: "ETHBTC" -> "ETH",
                                                         # "BTCUSDT" -> "BTC"; no match -> ValueError
def lock_keys(instruments: Iterable[str]) -> frozenset[str]   # base assets of all instruments
```
Rule for `base_asset`: try quotes longest-first; the base must be non-empty. `"BTCUSDT"`→`BTC`,
`"1000PEPEUSDT"`→`1000PEPE`, `"ETHBTC"`→`ETH`, `"BNBETH"`→`BNB`.

## 4. Market selection (ADR 0011 D8)

```python
def select_market(side: TradeSide, leverage: int, markets: Collection[Market]) -> Market | None
```
| side | leverage | result |
|---|---|---|
| LONG | 1 | SPOT if in markets, else PERP if in markets, else SPOT_MARGIN if in markets, else None |
| LONG | > 1 | PERP if in markets, else SPOT_MARGIN if in markets, else None |
| SHORT | any | PERP if in markets, else SPOT_MARGIN if in markets, else None |

`leverage < 1` → ValueError. (LONG at 1x falls back to PERP/SPOT_MARGIN at 1x when spot is not enabled.)

## 5. Direction and style

```python
def allows_side(direction: Direction, side: TradeSide) -> bool
STYLE_TIMEFRAMES: Mapping[Style, tuple[str, ...]] = {SWING: ("1h", "4h"), INTRADAY: ("15m", "1h")}
PRESETS: Mapping[tuple[Style, TradeSide], tuple[str, ...]] = {
  (SWING, LONG):     ("swing-trend-pullback-1h", "swing-squeeze-breakout-1h", "swing-sniper-pullback-15m"),
  (SWING, SHORT):    ("swing-trend-pullback-short-1h", "swing-squeeze-breakdown-short-1h", "swing-sniper-pullback-short-15m"),
  (INTRADAY, LONG):  ("scalp-multi-tf", "trend-pullback", "bollinger-breakout"),
  (INTRADAY, SHORT): ("scalp-multi-tf-short", "trend-pullback-short", "bollinger-breakdown-short"),
}
def preset_ids_for(style: Style, direction: Direction) -> tuple[str, ...]   # BOTH = LONG ids then SHORT ids, no duplicates, stable order
```
The SHORT slugs are names only here; P1.1 implements those presets with exactly these slugs.

## 6. Profile field validation

```python
class ProfileFields(BaseModel):            # frozen; the new ai_mandates columns plus the ones the rules read
    markets: frozenset[Market]             # non-empty
    direction: Direction = Direction.LONG
    style: Style = Style.SWING
    environment: Environment = Environment.PAPER
    max_leverage: int = 1                  # 1..10 (MAX_PERP_LEVERAGE_CAP from mandate.py)
    max_open_positions: int = 3            # 1..20
    max_debates_per_day: int = 6           # 0..48
    max_risk_per_trade_pct: Decimal = Decimal("1")
    daily_loss_limit_pct: Decimal | None = None
    max_drawdown_pct: Decimal = Decimal("20")
    exit_tp_r: Decimal = Decimal("2.0")    # 0.5..20
    exit_tp1_scale_pct: Decimal = Decimal("50")   # 0..100
    exit_trail_activate_pct: Decimal | None = None   # (0, 50]
    exit_trail_pct: Decimal | None = None            # (0, 50]; when both set: trail_pct <= trail_activate_pct
    exit_max_hold_hours: int | None = None           # 1..720
    dry_run: bool = False
    notify_level: NotifyLevel = NotifyLevel.IMPORTANT
    breaker_max_loss_streak: int = 4       # 1..50
    breaker_max_reject_streak: int = 5     # 1..50

def profile_errors(f: ProfileFields, *, running: bool) -> list[str]
```
`profile_errors` returns stable machine codes (empty list = valid); pydantic `Field` bounds cover the ranges above
(violations raise at construction). Cross-field rules returned as codes:
- `SHORT_NEEDS_MARGIN_OR_PERP` — direction SHORT/BOTH and markets ∩ {SPOT_MARGIN, PERP} empty
- `LEVERAGE_NEEDS_MARGIN_OR_PERP` — max_leverage > 1 and markets == {SPOT}
- `DAILY_LOSS_REQUIRED` — running and daily_loss_limit_pct is None
- `DAILY_LOSS_BELOW_RISK` — daily_loss_limit_pct < max_risk_per_trade_pct
- `DAILY_LOSS_ABOVE_DRAWDOWN` — daily_loss_limit_pct > max_drawdown_pct
- `TRAIL_ABOVE_ACTIVATION` — both trail values set and trail_pct > trail_activate_pct
- `LIVE_NOT_AVAILABLE` — environment LIVE (until P5 lifts it; a module constant `LIVE_ENABLED = False`)

```python
def default_daily_loss_pct(max_risk_per_trade_pct: Decimal, max_drawdown_pct: Decimal) -> Decimal
    # min(3 * risk, drawdown) — ADR 0011 D9, used by migration 050's backfill and the wizard
```

## 7. Risk-adding changes (TOTP)

```python
def risk_adding_profile_change(old: ProfileFields, new: ProfileFields, *, old_mode: ExecutionMode,
                               new_mode: ExecutionMode) -> list[str]
```
Returns the codes of every risk-adding difference (empty = free edit). Risk-adding:
`MODE_RAISED` (level up), `MARKET_ADDED`, `DIRECTION_WIDENED` (LONG→SHORT/BOTH, SHORT→LONG/BOTH),
`LEVERAGE_RAISED`, `OPEN_POSITIONS_RAISED`, `DEBATES_RAISED`, `DRY_RUN_OFF` (True→False),
`BREAKER_LOOSENED` (either streak raised), `HOLD_LIMIT_LOOSENED` (max hold raised or set→None),
`ENVIRONMENT_LIVE` (PAPER→LIVE). Daily-loss/risk/drawdown/R:R/conviction stay covered by the existing
`mandate.risk_adding_change`; do not duplicate them here. Exits TP/trail, notify level, style changes are free.

## 8. Tests (`tests/test_profile_contract.py`)

Every row of every table above; `base_asset` examples incl. no-match ValueError; `select_market` full matrix
(2 sides × {1, 3} leverage × all 7 non-empty market subsets); `preset_ids_for` for all 6 style×direction pairs
(BOTH order and dedupe); `profile_errors` each code alone and combined; `risk_adding_profile_change` each code alone,
a lowering edit returns `[]`; a hypothesis property: for any valid fields, `select_market` result (when not None) is
in `markets`, and SHORT never returns SPOT. 100% branch coverage of `profile.py`
(`uv run pytest tests/test_profile_contract.py --cov=cryptonite_schemas.profile --cov-branch --cov-report=term-missing`
if `pytest-cov` is available; otherwise list uncovered branches by inspection in the report).
