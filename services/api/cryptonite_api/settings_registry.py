"""The settings registry (ADR 0007): one entry per platform setting, with its tier, apply mode and owning ADR.

The registry says where each value lives and what a change costs. A key is writable only when it is in ``WRITE_PATHS``
and its tier allows writes; the server applies the tier (``write_needs_totp``), never the UI. ``apply`` describes what
the code does today: a value read at import is ``restart`` until its reader moves to a per-cycle read, so the UI never
promises a hot apply the code cannot deliver.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

from cryptonite_agents.gateway.budget import DEFAULT_DAILY_CAP_USD, parse_cap
from cryptonite_execution.risk_limits import RiskLimitValues, effective


class Tier(StrEnum):
    T_L = "T/L"          # tightening is free; loosening needs operator TOTP (ADR 0006)
    TOTP = "TOTP"        # every change needs TOTP
    SESSION = "Session"  # operator session only
    G = "G"              # governance: changed by ADR, not by a toggle
    R = "R"              # read-only in the module
    FIXED = "Fixed"      # not configurable


class Apply(StrEnum):
    HOT = "hot"
    RESTART = "restart"
    DEPLOY = "deploy"


WRITABLE_TIERS = frozenset({Tier.T_L, Tier.TOTP, Tier.SESSION})
SERVICE_STORE = "env"  # values read from the environment at import, outside the API
# Keys with a write endpoint. Everything else stays read-only until its write path (and its direction of "tighter") exists.
WRITE_PATHS = frozenset({"LLM_DAILY_BUDGET_USD"})


class WriteRefused(Exception):
    """A change no tier allows: the key has no write path, or its tier never takes a write."""


def write_needs_totp(setting: "Setting", current: Decimal, new: Decimal) -> bool:
    """Whether a change needs the operator's TOTP (ADR 0007 tiers). Raises WriteRefused for a change no tier allows.

    T/L is written for the LLM budget only, where lower is tighter (less spend). Any other T/L key needs its own
    direction before it joins WRITE_PATHS.
    """
    if setting.key not in WRITE_PATHS or setting.tier not in WRITABLE_TIERS:
        raise WriteRefused(f"{setting.key} is not writable")
    if setting.tier is Tier.TOTP:
        return True
    if setting.tier is Tier.SESSION:
        return False
    return new > current  # T/L: tightening (lowering the budget) is free; loosening needs TOTP


def validate_value(setting: "Setting", value: Decimal) -> None:
    """Raise ValueError when ``value`` is outside the setting's bounds."""
    if not value.is_finite():
        raise ValueError("must be a finite number")
    if setting.minimum is not None and value < Decimal(str(setting.minimum)):
        raise ValueError(f"must be at least {setting.minimum}")
    if setting.maximum is not None and value > Decimal(str(setting.maximum)):
        raise ValueError(f"must be at most {setting.maximum}")


@dataclass(frozen=True)
class Setting:
    key: str
    label: str
    default: Decimal | int | float
    minimum: Decimal | int | float | None
    maximum: Decimal | int | float | None
    apply: Apply
    tier: Tier
    adr: str
    store: str
    note: str = ""

    @property
    def writable(self) -> bool:
        return self.tier in WRITABLE_TIERS


REGISTRY: tuple[Setting, ...] = (
    Setting("LLM_DAILY_BUDGET_USD", "LLM daily budget (USD)", Decimal("25.00"), Decimal("0"), None,
            Apply.HOT, Tier.T_L, "0002", "system_settings", "the budget guard re-reads it every 10 s"),
    Setting("risk.max_daily_loss_pct", "Max daily loss (% of equity)", Decimal("5"), Decimal("0"), Decimal("5"),
            Apply.HOT, Tier.T_L, "0006", "risk_limit_overrides", "the code default is the ceiling; PAPER account shown"),
    Setting("risk.max_order_equity_pct", "Max single order (% of equity)", Decimal("25"), Decimal("1"), Decimal("25"),
            Apply.HOT, Tier.T_L, "0006", "risk_limit_overrides", "PAPER account shown"),
    Setting("risk.cross_mandate_exposure_pct", "Cross-mandate BTC exposure (% of equity)", Decimal("25"), Decimal("1"),
            Decimal("25"), Apply.HOT, Tier.T_L, "0006", "risk_limit_overrides", "PAPER account shown"),
    Setting("exit.stop_loss_pct", "Hard stop (% loss)", 5, 0, 20, Apply.RESTART, Tier.T_L, "0002",
            SERVICE_STORE, "EXIT_STOP_LOSS_PCT, read at import; hot after the per-cycle read ships"),
    Setting("exit.trail_activate_pct", "Trail activates at (% gain)", 3, 0, None, Apply.RESTART, Tier.T_L, "0002",
            SERVICE_STORE, "EXIT_TRAIL_ACTIVATE_PCT, read at import"),
    Setting("exit.trail_pct", "Trail give-back (%)", 2, 0, None, Apply.RESTART, Tier.T_L, "0002",
            SERVICE_STORE, "EXIT_TRAIL_PCT; must not exceed exit.trail_activate_pct"),
    Setting("exit.debate_drawdown_pct", "Exit debate trigger (% loss)", 2, 0, None, Apply.RESTART, Tier.T_L, "0002",
            SERVICE_STORE, "EXIT_DEBATE_DRAWDOWN_PCT; must stay below exit.stop_loss_pct"),
    Setting("exit.funding_spike", "Funding spike trigger (per interval)", 0.0005, 0, 0.01, Apply.RESTART, Tier.T_L,
            "0002", SERVICE_STORE, "EXIT_FUNDING_SPIKE"),
    Setting("exit.monitor_interval_s", "Exit monitor interval (s)", 30, 5, 300, Apply.RESTART, Tier.SESSION, "0002",
            SERVICE_STORE, "EXIT_MONITOR_INTERVAL_S"),
    Setting("alerts.heartbeat_grace_s", "Heartbeat alert grace (s)", 300, 60, 3600, Apply.RESTART, Tier.T_L, "0004",
            SERVICE_STORE, "HEARTBEAT_ALERT_GRACE_S, read at import"),
    Setting("alerts.sentiment_stale_s", "Sentiment stale alert (s)", 86400, 3600, None, Apply.RESTART, Tier.SESSION,
            "0004", SERVICE_STORE, "SENTIMENT_STALE_ALERT_S, read at import"),
    Setting("scanner.max_active_specs", "Max active scanner specs", 10, 1, 50, Apply.RESTART, Tier.SESSION, "0004",
            SERVICE_STORE, "MAX_ACTIVE_SPECS, read at import"),
    Setting("universe.symbols", "Ingested symbols", "", None, None, Apply.RESTART, Tier.R, "onboarding plan",
            SERVICE_STORE, "INGEST_SYMBOLS; changed through onboarding only"),
    Setting("guard.position_clamp_pct", "Single-order equity clamp (%)", 25, None, None, Apply.DEPLOY, Tier.FIXED, "0004",
            "code: risk_limits.py", "the 25% per-order clamp is an invariant"),
    Setting("guard.account_backstop_pct", "Account backstop drawdown (%)", 20, None, None, Apply.DEPLOY, Tier.FIXED,
            "0004", "code: risk_limits.py", "never configurable"),
    Setting("guard.min_order_pct", "Minimum order (% of equity)", 1, None, None, Apply.DEPLOY, Tier.FIXED, "0004",
            "code: risk_limits.py", "not configurable"),
)

_RISK_KEYS = {
    "risk.max_daily_loss_pct": "max_daily_loss_pct",
    "risk.max_order_equity_pct": "max_order_equity_pct",
    "risk.cross_mandate_exposure_pct": "cross_mandate_exposure_pct",
}


def current_values(budget_value: Any, row: Any) -> dict[str, Any]:
    """Live values the API can read today: the budget row and the PAPER risk overrides. Settings read by services
    at import return no value here."""
    values: dict[str, Any] = {"LLM_DAILY_BUDGET_USD": str(parse_cap(budget_value, DEFAULT_DAILY_CAP_USD))}
    effective_limits = effective(RiskLimitValues.from_row(row)).as_dict()
    for key, field in _RISK_KEYS.items():
        values[key] = effective_limits[field]
    return values


def entry_view(setting: Setting, value: Any) -> dict[str, Any]:
    """One registry entry as the API returns it. ``value`` is None where a service owns the value."""
    return {
        "key": setting.key,
        "label": setting.label,
        "default": str(setting.default),
        "minimum": None if setting.minimum is None else str(setting.minimum),
        "maximum": None if setting.maximum is None else str(setting.maximum),
        "apply": setting.apply.value,
        "tier": setting.tier.value,
        "writable": setting.writable and setting.key in WRITE_PATHS,  # the tier allows it and a write path exists
        "adr": setting.adr,
        "store": setting.store,
        "note": setting.note,
        "value": value,
        "value_source": "api" if value is not None else ("service" if setting.store == SERVICE_STORE else "code"),
    }
