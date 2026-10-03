"""Closed strategy research loop (AI-first blueprint Phase 4, A4.1–A4.5).

idea -> draft (Flash, structured) -> save via API (validated there) -> baseline backtest
     -> optimize numeric parameters on the in-sample window (Optuna; the model never guesses numbers)
     -> re-run the best parameters out of sample -> overfitting guard -> Pro review (survivors only)
     -> ResearchReport (JSON artifact)

Every backtest runs in the backtester's sandbox; the loop only talks to the API.
"""

import json
import logging
import math
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import optuna
from cryptonite_agents.gateway import BudgetExceeded, Gateway, GatewayError
from cryptonite_agents.persona import Persona
from cryptonite_agents.research.client import ResearchApiClient, ResearchApiError
from cryptonite_schemas.research import StrategyDraft, StrategyReview

logger = logging.getLogger("cryptonite.agents.research")

REPORTS_DIR = Path(os.getenv("RESEARCH_REPORTS_PATH", "/app/data/research_reports"))
MAX_DRAFT_ATTEMPTS = 3
MIN_TRADES = 10
OOS_SHARPE_RETENTION = 0.5  # out-of-sample Sharpe must keep at least half of the in-sample Sharpe

DRAFTER = Persona(
    "Strategy Drafter", "strategy.draft",
    "You write Nautilus Trader strategies for Cryptonite from a trading idea. Return name, description, "
    "python_code and parameter_space (at most 6 numeric parameters with sensible int/float ranges).\n"
    "Code rules (enforced by a validator; violations are rejected):\n"
    "- Only 'from <module> import <name>' (never plain 'import x', never submodules like "
    "nautilus_trader.indicators.ema). Allowed: indicator classes from nautilus_trader.indicators; Bar, BarType, "
    "OrderSide, PositionSide, Price, Quantity, InstrumentId from nautilus_trader.model; sqrt, log, exp, floor, "
    "ceil, isnan, isfinite, inf, fabs from math; mean, median, stdev, pstdev from statistics; Decimal from "
    "decimal; Any, Optional, List, Dict from typing; deque, defaultdict from collections. e.g.\n"
    "  from nautilus_trader.indicators import ExponentialMovingAverage, SimpleMovingAverage, RelativeStrengthIndex, "
    "BollingerBands, AverageTrueRange, MovingAverageConvergenceDivergence, DonchianChannel, KeltnerChannel, Stochastics\n"
    "  from nautilus_trader.model import Bar\n"
    "  from cryptonite_strategies.base import CryptoniteStrategy, CryptoniteStrategyConfig\n"
    "  Indicators expose .initialized and .value (BollingerBands/DonchianChannel/KeltnerChannel: .upper, "
    ".middle, .lower); bar fields: bar.open, bar.high, bar.low, bar.close, bar.volume (wrap in float()).\n"
    "- Define exactly one config class inheriting CryptoniteStrategyConfig (its __init__ takes instrument_id, "
    "bar_type, each tunable parameter with a default, trade_size=0.01, evaluation_start_ns=0, **kwargs and "
    "calls super().__init__(instrument_id=instrument_id, bar_type=bar_type, trade_size=trade_size, "
    "evaluation_start_ns=evaluation_start_ns, **kwargs)) and exactly one strategy class inheriting "
    "CryptoniteStrategy.\n"
    "- In on_start call super().on_start() and register every indicator with "
    "self.register_indicator_for_bars(self.bar_type, indicator), or it never updates.\n"
    "- Trade only with self.buy_market(), self.sell_market(), self.close_all(); query with self.is_flat(), "
    "self.is_long(), self.is_short(). Always include an exit rule.\n"
    "- No name or attribute starting with an underscore except __init__ (no self._x, no __post_init__); "
    "use plain names like self.fast_ema. No getattr/eval/exec/open/print/type/format, no string type "
    "annotations, no decorators, no module-level code besides imports, constants and the two classes.",
    output_type=StrategyDraft,
)

REVIEWER = Persona(
    "Strategy Reviewer", "strategy.review",
    "You review a candidate trading strategy for Cryptonite that already passed code validation, "
    "parameter optimization and an out-of-sample check. Look for look-ahead bias, overfitting (too many "
    "parameters, fragile thresholds, in-sample vs out-of-sample gap), unrealistic assumptions, missing "
    "exits and regime dependence. Verdict: PROMOTE (paper-trade it), REVISE (promising, name the fix) or "
    "REJECT. Be concrete and brief.",
    output_type=StrategyReview,
)


@dataclass
class ResearchSpec:
    idea: str
    instrument_id: str
    bar_type: str
    timeframe: str
    in_sample: tuple[int, int, int]  # (warmup_start_ns, evaluation_start_ns, evaluation_end_ns)
    out_of_sample: tuple[int, int, int]
    n_trials: int = 12
    seed: int = 7


@dataclass
class ResearchReport:
    id: str
    idea: str
    status: str  # PROMOTE | REVISE | REJECT | FAILED
    reason: str
    strategy_slug: str | None = None
    strategy_name: str | None = None
    best_parameters: dict[str, Any] = field(default_factory=dict)
    baseline: dict[str, Any] = field(default_factory=dict)  # default parameters, in-sample
    in_sample: dict[str, Any] = field(default_factory=dict)
    out_of_sample: dict[str, Any] = field(default_factory=dict)
    overfitting_flags: list[str] = field(default_factory=list)
    review: dict[str, Any] | None = None
    trials: list[dict[str, Any]] = field(default_factory=list)
    cost_usd: float = 0.0
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


def _score(metrics: dict[str, Any]) -> float:
    """In-sample objective: Sharpe, with too few trades scored as a failure."""
    sharpe = metrics.get("sharpe_ratio")
    if sharpe is None or (isinstance(sharpe, float) and math.isnan(sharpe)):
        return -10.0
    return float(sharpe) if (metrics.get("total_trades") or 0) >= MIN_TRADES else -10.0 + (metrics.get("total_trades") or 0) / 100


def overfitting_flags(in_sample: dict[str, Any], out_of_sample: dict[str, Any]) -> list[str]:
    """A4.5: reasons not to trust the in-sample result."""
    flags = []
    is_sharpe, oos_sharpe = in_sample.get("sharpe_ratio") or 0.0, out_of_sample.get("sharpe_ratio") or 0.0
    if (out_of_sample.get("total_trades") or 0) < MIN_TRADES:
        flags.append(f"out-of-sample trades {out_of_sample.get('total_trades') or 0} < {MIN_TRADES}")
    if (out_of_sample.get("total_return") or 0.0) <= 0:
        flags.append("out-of-sample return is not positive")
    if is_sharpe > 0 and oos_sharpe < OOS_SHARPE_RETENTION * is_sharpe:
        flags.append(f"out-of-sample Sharpe {oos_sharpe:.2f} kept less than {OOS_SHARPE_RETENTION:.0%} of in-sample {is_sharpe:.2f}")
    return flags


class ResearchLoop:
    def __init__(self, gateway: Gateway, client: ResearchApiClient, reports_dir: Path = REPORTS_DIR) -> None:
        self.gateway = gateway
        self.client = client
        self.reports_dir = reports_dir

    async def _draft(self, spec: ResearchSpec, report: ResearchReport) -> tuple[StrategyDraft, dict[str, Any]] | None:
        prompt = (f"Trading idea: {spec.idea}\nInstrument: {spec.instrument_id}, timeframe {spec.timeframe}.\n"
                  "Write the strategy.")
        for attempt in range(MAX_DRAFT_ATTEMPTS):
            result = await DRAFTER.run(self.gateway, prompt)
            report.cost_usd += float(result.cost_usd)
            draft: StrategyDraft = result.output
            try:
                # The API validates the code (allow-list, imports) and returns the violations; the
                # baseline backtest then loads and runs it in the sandbox with default parameters
                saved = await self.client.save_strategy(draft.name, draft.description, draft.python_code)
                report.baseline = await self.client.backtest(
                    saved["slug"], spec.instrument_id, spec.bar_type, spec.timeframe, {}, spec.in_sample
                )
            except ResearchApiError as e:
                prompt += f"\n\nAttempt {attempt + 1} failed: {e}\nFix it and return the full strategy again."
                continue
            return draft, saved
        return None

    async def _optimize(self, spec: ResearchSpec, draft: StrategyDraft, slug: str, report: ResearchReport) -> tuple[dict[str, Any], dict[str, Any]]:
        study = optuna.create_study(direction="maximize", sampler=optuna.samplers.TPESampler(seed=spec.seed))
        best: tuple[float, dict[str, Any], dict[str, Any]] | None = None
        trials = spec.n_trials if draft.parameter_space else 1
        for _ in range(trials):
            trial = study.ask()
            params = {
                p.name: (trial.suggest_int(p.name, int(p.low), int(p.high)) if p.kind == "int"
                         else trial.suggest_float(p.name, p.low, p.high))
                for p in draft.parameter_space if p.high > p.low
            }
            try:
                metrics = await self.client.backtest(slug, spec.instrument_id, spec.bar_type, spec.timeframe, params, spec.in_sample)
                value = _score(metrics)
            except ResearchApiError as e:  # an invalid parameter combination is a bad trial, not a crash
                metrics, value = {"error": str(e)}, -100.0
            study.tell(trial, value)
            report.trials.append({"parameters": params, "score": value, "run_id": metrics.get("run_id")})
            if "error" not in metrics and (best is None or value > best[0]):
                best = (value, params, metrics)
        if best is None:
            raise ResearchApiError("no in-sample backtest completed")
        return best[1], best[2]

    async def run(self, spec: ResearchSpec) -> ResearchReport:
        report = ResearchReport(id=str(uuid.uuid4()), idea=spec.idea, status="FAILED", reason="")
        try:
            drafted = await self._draft(spec, report)
            if drafted is None:
                report.reason = f"no valid draft after {MAX_DRAFT_ATTEMPTS} attempts"
                return await self._save(report)
            draft, saved = drafted
            report.strategy_slug, report.strategy_name = saved["slug"], draft.name

            report.best_parameters, report.in_sample = await self._optimize(spec, draft, saved["slug"], report)
            report.out_of_sample = await self.client.backtest(
                saved["slug"], spec.instrument_id, spec.bar_type, spec.timeframe, report.best_parameters, spec.out_of_sample
            )
            report.overfitting_flags = overfitting_flags(report.in_sample, report.out_of_sample)
            if report.overfitting_flags:
                report.status, report.reason = "REJECT", "overfitting guard: " + "; ".join(report.overfitting_flags)
                return await self._save(report)

            review = await REVIEWER.run(self.gateway, self._review_prompt(draft, report))
            report.cost_usd += float(review.cost_usd)
            report.review = review.output.model_dump()
            report.status, report.reason = review.output.verdict, review.output.summary
        except (GatewayError, BudgetExceeded, ResearchApiError) as e:
            report.status, report.reason = "FAILED", str(e)
        return await self._save(report)

    @staticmethod
    def _review_prompt(draft: StrategyDraft, report: ResearchReport) -> str:
        keep = ("total_return", "sharpe_ratio", "sortino_ratio", "max_drawdown", "win_rate", "profit_factor", "total_trades")
        fmt = lambda m: {k: m.get(k) for k in keep}  # noqa: E731
        return (f"Strategy: {draft.name}\n{draft.description}\n\nCode:\n{draft.python_code}\n\n"
                f"Best parameters (in-sample optimized): {json.dumps(report.best_parameters)}\n"
                f"In-sample: {json.dumps(fmt(report.in_sample))}\nOut-of-sample: {json.dumps(fmt(report.out_of_sample))}\n"
                f"Trials run: {len(report.trials)}")

    async def _save(self, report: ResearchReport) -> ResearchReport:
        try:
            self.reports_dir.mkdir(parents=True, exist_ok=True)
            (self.reports_dir / f"{report.id}.json").write_text(json.dumps(asdict(report), indent=2, default=str))
        except OSError as e:
            logger.error("Could not write research report file %s: %s", report.id, e)
        try:  # queryable history (LE-6); the JSON file stays as the artifact copy
            await self.client.save_report(json.loads(json.dumps(asdict(report), default=str)))
        except Exception as e:
            logger.warning("Research report %s not stored in the API: %s", report.id, e)
        logger.info("Research %s: %s (%s)", report.id, report.status, report.reason[:200])
        return report
