"""Pydantic schemas and models for the Scanner Engine and API."""

import hashlib
import json
import os
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Self
from uuid import UUID

from cryptonite_schemas.runtime_settings import RUNTIME_BOUNDS, read_number
from pydantic import BaseModel, Field, model_validator

MAX_ACTIVE_SPECS: int = int(os.getenv("MAX_ACTIVE_SPECS", "10"))
ALLOWED_AUTOMATIC_TIMEFRAMES: tuple[str, ...] = ("15m", "1h", "4h")
MIN_RISK_REWARD_RATIO: float = 1.5


async def active_spec_cap(db: Any) -> int:
    """The cap on active scan specs (ADR 0007): the stored ``scanner.max_active_specs``, else the env default.
    ``db`` is an asyncpg pool or connection. A failed read or a refused value falls back to the default, so a
    missing grant or a bad row never stops scanning."""
    default = int(MAX_ACTIVE_SPECS)
    try:
        raw = await db.fetchval("SELECT value FROM system_settings WHERE key = 'scanner.max_active_specs'")
    except Exception:
        return default
    bound = replace(RUNTIME_BOUNDS["scanner.max_active_specs"], default=default)
    value, _ = read_number(raw, bound, last_good=default)
    return int(value)


class RuleOperator(StrEnum):
    GT = ">"
    GTE = ">="
    LT = "<"
    LTE = "<="
    EQ = "=="
    CROSS_ABOVE = "cross_above"
    CROSS_BELOW = "cross_below"
    CROSSES_ABOVE = "crosses_above"
    CROSSES_BELOW = "crosses_below"


class RuleCondition(BaseModel):
    field: str
    operator: RuleOperator
    value: float | str  # numeric threshold or target column name e.g. "ema_200"
    lookback: int = 0

    @model_validator(mode="before")
    @classmethod
    def set_default_lookback(cls, data: Any) -> Any:
        if isinstance(data, dict):
            op = data.get("operator")
            if op in (
                RuleOperator.CROSS_ABOVE,
                RuleOperator.CROSS_BELOW,
                RuleOperator.CROSSES_ABOVE,
                RuleOperator.CROSSES_BELOW,
                "cross_above",
                "cross_below",
                "crosses_above",
                "crosses_below",
            ) and "lookback" not in data:
                data["lookback"] = 1
        return data

    @model_validator(mode="after")
    def validate_condition(self) -> Self:
        is_crossover = self.operator in (
            RuleOperator.CROSS_ABOVE,
            RuleOperator.CROSS_BELOW,
            RuleOperator.CROSSES_ABOVE,
            RuleOperator.CROSSES_BELOW,
        )
        if is_crossover:
            if self.lookback < 1:
                raise ValueError(
                    f"Crossover condition '{self.operator}' requires lookback >= 1, got {self.lookback}"
                )
        elif self.lookback < 0:
            raise ValueError(f"Condition lookback cannot be negative, got {self.lookback}")
        return self


class RuleGroup(BaseModel):
    category: str  # universe, liquidity, trend, trigger, confirmation, risk
    logic: str = "AND"  # AND or OR
    conditions: list[RuleCondition] = Field(default_factory=list)


class UniverseConfig(BaseModel):
    instrument_ids: list[str] = Field(default_factory=lambda: ["BTCUSDT.BINANCE", "ETHUSDT.BINANCE"])
    timeframes: list[str] = Field(default_factory=lambda: ["1m", "5m", "15m", "1h"])


class RiskConfig(BaseModel):
    direction: str = "LONG"  # "LONG" or "SHORT"
    stop_mode: str = "structure"  # "structure", "donchian", "atr"
    donchian_lookback: int = 20
    min_atr_mult: float = 0.5
    max_atr_mult: float = 4.0
    fallback_stop_atr_mult: float = 2.0
    fallback_target_atr_mult: float = 3.0
    min_risk_reward: float = 1.5
    stop_loss_atr_mult: float = 2.0  # legacy compatibility
    take_profit_atr_mult: float = 4.0  # legacy compatibility
    risk_reward_ratio: float = 2.0  # legacy compatibility


def compute_spec_hash(universe: dict[str, Any], rules: list[Any] | dict[str, Any]) -> str:
    """Compute deterministic SHA-256 hash over canonical sorted JSON."""
    canonical_obj = {"rules": rules, "universe": universe}
    canonical_str = json.dumps(canonical_obj, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()


class ScanSpecCreate(BaseModel):
    slug: str
    name: str
    description: str | None = None
    universe: UniverseConfig = Field(default_factory=UniverseConfig)
    rules: list[RuleGroup] = Field(default_factory=list)
    risk: RiskConfig = Field(default_factory=RiskConfig)


class ScanSpecVersionRead(BaseModel):
    id: UUID
    spec_id: UUID
    version_num: int
    spec_hash: str
    universe: dict[str, Any]
    rules: list[dict[str, Any]] | dict[str, Any]
    created_at: datetime


class ScanSpecRead(BaseModel):
    id: UUID
    slug: str
    name: str
    description: str | None = None
    active_version_id: UUID | None = None
    active_version: ScanSpecVersionRead | None = None
    is_archived: bool = False
    created_at: datetime
    updated_at: datetime


class ScanJobCreate(BaseModel):
    request_id: str
    spec_version_id: UUID
    requested_watermark_ns: int | None = None
    requested_range_start_ns: int | None = None
    requested_range_end_ns: int | None = None


class ScanJobRead(BaseModel):
    id: UUID
    request_id: str
    spec_version_id: UUID
    status: str
    requested_watermark_ns: int | None = None
    requested_range_start_ns: int | None = None
    requested_range_end_ns: int | None = None
    scan_run_id: UUID | None = None
    error_message: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None


class ScanRunRead(BaseModel):
    id: UUID
    spec_version_id: UUID
    spec_hash: str
    scanner_version: str
    indicator_version: str
    input_watermark_ns: int
    source_partitions: list[str]
    source_partition_checksums: dict[str, str]
    trigger_reason: str
    trigger_event_id: str | None = None
    status: str
    started_at: datetime
    completed_at: datetime | None = None
    candidate_count: int
    error_message: str | None = None
    created_at: datetime


class SignalCandidateRead(BaseModel):
    id: UUID
    scan_run_id: UUID
    spec_version_id: UUID
    instrument_id: str
    timeframe: str
    bar_open_ns: int
    bar_close_inclusive_ns: int
    interval_end_exclusive_ns: int
    matched_rules: list[str]
    metrics: dict[str, Any]
    suggested_entry: Decimal
    suggested_stop: Decimal
    suggested_target: Decimal
    risk_reward_ratio: Decimal | None = None
    created_at: datetime

    @model_validator(mode="after")
    def populate_risk_reward(self) -> "SignalCandidateRead":
        if self.risk_reward_ratio is None:
            if "risk_reward_ratio" in self.metrics and self.metrics["risk_reward_ratio"] is not None:
                self.risk_reward_ratio = Decimal(str(self.metrics["risk_reward_ratio"]))
            elif self.suggested_entry is not None and self.suggested_stop is not None and self.suggested_target is not None:
                risk = abs(self.suggested_entry - self.suggested_stop)
                reward = abs(self.suggested_target - self.suggested_entry)
                if risk > Decimal("0"):
                    self.risk_reward_ratio = reward / risk
        return self


def deduplicate_candidates(candidates: list[SignalCandidateRead]) -> list[SignalCandidateRead]:
    """Deduplicate candidates per (instrument_id, timeframe, bar_close_inclusive_ns) across specs."""
    seen: set[tuple[str, str, int]] = set()
    deduped: list[SignalCandidateRead] = []
    for c in candidates:
        key = (c.instrument_id, c.timeframe, c.bar_close_inclusive_ns)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(c)
    return deduped
