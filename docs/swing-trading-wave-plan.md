# Cryptonite AI-First Swing Trading Wave Plan (`WS-S0` – `WS-S5`)

**Status:** Completed (`WS-S0` – `WS-S5` Implemented & Verified)  
**Date:** 17 Oct 2026  
**Baseline Commit:** `c0d3c7e` (Alembic `047`, post-Wave 3 `#92`–`#97`)

---

## 1. Executive Summary & AI-First North Star

Cryptonite is an **AI-first trading platform**: the operator can discover multi-timeframe swing setups, inspect Entry/Stop/Target geometry on inline charts, stage adversarial debates, approve entries with structural stops and targets, scale out `50%` at `TP1`, and run trade post-mortems **entirely inside the AI Brain chat window** (`BrainHome` / `BrainConversation` / `BrainWidgets`), while keeping the Terminal (`ScannerStudio`, `ScannerBuilder`) and Portfolio (`ApprovalRiskContext`) synchronized.

### Rule S1 (`honest_paper`) Compliance
- The live `BTC-ETH spot L1` evaluation window (`2026-10-01` → `2026-10-31`) and the pre-debate gate's deterministic triage rules (`hard_rejects` and `debate:cooldown`) remain untouched.
- Per-position structural stops (`stop_price`) and `TP1` (`take_profit_price`) activate **only** when a proposal or card explicitly carries `proposed_stop_loss` / `proposed_take_profit` (or Redis override `exit:plan:{instrument}:{owner}`); positions without them continue to use the global `ExitThresholds` unchanged.
- Brain `SELL` cards without `reduce_pct` (or with `reduce_pct=100`) continue to close 100% of the mandate's holding with the exact existing audit summary.

---

## 2. Quantitative & Architectural Specification (`WS-S0`)

### 2.1 Fee-Adjusted Swing Expectancy Model
Given round-trip paper/Binance execution friction $F \approx 0.35\%$ ($2 \times 10\text{ bps}$ taker fee $+ 15\text{ bps}$ effective spread/slippage):

$$E[\text{Net Return}] = p_{\text{win}} \cdot (R_{\text{gross}} - F) - (1 - p_{\text{win}}) \cdot (L_{\text{gross}} + F)$$

| Parameter | Target Swing Value | Rationale |
| :--- | :--- | :--- |
| **Macro / Structure / Trigger TFs** | `4h` / `1h` / `15m` | `15m` and `1h` triggers inside `4h` structure survive the 45s debate + L1/L2 approval window while keeping noise stop-outs low. |
| **Minimum Gross R:R** | $\ge 1.80$ (Hard floor $1.50$) | Guarantees net fee-adjusted R:R $\ge 1.45$ after $0.35\%$ round-trip friction. |
| **Anti-Chop Filter** | `volume_ratio >= 0.90` | Ensures scanner candidates never hit `TRIAGE_RULE_CHOP` (`volume_ratio <= 0.8` inside Bollinger Bands) in `gate.py`. |
| **Partial Scale-Out (`TP1`)** | `50%` at `take_profit_price` | Locks in realized profit at `TP1` (`REDUCE_TP1`) and leaves a `50%` runner managed by `ExitMonitor` trailing stop. |
| **Recommended Global Exit Thresholds** | Stop `3.0%`, Trail Activate `2.5%`, Trail `1.2%`, Debate DD `1.8%` | Eliminates the negative payoff asymmetry of the legacy `5.0%` stop / `3.0%` trail default. |

---

## 3. Workstream Breakdown, Agent Roles & Allowed Files

### Wave A — Core Scanner MTF Engine & Guarded Swing Execution (Completed)

#### `WS-S1`: Multi-Timeframe Scanner Engine, Brief Pyramid & Swing Presets
- **Assigned Specialist:** `/agency-backend-architect` (`flash`)
- **Deliverables:**
  1. **`htf_*` Temporal Join in `ScannerEngine.evaluate` (`engine.py`)**: Joins closed higher-timeframe indicator columns (`htf_close`, `htf_open`, `htf_high`, `htf_low`, `htf_ema_20`, `htf_ema_50`, `htf_ema_200`, `htf_rsi`, `htf_macd_hist`, `htf_atr`, `htf_volume_ratio`, `htf_bb_upper`, `htf_bb_lower`, `htf_donchian_upper`, `htf_donchian_lower`) onto `valid_df` via `join_asof(..., on="interval_end_exclusive_ns", strategy="backward")` so multi-timeframe rules (`htf_close > htf_ema_50`, `htf_rsi > 50`) evaluate vectorially with zero lookahead bias.
  2. **Full Timeframe Pyramid in Market Brief (`worker.py`)**: `_execute_scan` populates `frames_by_inst[inst]` with all timeframes in `source_timeframes` (`15m`, `1h`, `4h`) even when triggered by a single `COMMITTED_BAR_EVENT`, and parses optional `risk` config from `universe_data`.
  3. **Curated Swing Presets (`presets.py`)**: Defines `SWING_LIQUID_12` and 3 gate-aligned multi-timeframe swing presets (`swing-trend-pullback-1h`, `swing-sniper-pullback-15m`, `swing-squeeze-breakout-1h`) + `ensure_swing_presets(conn)`.
  4. **Unit Tests (`tests/test_scanner_swing_mtf.py`)**: 6 unit/integration tests covering `htf_*` rules, no-lookahead `join_asof`, multi-timeframe brief population, and preset qualification against `hard_rejects`.

#### `WS-S2`: Per-Position Structural Stops, `TP1` Scale-Out & Partial Sell Cards
- **Assigned Specialist:** `/agency-backend-architect` + `/agency-minimal-change-engineer` (`pro`)
- **Deliverables:**
  1. **Partial Sell (`reduce_pct`) in Brain Order Cards (`widgets.py`, `toolset.py`, `brain.py`)**:
     - Added optional `reduce_pct: Literal[25, 50, 100] | None = None` to `OrderProposal` and `propose_order`.
     - In `services/api/cryptonite_api/routes/brain.py` (`_plan_card`, `_card_summary`), when `side == "SELL"` and `reduce_pct in (25, 50)`, sizes `held` to the exact decimal fraction of the holding (`held * Decimal(reduce_pct) / Decimal(100)`), raising `SPOT_SELL_TOO_SMALL` (`409`) if below `MIN_HOLDING_QTY`, and preserving 100% backward compatibility when `reduce_pct` is `None` or `100`.
  2. **Per-Position Structural Stop & `TP1` (`50%` Scale-Out) in `ExitMonitor` (`exit_monitor.py`)**:
     - Extended `Position` with `stop_price: float | None = None`, `take_profit_price: float | None = None`, and `quantity: float = 0.0`.
     - In `load_positions`, loads `proposed_stop_loss` and `proposed_take_profit` from the position's entry `SUBMIT_ORDER` command in `execution_commands.payload` (or Redis override `exit:plan:{instrument_id}:{owner}`).
     - In `evaluate_exit(pos, best_price, debate_armed, in_drawdown_episode, t, tp1_done=False)`:
       - Triggers `STRUCTURAL_STOP` if `pos.stop_price` is breached before `-t.stop_loss_pct`.
       - Triggers `ExitAction("REDUCE_TP1", "TAKE_PROFIT_1")` when `pos.take_profit_price` is reached and `not tp1_done`.
     - In `act()`, handles `REDUCE_TP1` via `enqueue_tp1_reduce` (50% `SUBMIT_ORDER` `SELL` on `pos.owner_instance`) and records `exit:tp1:{pos.key} = "1"` in Redis.
  3. **Debate Entry Stop/Target Forwarding (`mandate_routing.py`)**:
     - `route_entry` accepts optional `stop_loss: float | None = None` and `take_profit: float | None = None` and persists `"proposed_stop_loss"` and `"proposed_take_profit"` in the command/approval payload.
  4. **Unit & Integration Tests (`tests/test_swing_execution_and_exits.py`)**: 12 tests covering `STRUCTURAL_STOP`, `REDUCE_TP1`, `enqueue_tp1_reduce`, `monitor_once`, and `reduce_pct` SELL card preflight/approval.

---

### Wave B — AI Brain Chat Swing Tools (`WS-S3`, Completed)
- **Assigned Specialist:** `/agency-ai-engineer` (`flash`)
- **Deliverables:**
  1. **`scan_swing_setups` (`READ`) Tool (`tools.py`, `toolset.py`)**: Evaluates `SWING_PRESETS` across `SWING_LIQUID_12` over `15m`/`1h`/`4h` timeframes using `DuckDBLakeRepository` + `ScannerEngine` (with PostgreSQL `signal_candidates` fallback), returning ranked swing candidates with `trend_4h`, `trend_1h`, `trend_15m`, ATR-calibrated `entry`, `stop`, `target`, `rr_ratio`, and `matched_rules`.
  2. **`SwingRadarWidget` & Enhanced `PriceChartWidget` (`widgets.py`, `toolset.py`)**: Added `SwingCandidateItem` and `SwingRadarWidget` (`kind: "swing_radar"`), and extended `show_price_chart` / `PriceChartWidget` with optional `entry`, `stop`, and `target` price levels.
  3. **Unit Tests (`tests/test_brain_swing_tools.py`)**: 10 unit tests covering `scan_swing_setups`, `SwingRadarWidget`, `PriceChartWidget` levels, `RiskClass.READ` invariants, and `BRAIN_TOOLS` ordering.

---

### Wave C — Frontend Chat Widgets v2 & Swing HUD (`WS-S4`, Completed)
- **Assigned Specialist:** `/agency-frontend-developer` + `/agency-ui-designer` (`flash`)
- **Deliverables:**
  1. **Swing Trade Geometry Tokens (`globals.css`)**: Added `--color-swing-entry`, `--color-swing-stop`, `--color-swing-trail`, and `--color-swing-tp1`.
  2. **`SwingRadarCard`, `PriceChartCard` Level Overlays & `OrderCard` R:R Geometry (`BrainWidgets.tsx`, `types.ts`)**:
     - `SwingRadarCard` (`data-testid="swing-radar-card"`): renders MTF trend alignment pills (`4H`/`1H`/`15M`), `R:R` badge, `Entry`/`Stop`/`TP1` geometry, and 1-click `[📈 Chart]`, `[⚡ Debate]`, and `[🎯 Propose Order]` buttons wired to `sendBrainMessage`.
     - `PriceChartCard`: renders dashed SVG horizontal reference lines for `entry`, `stop`, and `target` plus a `swing-levels-legend` strip (`Entry`, `Stop %`, `TP1 %`, `R:R`).
     - `OrderCard`: renders `SCALE-OUT ${reduce_pct}%` (`data-testid="order-card-reduce-pct"`) for partial sells and a visual **Swing R:R Geometry Bar** (`data-testid="order-card-rr-bar"`).
  3. **`/swing` Slash Command & Quick-Chip (`BrainConversation.tsx`)**: Normalizes `/swing [SYMBOL]` into an actionable multi-timeframe swing evaluation prompt and labels `scan_swing_setups` as `"swing radar"`.
  4. **Approval R:R Badge & `ScannerBuilder` Presets (`ApprovalRiskContext.tsx`, `ScannerBuilder.tsx`, `ScannerStudio.tsx`)**: Added `data-testid="approval-rr"`, upgraded `ScannerBuilder` with 1-click Swing Preset templates and valid `ScanSpecCreate` schema (`slug`, `universe`, `rules`, `risk`), and added R:R + MTF badges to `ScannerStudio`.

---

### Wave D — Verification, Docs, Knowledge Graph & Repo Sync (`WS-S5`, Completed)
- **Assigned Specialist:** `/agency-code-reviewer` + `/agency-reality-checker` (`pro`)
- **Verification Results:**
  - `pytest tests/test_scanner_swing_mtf.py tests/test_swing_execution_and_exits.py tests/test_brain_swing_tools.py tests/test_exit_monitor.py tests/test_brain.py tests/test_brain_market_context.py tests/test_brain_card_sell.py tests/test_brain_card_preflight.py`: **333 passed, 0 failed**.
  - `ruff check` on all modified/new Python files: **0 errors**.
  - `npx tsc --noEmit` in `apps/web`: **0 TypeScript errors**.
  - `graphify update .`: Knowledge graph updated and committed.

