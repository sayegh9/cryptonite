"""Closed strategy research loop (AI-first blueprint Phase 4, A4.1–A4.5).

history guard -> idea -> draft (Flash, structured) -> save via API (validated there) -> baseline backtest
     -> optimize numeric parameters on 4 anchored walk-forward folds of the in-sample window (Optuna;
        objective mean fold Sharpe - 0.5 std; the model never guesses numbers)
     -> re-run the best parameters on the whole in-sample window and out of sample -> overfitting guard
     -> Deflated Sharpe Ratio over every trial tested on the instrument/timeframe -> robustness (+-10%)
     -> Pro review (survivors only) -> holdout, scored once, for PROMOTE only
     -> ResearchReport (JSON artifact + research_reports row)

Every backtest runs in the backtester's sandbox; the loop only talks to the API. The final holdout
window is never seen by optimization, the out-of-sample check or the review (remediation Phase 5).
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
from cryptonite_agents.research.validation import (
    DAY_NS,
    InsufficientHistory,
    anchored_folds,
    check_history,
    deflated_sharpe_ratio,
    fold_objective,
    perturbations,
)
from cryptonite_schemas.research import StrategyDraft, StrategyReview

logger = logging.getLogger("cryptonite.agents.research")

REPORTS_DIR = Path(os.getenv("RESEARCH_REPORTS_PATH", "/app/data/research_reports"))
MAX_DRAFT_ATTEMPTS = 3
MIN_TRADES = 30  # per full window (the in-sample folds together need their share of it)
OOS_SHARPE_RETENTION = 0.5  # out-of-sample Sharpe must keep at least half of the in-sample Sharpe
DSR_MIN = 0.95  # Deflated Sharpe Ratio below this: the best trial is what that many tries give by chance
ROBUSTNESS_MIN_RETENTION = 0.5  # a +-10% parameter move keeping less than half the Sharpe is fragile
TRIALS_LOOKBACK_DAYS = 90  # earlier research on the same instrument/timeframe counts toward the DSR trials

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
    holdout: tuple[int, int, int] | None = None  # final window, scored once for PROMOTE candidates only


def spec_to_payload(spec: ResearchSpec) -> dict[str, Any]:
    """A spec as JSON for an approval payload (the windows are the ones the operator approved)."""
    return asdict(spec)


def spec_from_payload(data: dict[str, Any]) -> ResearchSpec:
    kwargs = dict(data)
    for key in ("in_sample", "out_of_sample", "holdout"):
        if kwargs.get(key) is not None:
            kwargs[key] = tuple(kwargs[key])
    return ResearchSpec(**kwargs)


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
    instrument_id: str | None = None
    timeframe: str | None = None
    folds: list[dict[str, Any]] = field(default_factory=list)  # best parameters on each walk-forward fold
    dsr: float | None = None
    trials_tested: int = 0  # trials behind the DSR: this report's plus earlier ones on the instrument/timeframe
    robustness: dict[str, Any] = field(default_factory=dict)
    holdout: dict[str, Any] = field(default_factory=dict)
    cost_usd: float = 0.0
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())



def _fold_score(folds: list[dict[str, Any]]) -> tuple[float, float | None]:
    """(objective, mean fold Sharpe) of one trial; a missing Sharpe or too few trades scores a failure."""
    sharpes = [f.get("sharpe_ratio") for f in folds]
    if any(s is None or (isinstance(s, float) and math.isnan(s)) for s in sharpes):
        return -10.0, None
    values = [float(s) for s in sharpes]
    trades = sum(f.get("total_trades") or 0 for f in folds)
    if trades < MIN_TRADES * len(folds) / (len(folds) + 1):  # the folds cover 4/5 of the window
        return -10.0 + trades / 100, sum(values) / len(values)
    return fold_objective(values), sum(values) / len(values)


def holdout_failure(holdout: dict[str, Any]) -> str | None:
    if (holdout.get("total_return") or 0.0) <= 0:
        return "holdout return is not positive"
    if (holdout.get("sharpe_ratio") or 0.0) <= 0:
        return "holdout Sharpe is not positive"
    return None


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

    async def _draft(self, spec: ResearchSpec, report: ResearchReport) -> StrategyDraft | None:
        """The drafter's strategy, checked by the API's code validator but not saved or backtested: the
        operator approves a draft before anything runs on it. Violations go back to the drafter."""
        prompt = (f"Trading idea: {spec.idea}\nInstrument: {spec.instrument_id}, timeframe {spec.timeframe}.\n"
                  "Write the strategy.")
        for attempt in range(MAX_DRAFT_ATTEMPTS):
            result = await DRAFTER.run(self.gateway, prompt)
            report.cost_usd += float(result.cost_usd)
            draft: StrategyDraft = result.output
            violations = await self.client.validate_strategy(draft.python_code)
            if violations:
                prompt += f"\n\nAttempt {attempt + 1} failed: {'; '.join(violations)}\nFix it and return the full strategy again."
                continue
            return draft
        return None

    async def _optimize(self, spec: ResearchSpec, draft: StrategyDraft, slug: str,
                        report: ResearchReport) -> tuple[dict[str, Any], dict[str, Any]]:
        """Optuna over 4 anchored walk-forward folds of the in-sample window (R5.1); the best parameters
        are then run once on the whole in-sample window."""
        folds = anchored_folds(spec.in_sample)
        study = optuna.create_study(direction="maximize", sampler=optuna.samplers.TPESampler(seed=spec.seed))
        best: tuple[float, dict[str, Any], list[dict[str, Any]]] | None = None
        trials = spec.n_trials if draft.parameter_space else 1
        for _ in range(trials):
            trial = study.ask()
            params = {
                p.name: (trial.suggest_int(p.name, int(p.low), int(p.high)) if p.kind == "int"
                         else trial.suggest_float(p.name, p.low, p.high))
                for p in draft.parameter_space if p.high > p.low
            }
            try:
                fold_metrics = [await self.client.backtest(slug, spec.instrument_id, spec.bar_type, spec.timeframe,
                                                           params, window) for window in folds]
                value, sharpe = _fold_score(fold_metrics)
            except ResearchApiError as e:  # an invalid parameter combination is a bad trial, not a crash
                fold_metrics, value, sharpe = [{"error": str(e)}], -100.0, None
            study.tell(trial, value)
            report.trials.append({"parameters": params, "score": value, "sharpe": sharpe,
                                  "fold_sharpes": [m.get("sharpe_ratio") for m in fold_metrics],
                                  "run_ids": [m.get("run_id") for m in fold_metrics]})
            if not any("error" in m for m in fold_metrics) and (best is None or value > best[0]):
                best = (value, params, fold_metrics)
        if best is None:
            raise ResearchApiError("no in-sample backtest completed")
        report.folds = [{"window": list(w), **m} for w, m in zip(folds, best[2], strict=True)]
        full = await self.client.backtest(slug, spec.instrument_id, spec.bar_type, spec.timeframe, best[1], spec.in_sample)
        return best[1], full

    async def _history_guard(self, spec: ResearchSpec) -> None:
        """R5.0: refuse before spending anything when the windows need more history than the catalog has."""
        try:
            coverage = await self.client.coverage(spec.bar_type)
        except ResearchApiError as e:  # coverage unknown: the backtests still check their own cadence
            logger.warning("History coverage unknown for %s: %s", spec.bar_type, e)
            return
        check_history([spec.in_sample, spec.out_of_sample, *([spec.holdout] if spec.holdout else [])], coverage)

    async def _deflated_sharpe(self, spec: ResearchSpec, report: ResearchReport) -> None:
        """R5.2/R5.4: DSR of the selected trial over every trial tested on this instrument/timeframe."""
        try:
            earlier = await self.client.trials_tested(spec.instrument_id, spec.timeframe, TRIALS_LOOKBACK_DAYS)
        except ResearchApiError as e:
            logger.warning("Earlier trial count unavailable (%s); the DSR counts this report's trials only", e)
            earlier = 0
        report.trials_tested = earlier + len(report.trials)
        best = max(report.trials, key=lambda t: t["score"]).get("sharpe")
        sharpes = [t["sharpe"] for t in report.trials if t.get("sharpe") is not None]
        n_obs = int(sum(w[2] - w[1] for w in anchored_folds(spec.in_sample)) // DAY_NS)  # days the folds score
        if best is not None:
            report.dsr = deflated_sharpe_ratio(best, sharpes, report.trials_tested, n_obs)

    async def _robustness(self, spec: ResearchSpec, draft: StrategyDraft, slug: str, report: ResearchReport) -> None:
        """R5.5: each best parameter +-10%, whole in-sample window; fragile when the Sharpe halves."""
        base = report.in_sample.get("sharpe_ratio")
        results = []
        for name, factor, params in perturbations(report.best_parameters, draft.parameter_space):
            row = {"parameter": name, "factor": round(factor, 2), "value": params[name]}
            try:
                m = await self.client.backtest(slug, spec.instrument_id, spec.bar_type, spec.timeframe, params, spec.in_sample)
                results.append({**row, "sharpe_ratio": m.get("sharpe_ratio")})
            except ResearchApiError as e:
                results.append({**row, "error": str(e)})
        ratios = [(r.get("sharpe_ratio") or 0.0) / base for r in results if base and base > 0 and "error" not in r]
        worst = min(ratios) if ratios else None
        report.robustness = {"base_sharpe": base, "results": results,
                             "worst_retention": round(worst, 4) if worst is not None else None,
                             "fragile": worst is not None and worst < ROBUSTNESS_MIN_RETENTION}

    @staticmethod
    def _new_report(spec: ResearchSpec, report_id: str | None = None) -> ResearchReport:
        return ResearchReport(id=report_id or str(uuid.uuid4()), idea=spec.idea, status="FAILED", reason="",
                              instrument_id=spec.instrument_id, timeframe=spec.timeframe)

    async def run(self, spec: ResearchSpec) -> ResearchReport:
        """Draft the strategy and ask the operator to approve it. Nothing is saved or backtested here: the
        report waits as PENDING_APPROVAL until the approval is granted (``resume``)."""
        report = self._new_report(spec)
        try:
            await self._history_guard(spec)
            draft = await self._draft(spec, report)
            if draft is None:
                report.reason = f"no valid draft after {MAX_DRAFT_ATTEMPTS} attempts"
                return await self._save(report)
            report.strategy_name = draft.name
            report.status = "PENDING_APPROVAL"
            approval_id = await self.client.request_approval(
                "DRAFT_STRATEGY", f"Draft '{draft.name}' for: {spec.idea[:300]}",
                {"report_id": report.id, "spec": spec_to_payload(spec), "draft": draft.model_dump()})
            report.reason = f"waiting for the operator to approve the draft (approval {approval_id})"
        except InsufficientHistory as e:
            report.status, report.reason = "FAILED", str(e)
        except (GatewayError, BudgetExceeded, ResearchApiError) as e:
            report.status, report.reason = "FAILED", str(e)
        return await self._save(report)

    async def resume(self, approval: dict[str, Any]) -> ResearchReport:
        """The operator approved the draft: save it, run its baseline and the whole evaluation. Same report id
        as the pending one, so the stored report is finished in place."""
        payload = approval["payload"]
        spec = spec_from_payload(payload["spec"])
        draft = StrategyDraft.model_validate(payload["draft"])
        report = self._new_report(spec, report_id=str(payload["report_id"]))
        try:
            await self._history_guard(spec)
            saved = await self.client.save_strategy(draft.name, draft.description, draft.python_code)
            report.strategy_slug, report.strategy_name = saved["slug"], draft.name
            report.baseline = await self.client.backtest(
                saved["slug"], spec.instrument_id, spec.bar_type, spec.timeframe, {}, spec.in_sample
            )
            await self._evaluate(spec, draft, saved, report)
        except InsufficientHistory as e:
            report.status, report.reason = "FAILED", str(e)
        except (GatewayError, BudgetExceeded, ResearchApiError) as e:
            report.status, report.reason = "FAILED", str(e)
        return await self._save(report)

    async def _evaluate(self, spec: ResearchSpec, draft: StrategyDraft, saved: dict[str, Any],
                        report: ResearchReport) -> None:
        """Optimization, out-of-sample and the guards, review, holdout. Sets the report's status and reason;
        the caller saves it."""

        report.best_parameters, report.in_sample = await self._optimize(spec, draft, saved["slug"], report)
        report.out_of_sample = await self.client.backtest(
            saved["slug"], spec.instrument_id, spec.bar_type, spec.timeframe, report.best_parameters, spec.out_of_sample
        )
        report.overfitting_flags = overfitting_flags(report.in_sample, report.out_of_sample)
        if report.overfitting_flags:
            report.status, report.reason = "REJECT", "overfitting guard: " + "; ".join(report.overfitting_flags)
            return

        await self._deflated_sharpe(spec, report)
        if report.dsr is not None and report.dsr < DSR_MIN:
            report.status = "REJECT"
            report.reason = (f"deflated Sharpe ratio {report.dsr:.2f} < {DSR_MIN} over {report.trials_tested} "
                             "trials: the best result is what that many tries give by chance")
            return

        await self._robustness(spec, draft, saved["slug"], report)

        review = await REVIEWER.run(self.gateway, self._review_prompt(draft, report))
        report.cost_usd += float(review.cost_usd)
        report.review = review.output.model_dump()
        report.status, report.reason = review.output.verdict, review.output.summary

        if report.status == "PROMOTE" and spec.holdout:  # scored once, never fed back into anything
            report.holdout = await self.client.backtest(
                saved["slug"], spec.instrument_id, spec.bar_type, spec.timeframe, report.best_parameters, spec.holdout
            )
            failure = holdout_failure(report.holdout)
            if failure:
                report.status, report.reason = "REJECT", f"holdout: {failure} (review: {report.reason})"

    @staticmethod
    def _review_prompt(draft: StrategyDraft, report: ResearchReport) -> str:
        keep = ("total_return", "sharpe_ratio", "sortino_ratio", "max_drawdown", "win_rate", "profit_factor", "total_trades")
        fmt = lambda m: {k: m.get(k) for k in keep}  # noqa: E731
        return (f"Strategy: {draft.name}\n{draft.description}\n\nCode:\n{draft.python_code}\n\n"
                f"Best parameters (in-sample optimized): {json.dumps(report.best_parameters)}\n"
                f"In-sample: {json.dumps(fmt(report.in_sample))}\nOut-of-sample: {json.dumps(fmt(report.out_of_sample))}\n"
                f"Walk-forward fold Sharpes: {[f.get('sharpe_ratio') for f in report.folds]}\n"
                f"Trials run: {len(report.trials)} (deflated Sharpe ratio {report.dsr} over {report.trials_tested} trials)\n"
                f"Robustness, each parameter +-10%: worst Sharpe retention {report.robustness.get('worst_retention')}"
                f"{' (FRAGILE)' if report.robustness.get('fragile') else ''}")

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
