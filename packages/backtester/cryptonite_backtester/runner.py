"""Institutional quantitative backtest runner using NautilusTrader BacktestNode."""

import hashlib
import inspect
import json
import logging
import math
import os
from pathlib import Path
from typing import Any
from uuid import UUID

import nautilus_trader
import pyarrow as pa
import pyarrow.parquet as pq
from cryptonite_storage.nautilus_catalog import NautilusCatalogManager
from cryptonite_strategies.registry import (
    STRATEGY_REGISTRY,
    get_source_tree_hash,
    get_strategy_class,
    get_strategy_config_class,
    get_strategy_meta,
)
from nautilus_trader.backtest import (
    BacktestDataConfig,
    BacktestEngineConfig,
    BacktestNode,
    BacktestRunConfig,
)
from nautilus_trader.model import (
    BarAggregation,
    BarSpecification,
    InstrumentId,
    PriceType,
    Venue,
)
from pydantic import BaseModel, Field

from cryptonite_backtester.cadence import (
    validate_bar_cadence,
)
from cryptonite_backtester.execution_profiles import resolve_execution_profile
from cryptonite_backtester.metrics import (
    METRICS_VERSION,
    benchmark_metrics,
    calculate_performance_metrics,
    fees_and_turnover,
    marked_equity_series,
    regime_breakdown,
    round_trips,
)
from cryptonite_backtester.storage import BacktestArtifactStore

logger = logging.getLogger("cryptonite.backtester.runner")


SANDBOX_ENV_FLAG = "CRYPTONITE_STRATEGY_SANDBOX"
QUOTE_CURRENCY = "USDT"  # the simulated cash account is funded and valued in USDT
_NON_PARAMETERS = {"self", "instrument_id", "bar_type", "evaluation_start_ns", "kwargs"}


def _commissions(order: Any) -> list[tuple[float, str]]:
    """(amount, currency) of every commission an order paid (Nautilus keys them by currency)."""
    raw = order.commissions()
    money = raw.values() if isinstance(raw, dict) else raw
    return [(float(m.as_double()), str(m.currency)) for m in money]


def resolve_strategy(request: "BacktestRunRequest") -> tuple[type, type, dict[str, Any]]:
    """(strategy class, config class, default parameters) for a run.

    Registry strategies load in-process. Model-written code (``python_code`` for a slug outside the
    registry) is validated and executed with restricted builtins, and only inside the sandbox
    subprocess (``cryptonite_backtester.sandbox``), never in a process that holds secrets.
    """
    if request.strategy_slug in STRATEGY_REGISTRY:
        meta = get_strategy_meta(request.strategy_slug)
        return (get_strategy_class(request.strategy_slug), get_strategy_config_class(request.strategy_slug),
                dict(meta["default_parameters"]))
    if not request.python_code:
        raise KeyError(f"Strategy '{request.strategy_slug}' is neither registered nor has source code")
    if os.environ.get(SANDBOX_ENV_FLAG) != "1":
        raise RuntimeError("model-written strategies only run in the backtest sandbox subprocess")
    from cryptonite_strategies.validation import safe_exec

    strat_cls, cfg_cls = safe_exec(request.python_code, module_name=f"ai_{request.strategy_slug.replace('-', '_')}")
    defaults = {
        name: p.default
        for name, p in inspect.signature(cfg_cls.__init__).parameters.items()
        if name not in _NON_PARAMETERS and p.default is not inspect.Parameter.empty
    }
    return strat_cls, cfg_cls, defaults


class BacktestRunRequest(BaseModel):
    """Parameters required to execute a deterministic backtest run."""

    run_id: str | UUID
    strategy_slug: str
    strategy_version_id: str | UUID
    instrument_id: str
    bar_type: str
    timeframe: str = "1m"
    parameters: dict[str, Any] = Field(default_factory=dict)
    warmup_start_ns: int
    evaluation_start_ns: int
    evaluation_end_ns: int
    execution_profile: str = "conservative"  # fees + slippage; "optimistic" has no costs, not for decisions
    initial_balance: float = 10000.0
    random_seed: int = 42
    attempt_id: str | UUID | None = None
    run_config_hash: str | None = None
    python_code: str | None = None


def compute_run_config_hash(
    request: BacktestRunRequest,
    resolved_profile_meta: dict[str, Any],
) -> str:
    """Compute deterministic SHA-256 hash across all configuration parameters and code tree."""
    payload = {
        "strategy_slug": request.strategy_slug,
        "strategy_version_id": str(request.strategy_version_id),
        "instrument_id": request.instrument_id,
        "bar_type": request.bar_type,
        "timeframe": request.timeframe,
        "parameters": request.parameters,
        "warmup_start_ns": request.warmup_start_ns,
        "evaluation_start_ns": request.evaluation_start_ns,
        "evaluation_end_ns": request.evaluation_end_ns,
        "execution_profile": request.execution_profile,
        "resolved_profile_meta": resolved_profile_meta,
        "source_tree_hash": get_source_tree_hash(),
        "nautilus_version": nautilus_trader.__version__,
    }
    canonical_json = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


class BacktestRunner:
    """Stateless NautilusTrader execution wrapper executing BacktestNode for a single backtest request."""

    def __init__(
        self,
        catalog_path: str | Path = "data/nautilus_catalog",
        artifacts_path: str | Path = "data/backtest_artifacts",
    ) -> None:
        self.catalog_path = Path(catalog_path).resolve()
        self.artifacts_path = Path(artifacts_path).resolve()
        self.catalog_manager = NautilusCatalogManager(self.catalog_path)
        self.artifact_store = BacktestArtifactStore(self.artifacts_path)

    def compute_run_config_hash(
        self,
        request: BacktestRunRequest,
        resolved_profile_meta: dict[str, Any],
    ) -> str:
        """Compute deterministic SHA-256 hash across all configuration parameters and code tree."""
        return compute_run_config_hash(request, resolved_profile_meta)

    def build_catalog_manifest(
        self,
        bar_type_str: str,
        start_ns: int,
        end_ns: int,
    ) -> list[dict[str, Any]]:
        """Identify catalog Parquet files covering the run range and record their SHA-256 checksums."""
        catalog = self.catalog_manager.raw_catalog
        files = catalog.query_files(
            "bars",
            identifiers=[bar_type_str],
            start=start_ns,
            end=end_ns,
        )

        manifest = []
        for file_path_str in sorted(files):
            fp = Path(file_path_str)
            if not fp.is_absolute():
                fp = self.catalog_path / fp
            if not fp.is_file():
                continue
            hasher = hashlib.sha256()
            with open(fp, "rb") as fh:
                while chunk := fh.read(65536):
                    hasher.update(chunk)
            try:
                rel_path = str(fp.relative_to(self.catalog_path))
            except ValueError:
                rel_path = fp.name
            manifest.append({
                "path": rel_path,
                "size_bytes": fp.stat().st_size,
                "sha256": hasher.hexdigest(),
            })
        return manifest

    def query_catalog_timestamps(
        self,
        bar_type_str: str,
        start_ns: int,
        end_ns: int,
    ):
        """Query strictly ts_event timestamps from catalog parquet files with minimal memory footprint, returning a generator."""
        catalog = self.catalog_manager.raw_catalog
        files = catalog.query_files(
            "bars",
            identifiers=[bar_type_str],
            start=start_ns,
            end=end_ns,
        )
        if not files:
            bars = self.catalog_manager.query_bars(
                identifiers=[bar_type_str],
                start=start_ns,
                end=end_ns,
            )
            all_ts = set()
            for b in bars:
                all_ts.add(b.ts_event)
            for ts in sorted(list(all_ts)):
                yield ts
            return

        # Collect all timestamps to handle overlaps/duplicates across parquet files,
        # since nautilus_trader does this automatically at runtime but our raw reader doesn't.
        all_ts = set()
        for file_path_str in sorted(files):
            fp = Path(file_path_str)
            if not fp.is_absolute():
                fp = self.catalog_path / fp
            if not fp.is_file():
                continue
            parquet_file = pq.ParquetFile(fp)
            for batch in parquet_file.iter_batches(columns=["ts_event"]):
                for ts in batch["ts_event"].to_pylist():
                    if start_ns <= ts <= end_ns:
                        all_ts.add(ts)

        for ts in sorted(list(all_ts)):
            yield ts

    def query_catalog_ohlc(self, bar_type_str: str, start_ns: int, end_ns: int) -> list[tuple[int, float, float, float, float]]:
        """(ts_event, open, high, low, close) of every bar in [start_ns, end_ns], one per timestamp: the
        closes mark equity to market, the full bars feed the regime breakdown (F1)."""
        bars: dict[int, tuple[float, float, float, float]] = {}
        for b in self.catalog_manager.query_bars(identifiers=[bar_type_str], start=start_ns, end=end_ns):
            ts = int(b.ts_event)
            if start_ns <= ts <= end_ns:
                bars[ts] = (float(b.open), float(b.high), float(b.low), float(b.close))
        return [(ts, *v) for ts, v in sorted(bars.items())]

    def query_catalog_closes(self, bar_type_str: str, start_ns: int, end_ns: int) -> list[tuple[int, float]]:
        """(ts_event, close) of every bar in [start_ns, end_ns], one per timestamp, for mark-to-market."""
        return [(ts, c) for ts, _, _, _, c in self.query_catalog_ohlc(bar_type_str, start_ns, end_ns)]

    def run(self, request: BacktestRunRequest) -> dict[str, Any]:
        """Execute backtest with BacktestNode and return the complete result record."""
        run_id_str = str(request.run_id)
        logger.info(
            "Running backtest %s (strategy=%s, instrument=%s, profile=%s)",
            run_id_str,
            request.strategy_slug,
            request.instrument_id,
            request.execution_profile,
        )

        # 1. Validation of time bounds
        if request.warmup_start_ns >= request.evaluation_start_ns:
            raise ValueError(
                f"warmup_start_ns ({request.warmup_start_ns}) must be strictly less than "
                f"evaluation_start_ns ({request.evaluation_start_ns})"
            )
        if request.evaluation_start_ns >= request.evaluation_end_ns:
            raise ValueError(
                f"evaluation_start_ns ({request.evaluation_start_ns}) must be strictly less than "
                f"evaluation_end_ns ({request.evaluation_end_ns})"
            )

        # Validate canonical external bar type
        if not request.bar_type.endswith("-LAST-EXTERNAL"):
            raise ValueError(
                f"Invalid bar_type '{request.bar_type}': Milestone 3 requires canonical '...-LAST-EXTERNAL' "
                f"external bars for simulated L1 execution matching."
            )

        # 2. Query and validate catalog bar timestamps (projection-only for constant memory)
        catalog_timestamps = self.query_catalog_timestamps(
            bar_type_str=request.bar_type,
            start_ns=request.warmup_start_ns,
            end_ns=request.evaluation_end_ns,
        )
        validate_bar_cadence(
            bars=catalog_timestamps,
            timeframe=request.timeframe,
            warmup_start_ns=request.warmup_start_ns,
            evaluation_start_ns=request.evaluation_start_ns,
            evaluation_end_ns=request.evaluation_end_ns,
        )


        # 3. Catalog manifest
        catalog_manifest = self.build_catalog_manifest(
            bar_type_str=request.bar_type,
            start_ns=request.warmup_start_ns,
            end_ns=request.evaluation_end_ns,
        )

        # 4. Resolve execution profile & venue configuration
        venue_cfg, profile_meta = resolve_execution_profile(
            profile_name=request.execution_profile,
            venue_name="BINANCE",
            starting_balance=f"{request.initial_balance:.2f} {QUOTE_CURRENCY}",
            random_seed=request.random_seed,
        )

        # 5. Compute run_config_hash
        run_config_hash = self.compute_run_config_hash(request, profile_meta)
        if (
            request.run_config_hash
            and not request.run_config_hash.startswith("hash-")
            and request.run_config_hash != run_config_hash
        ):
            raise ValueError(
                f"Immutable run_config_hash mismatch: expected {request.run_config_hash}, "
                f"computed {run_config_hash}"
            )

        # 6. Configure BacktestDataConfig with canonical bar filter & time bounds
        inst_id = InstrumentId.from_str(request.instrument_id)
        if request.timeframe == "1m":
            bar_spec = BarSpecification(1, BarAggregation.MINUTE, PriceType.LAST)
        elif request.timeframe == "1h":
            bar_spec = BarSpecification(1, BarAggregation.HOUR, PriceType.LAST)
        else:
            bar_spec = BarSpecification(1, BarAggregation.MINUTE, PriceType.LAST)

        data_cfg = BacktestDataConfig(
            catalog_path=str(self.catalog_path),
            data_type="Bar",
            instrument_id=inst_id,
            bar_spec=bar_spec,
            bar_types=[request.bar_type],
            start_time=request.warmup_start_ns,
            end_time=request.evaluation_end_ns,
            optimize_file_loading=True,
        )

        run_cfg = BacktestRunConfig(
            id=run_id_str,
            engine=BacktestEngineConfig(),
            venues=[venue_cfg],
            data=[data_cfg],
            chunk_size=100_000,
            dispose_on_completion=False,
        )

        node = BacktestNode(configs=[run_cfg])
        node.build()

        # 7. Instantiate Strategy with merged parameters
        strat_cls, cfg_cls, defaults = resolve_strategy(request)
        merged_params = dict(defaults)
        merged_params.update(request.parameters)

        strat_cfg = cfg_cls(
            instrument_id=request.instrument_id,
            bar_type=request.bar_type,
            evaluation_start_ns=request.evaluation_start_ns,
            **merged_params,
        )

        strategy = strat_cls(config=strat_cfg)
        node.add_strategy(run_cfg.id, strategy)

        # 8. Run simulation & extract outputs safely before disposing node
        try:
            node.run()
            try:
                acc_report = node.generate_account_report(run_cfg.id, venue=Venue("BINANCE"))
            except Exception:
                acc_report = None
            cache = node.get_engine_cache(run_cfg.id)
            positions = list(cache.positions())
            orders = list(cache.orders())
        finally:
            node.dispose()

        # Strict warmup enforcement: verify zero orders and zero positions prior to evaluation_start_ns
        for o in orders:
            if int(o.ts_init) < request.evaluation_start_ns:
                raise RuntimeError(
                    f"Warm-up violation: order {o.client_order_id} initialized at {o.ts_init}ns "
                    f"prior to evaluation_start_ns {request.evaluation_start_ns}ns"
                )
        for pos in positions:
            if int(pos.ts_opened) < request.evaluation_start_ns:
                raise RuntimeError(
                    f"Warm-up violation: position {pos.id} opened at {pos.ts_opened}ns "
                    f"prior to evaluation_start_ns {request.evaluation_start_ns}ns"
                )

        # 10. Extract Equity Curve series (isolated strictly to evaluation period), marked to market at
        # every bar close of the window (R2.2): holdings change only at fills, their value at every bar
        ohlc = self.query_catalog_ohlc(request.bar_type, request.evaluation_start_ns, request.evaluation_end_ns)
        closes = [(ts, c) for ts, _, _, _, c in ohlc]
        equity_series: list[tuple[int, float]] = []
        if acc_report is not None and len(acc_report) > 0:
            # one row per currency per account update: value the base asset, never add it as equity
            balances = [(int(dt.timestamp() * 1e9), str(row.get("currency", QUOTE_CURRENCY)), float(row["total"]))
                        for dt, row in acc_report.iterrows()]
            fills = [(int(o.ts_last), float(o.avg_px)) for o in orders if o.avg_px and float(o.filled_qty) > 0]
            equity_series = marked_equity_series(balances, fills, closes, quote_currency=QUOTE_CURRENCY,
                                                 start_ns=request.evaluation_start_ns,
                                                 end_ns=request.evaluation_end_ns)

        if not equity_series:
            equity_series = [
                (request.evaluation_start_ns, request.initial_balance),
                (request.evaluation_end_ns, request.initial_balance),
            ]
        elif equity_series[0][0] > request.evaluation_start_ns:
            equity_series.insert(0, (request.evaluation_start_ns, request.initial_balance))
        elif len(equity_series) == 1:
            equity_series.append((request.evaluation_end_ns, equity_series[0][1]))

        # 11. Extract Trades List: round trips rebuilt from fills. Under NETTING every round trip shares
        # one position id, so the position list held a single "trade" per run (metrics v3).
        def quote_fee(o: Any) -> float:
            px = float(o.avg_px or 0.0)
            return sum(amount if currency == QUOTE_CURRENCY else amount * px for amount, currency in _commissions(o))

        trades_list = round_trips(
            ((int(o.ts_last), "BUY" if "BUY" in str(o.side).upper() else "SELL", float(o.filled_qty),
              float(o.avg_px or 0.0), quote_fee(o)) for o in orders if float(o.filled_qty) > 0),
            instrument_id=request.instrument_id,
        )

        # 12. Calculate performance metrics
        perf_metrics = calculate_performance_metrics(
            equity_curve=equity_series,
            trades=trades_list,
            initial_balance=request.initial_balance,
        )
        fees_paid, turnover = fees_and_turnover(
            ((float(o.filled_qty), float(o.avg_px or 0.0), _commissions(o)) for o in orders if float(o.filled_qty) > 0),
            quote_currency=QUOTE_CURRENCY,
            initial_balance=request.initial_balance,
        )
        extra_metrics = {
            "metrics_version": METRICS_VERSION,
            "benchmark": benchmark_metrics(closes, request.initial_balance, perf_metrics["total_return"]),
            "fees_paid": fees_paid,
            "turnover": turnover,
            "regime_breakdown": regime_breakdown(trades_list, ohlc),  # F1, reporting only; None when unavailable
        }

        # 13. Build Arrow tables for artifacts
        # Equity table
        eq_ts = [ts for ts, _ in equity_series]
        eq_vals = [eq for _, eq in equity_series]
        # Calculate running drawdown for each equity point
        pk = -math.inf
        eq_dds = []
        for eq in eq_vals:
            if eq > pk:
                pk = eq
            eq_dds.append((pk - eq) / pk if pk > 0 else 0.0)

        equity_table = pa.Table.from_arrays(
            [
                pa.array(eq_ts, pa.int64()),
                pa.array(eq_vals, pa.float64()),
                pa.array(eq_dds, pa.float64()),
            ],
            names=["ts_event_ns", "equity", "drawdown"],
        )

        # Trades table
        trades_table = pa.Table.from_arrays(
            [
                pa.array([t["position_id"] for t in trades_list], pa.string()),
                pa.array([t["instrument_id"] for t in trades_list], pa.string()),
                pa.array([t["side"] for t in trades_list], pa.string()),
                pa.array([t["ts_opened_ns"] for t in trades_list], pa.int64()),
                pa.array([t["ts_closed_ns"] for t in trades_list], pa.int64()),
                pa.array([t["entry_price"] for t in trades_list], pa.float64()),
                pa.array([t["exit_price"] for t in trades_list], pa.float64()),
                pa.array([t["quantity"] for t in trades_list], pa.float64()),
                pa.array([t["pnl"] for t in trades_list], pa.float64()),
                pa.array([t["realized_return"] for t in trades_list], pa.float64()),
            ],
            names=[
                "position_id",
                "instrument_id",
                "side",
                "ts_opened_ns",
                "ts_closed_ns",
                "entry_price",
                "exit_price",
                "quantity",
                "realized_pnl",
                "realized_return",
            ],
        )

        # Orders table
        orders_data = []
        for o in orders:
            orders_data.append({
                "client_order_id": str(o.client_order_id),
                "venue_order_id": str(o.venue_order_id) if o.venue_order_id else "",
                "instrument_id": str(o.instrument_id),
                "side": str(o.side),
                "order_type": str(o.order_type),
                "status": str(o.status),
                "ts_init_ns": int(o.ts_init),
                "ts_closed_ns": int(o.ts_closed or 0),
                "price": float(getattr(o, "price", 0.0) or 0.0),
                "quantity": float(o.quantity),
                "filled_qty": float(o.filled_qty),
                "avg_px": float(o.avg_px or 0.0),
            })

        orders_table = pa.Table.from_arrays(
            [
                pa.array([o["client_order_id"] for o in orders_data], pa.string()),
                pa.array([o["venue_order_id"] for o in orders_data], pa.string()),
                pa.array([o["instrument_id"] for o in orders_data], pa.string()),
                pa.array([o["side"] for o in orders_data], pa.string()),
                pa.array([o["order_type"] for o in orders_data], pa.string()),
                pa.array([o["status"] for o in orders_data], pa.string()),
                pa.array([o["ts_init_ns"] for o in orders_data], pa.int64()),
                pa.array([o["ts_closed_ns"] for o in orders_data], pa.int64()),
                pa.array([o["price"] for o in orders_data], pa.float64()),
                pa.array([o["quantity"] for o in orders_data], pa.float64()),
                pa.array([o["filled_qty"] for o in orders_data], pa.float64()),
                pa.array([o["avg_px"] for o in orders_data], pa.float64()),
            ],
            names=[
                "client_order_id",
                "venue_order_id",
                "instrument_id",
                "side",
                "order_type",
                "status",
                "ts_init_ns",
                "ts_closed_ns",
                "price",
                "quantity",
                "filled_qty",
                "avg_px",
            ],
        )

        # 14. Persist Parquet artifacts and manifest.json
        manifest_meta = {
            "strategy_slug": request.strategy_slug,
            "strategy_version_id": str(request.strategy_version_id),
            "instrument_id": request.instrument_id,
            "bar_type": request.bar_type,
            "timeframe": request.timeframe,
            "parameters": request.parameters,
            "warmup_start_ns": request.warmup_start_ns,
            "evaluation_start_ns": request.evaluation_start_ns,
            "evaluation_end_ns": request.evaluation_end_ns,
            "execution_profile": request.execution_profile,
            "run_config_hash": run_config_hash,
            "nautilus_version": nautilus_trader.__version__,
            "source_tree_hash": get_source_tree_hash(),
            "catalog_manifest": catalog_manifest,
            **extra_metrics,  # metrics_version lives here and in the results, never in run_config_hash
            "realism_disclosure": (
                "OHLCV bar backtesting uses simulated L1 matching and cannot reconstruct "
                "order-book depth, spread variation, queue position, or exchange latency. "
                "Backtest results are hypothetical and do not represent actual live execution."
            ),
        }

        if request.attempt_id:
            artifact_manifest = self.artifact_store.persist_staged_artifacts(
                run_id=run_id_str,
                attempt_id=str(request.attempt_id),
                equity_table=equity_table,
                trades_table=trades_table,
                orders_table=orders_table,
                manifest_meta=manifest_meta,
                metrics=extra_metrics,
            )
        else:
            artifact_manifest = self.artifact_store.persist_artifacts(
                run_id=run_id_str,
                equity_table=equity_table,
                trades_table=trades_table,
                orders_table=orders_table,
                manifest_meta=manifest_meta,
                metrics=extra_metrics,
            )

        return {
            "backtest_run_id": run_id_str,
            "run_config_hash": run_config_hash,
            "total_return": perf_metrics["total_return"],
            "cagr": perf_metrics["cagr"],
            "sharpe_ratio": perf_metrics["sharpe_ratio"],
            "sortino_ratio": perf_metrics["sortino_ratio"],
            "max_drawdown": perf_metrics["max_drawdown"],
            "profit_factor": perf_metrics["profit_factor"],
            "win_rate": perf_metrics["win_rate"],
            "total_trades": perf_metrics["total_trades"],
            "winning_trades": perf_metrics["winning_trades"],
            "losing_trades": perf_metrics["losing_trades"],
            "avg_trade_pnl": perf_metrics["avg_trade_pnl"],
            "max_consecutive_losses": perf_metrics["max_consecutive_losses"],
            "downsampled_equity": perf_metrics["downsampled_equity"],
            "monthly_returns": perf_metrics["monthly_returns"],
            **extra_metrics,
            "catalog_manifest": catalog_manifest,
            "artifact_manifest": artifact_manifest,
        }
