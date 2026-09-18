"""Institutional quantitative backtest runner using NautilusTrader BacktestNode."""

import hashlib
import json
import logging
import math
from pathlib import Path
from typing import Any
from uuid import UUID

import nautilus_trader
import pyarrow as pa
import pyarrow.parquet as pq
from cryptonite_backtester.cadence import (
    DataGapError,
    InsufficientWarmupError,
    validate_bar_cadence,
)
from cryptonite_backtester.execution_profiles import resolve_execution_profile
from cryptonite_backtester.metrics import calculate_performance_metrics
from cryptonite_backtester.storage import BacktestArtifactStore
from cryptonite_schemas.identifiers import to_nautilus_bar_type
from cryptonite_storage.nautilus_catalog import NautilusCatalogManager
from cryptonite_strategies.registry import (
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
    Bar,
    BarAggregation,
    BarSpecification,
    InstrumentId,
    PriceType,
    Venue,
)
from pydantic import BaseModel, Field

logger = logging.getLogger("cryptonite.backtester.runner")


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
    execution_profile: str = "optimistic"
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
            starting_balance=f"{request.initial_balance:.2f} USDT",
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
        meta = get_strategy_meta(request.strategy_slug)
        merged_params = dict(meta["default_parameters"])
        merged_params.update(request.parameters)

        cfg_cls = get_strategy_config_class(request.strategy_slug)
        strat_cfg = cfg_cls(
            instrument_id=request.instrument_id,
            bar_type=request.bar_type,
            evaluation_start_ns=request.evaluation_start_ns,
            **merged_params,
        )

        strat_cls = get_strategy_class(request.strategy_slug)
        strategy = strat_cls(config=strat_cfg)
        node.add_strategy(run_cfg.id, strategy)

        # 8. Run simulation & extract outputs safely before disposing node
        try:
            results = node.run()
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

        # 10. Extract Equity Curve series (isolated strictly to evaluation period)
        equity_series: list[tuple[int, float]] = []
        if acc_report is not None and len(acc_report) > 0:
            for dt, row in acc_report.iterrows():
                ts_ns = int(dt.timestamp() * 1e9)
                if ts_ns >= request.evaluation_start_ns:
                    total_val = float(row["total"])
                    equity_series.append((ts_ns, total_val))

        if not equity_series:
            equity_series = [
                (request.evaluation_start_ns, request.initial_balance),
                (request.evaluation_end_ns, request.initial_balance),
            ]
        elif equity_series[0][0] > request.evaluation_start_ns:
            equity_series.insert(0, (request.evaluation_start_ns, request.initial_balance))
        elif len(equity_series) == 1:
            equity_series.append((request.evaluation_end_ns, equity_series[0][1]))

        # 11. Extract Trades List
        trades_list: list[dict[str, Any]] = []
        for pos in positions:
            pnl_val = float(pos.realized_pnl)
            trades_list.append({
                "position_id": str(pos.id),
                "instrument_id": str(pos.instrument_id),
                "side": str(pos.side),
                "ts_opened_ns": int(pos.ts_opened),
                "ts_closed_ns": int(pos.ts_closed) if pos.ts_closed else 0,
                "entry_price": float(pos.avg_px_open),
                "exit_price": float(pos.avg_px_close) if pos.avg_px_close else 0.0,
                "quantity": float(pos.quantity),
                "pnl": pnl_val,
                "realized_return": float(pos.realized_return),
            })

        # 12. Calculate performance metrics
        perf_metrics = calculate_performance_metrics(
            equity_curve=equity_series,
            trades=trades_list,
            initial_balance=request.initial_balance,
        )

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
            )
        else:
            artifact_manifest = self.artifact_store.persist_artifacts(
                run_id=run_id_str,
                equity_table=equity_table,
                trades_table=trades_table,
                orders_table=orders_table,
                manifest_meta=manifest_meta,
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
            "catalog_manifest": catalog_manifest,
            "artifact_manifest": artifact_manifest,
        }
