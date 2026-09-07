"""Audit and quarantine data models."""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class BatchStatus(StrEnum):
    PENDING = "pending"
    COMMITTED = "committed"
    FAILED = "failed"


class IngestBatchRecord(BaseModel):
    batch_id: str
    venue: str
    market_type: str
    symbol: str
    bar_type: str
    start_ns: int
    end_ns: int
    bar_count: int
    payload_checksum: str
    canonical_payload_json: list[dict[str, Any]]
    nautilus_paths: list[str] = Field(default_factory=list)
    analytics_paths: list[str] = Field(default_factory=list)
    nautilus_file_checksum: str | None = None
    analytics_file_checksum: str | None = None
    status: BatchStatus = BatchStatus.PENDING
    created_at: datetime = Field(default_factory=datetime.utcnow)
    committed_at: datetime | None = None


class QuarantineBarRecord(BaseModel):
    venue: str
    symbol: str
    bar_type: str
    ts_event_ns: int
    conflicting_data: dict[str, Any]
    existing_data: dict[str, Any] | None = None
    reason: str
    quarantined_at: datetime = Field(default_factory=datetime.utcnow)
