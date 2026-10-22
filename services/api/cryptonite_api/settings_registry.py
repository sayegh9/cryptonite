"""The settings registry (ADR 0007): one entry per platform setting, with its tier, apply mode and owning ADR.

The registry says where each value lives and what a change costs. A key is writable only when it is in ``WRITE_PATHS``
and its tier allows writes; the server applies the tier (``write_needs_totp``), never the UI. ``apply`` says what the
code does: the exit thresholds are read from the database every cycle (``hot``); a value read at import is ``restart``
until its reader moves to a per-cycle read, so the UI never promises a hot apply the code cannot deliver.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from cryptonite_agents.gateway.budget import DEFAULT_DAILY_CAP_USD, parse_cap
from cryptonite_execution.risk_limits import RiskLimitValues, effective
from cryptonite_schemas.exit_thresholds import (
    BOUNDS,
    DECIMALS,
    SETTING_KEYS,
    ExitThresholds,
    violations,
)
from cryptonite_schemas.runtime_settings import ROUTING_MODES, RUNTIME_BOUNDS


class Tier(StrEnum):
    T_L = "T/L"          # tightening is free; loosening needs operator TOTP (ADR 0006)
    TOTP = "TOTP"        # every change needs TOTP
    SESSION = "Session"  # operator session only
    G = "G"              # governance: changed by ADR, not by a toggle
    R = "R"              # read-only in the module
    FIXED = "Fixed"      # not configurable


class Tighter(StrEnum):
    """Which direction of a T/L value is the tighter one: lowering the budget or an exit threshold makes the system
    stricter (LOWER); lengthening a lockout does (HIGHER)."""
    LOWER = "lower"
    HIGHER = "higher"


class Apply(StrEnum):
    HOT = "hot"
    RESTART = "restart"
    DEPLOY = "deploy"


WRITABLE_TIERS = frozenset({Tier.T_L, Tier.TOTP, Tier.SESSION})
SERVICE_STORE = "env"  # values read from the environment at import, outside the API
SYSTEM_STORE = "system_settings"
# Keys with a write endpoint. Everything else stays read-only until its write path exists.
WRITE_PATHS = frozenset({"LLM_DAILY_BUDGET_USD", *SETTING_KEYS, *RUNTIME_BOUNDS})
EXIT_MONITOR_HEARTBEAT = "exit_monitor:heartbeat"  # the monitor publishes the set it applies here


class WriteRefused(Exception):
    """A change no tier allows: the key has no write path, or its tier never takes a write."""


def write_needs_totp(setting: "Setting", current: Decimal, new: Decimal) -> bool:
    """Whether a change needs the operator's TOTP (ADR 0007 tiers). Raises WriteRefused for a change no tier allows.

    Every T/L key with a write path is written so that a lower value is tighter: less AI spend for the budget, and
    exits that fire sooner for the exit thresholds. So lowering is free and raising needs TOTP.
    """
    if setting.key not in WRITE_PATHS or setting.tier not in WRITABLE_TIERS:
        raise WriteRefused(f"{setting.key} is not writable")
    if setting.tier is Tier.TOTP:
        return True
    if setting.tier is Tier.SESSION:
        return False
    if setting.tighter is Tighter.HIGHER:
        return new < current  # the higher value is the tighter one: lowering loosens and needs TOTP
    return new > current  # T/L: tightening is free; loosening needs TOTP


def validate_value(setting: "Setting", value: Decimal) -> None:
    """Raise ValueError when ``value`` is outside the setting's bounds or has more decimal places than it takes."""
    if not value.is_finite():
        raise ValueError("must be a finite number")
    if setting.minimum is not None and value < Decimal(str(setting.minimum)):
        raise ValueError(f"must be at least {setting.minimum}")
    if setting.maximum is not None and value > Decimal(str(setting.maximum)):
        raise ValueError(f"must be at most {setting.maximum}")
    if value != value.quantize(quantum(setting)):
        raise ValueError(f"must have at most {setting.decimals} decimal places")


def quantum(setting: "Setting") -> Decimal:
    return Decimal(1).scaleb(-setting.decimals)


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
    decimals: int = 2  # decimal places a value may have
    tighter: Tighter = Tighter.LOWER  # which direction is the tighter one (T/L keys)
    # An enum stored as a number: (stored value, label) in order. The registry stays numeric (bounds, TOTP direction);
    # the UI shows the labels
    choices: tuple[tuple[str, str], ...] = ()

    @property
    def writable(self) -> bool:
        return self.tier in WRITABLE_TIERS


def _runtime_setting(key: str, label: str, tier: Tier, adr: str, note: str,
                     tighter: Tighter = Tighter.LOWER, choices: tuple[tuple[str, str], ...] = ()) -> Setting:
    """A value the services read every cycle (ADR 0007): stored in system_settings, whole numbers, hot."""
    bound = RUNTIME_BOUNDS[key]
    return Setting(key, label, int(bound.default), int(bound.low), int(bound.high), Apply.HOT, tier, adr,
                   SYSTEM_STORE, note, decimals=0, tighter=tighter, choices=choices)


def _exit_setting(key: str, label: str, default: Decimal | int | float, field: str, note: str) -> Setting:
    low, high = BOUNDS[field]
    return Setting(key, label, default, Decimal(low), Decimal(high), Apply.HOT, Tier.T_L, "0002", SYSTEM_STORE,
                   note, decimals=DECIMALS.get(field, 2))


REGISTRY: tuple[Setting, ...] = (
    Setting("LLM_DAILY_BUDGET_USD", "LLM daily budget (USD)", Decimal("25.00"), Decimal("1.00"), None,
            Apply.HOT, Tier.T_L, "0002", SYSTEM_STORE,
            "the budget guard re-reads it every 10 s; floor $1: 0 would stop every non-critical AI call (pause with the mandate or kill switch)"),
    Setting("risk.max_daily_loss_pct", "Max daily loss (% of equity)", Decimal("5"), Decimal("0"), Decimal("5"),
            Apply.HOT, Tier.T_L, "0006", "risk_limit_overrides", "the code default is the ceiling; PAPER account shown"),
    Setting("risk.max_order_equity_pct", "Max single order (% of equity)", Decimal("25"), Decimal("1"), Decimal("25"),
            Apply.HOT, Tier.T_L, "0006", "risk_limit_overrides", "PAPER account shown"),
    Setting("risk.cross_mandate_exposure_pct", "Cross-mandate BTC exposure (% of equity)", Decimal("25"), Decimal("1"),
            Decimal("25"), Apply.HOT, Tier.T_L, "0006", "risk_limit_overrides", "PAPER account shown"),
    _exit_setting("exit.stop_loss_pct", "Hard stop (% loss)", 5, "stop_loss_pct",
                  "read by the exit monitor every cycle; the env default applies until a value is stored"),
    _exit_setting("exit.trail_activate_pct", "Trail activates at (% gain)", 3, "trail_activate_pct",
                  "must be at least exit.trail_pct"),
    _exit_setting("exit.trail_pct", "Trail give-back (%)", 2, "trail_pct",
                  "must not exceed exit.trail_activate_pct"),
    _exit_setting("exit.debate_drawdown_pct", "Exit debate trigger (% loss)", 2, "debate_drawdown_pct",
                  "must stay below exit.stop_loss_pct"),
    _exit_setting("exit.funding_spike", "Funding spike trigger (per interval)", 0.0005, "funding_spike",
                  "funding per interval against the position"),
    _runtime_setting("exit.monitor_interval_s", "Exit monitor interval (s)", Tier.SESSION, "0002",
                     "read at the start of every cycle; EXIT_MONITOR_INTERVAL_S sets the default"),
    _runtime_setting("alerts.heartbeat_grace_s", "Heartbeat alert grace (s)", Tier.T_L, "0004",
                     "read on every ops-monitor pass; a lower value raises the alert sooner"),
    _runtime_setting("alerts.sentiment_stale_s", "Sentiment stale alert (s)", Tier.SESSION, "0004",
                     "read on every ops-monitor pass; SENTIMENT_STALE_ALERT_S sets the default"),
    _runtime_setting("scanner.max_active_specs", "Max active scanner specs", Tier.SESSION, "0004",
                     "read on every spec change and scan cycle; MAX_ACTIVE_SPECS sets the default. "
                     "Presets turn on and off as scan specs in Scanner."),
    _runtime_setting("auth.session_ttl_s", "Operator session, absolute (s)", Tier.T_L, "0007",
                     "a session ends this long after sign-in; the ceiling is the current 12 h, so this can only shorten it"),
    _runtime_setting("auth.idle_timeout_s", "Operator session, idle (s)", Tier.T_L, "0007",
                     "a session ends after this long without a request; the ceiling is the current 30 min"),
    _runtime_setting("auth.lockout_s", "Failed-login window (s)", Tier.T_L, "0007",
                     "failed logins are counted over this window; a longer window is the tighter one",
                     tighter=Tighter.HIGHER),
    _runtime_setting("auth.max_login_attempts", "Failed logins before lockout", Tier.T_L, "0007",
                     "a lower count locks out sooner"),
    Setting("alerts.routing", "Alert routing", "critical only", None, None, Apply.DEPLOY, Tier.R, "0004",
            "code: notifier/bot.py",
            "Critical alerts go to Telegram and ntfy; every other alert stays in the app. A toggle needs a read grant "
            "on system_settings for the notifier role (a later migration)."),
    Setting("universe.symbols", "Ingested symbols", "", None, None, Apply.RESTART, Tier.R, "onboarding plan",
            SERVICE_STORE, "INGEST_SYMBOLS; changed through onboarding only"),
    Setting("guard.position_clamp_pct", "Single-order equity clamp (%)", 25, None, None, Apply.DEPLOY, Tier.FIXED, "0004",
            "code: risk_limits.py", "the 25% per-order clamp is an invariant"),
    Setting("guard.account_backstop_pct", "Account backstop drawdown (%)", 20, None, None, Apply.DEPLOY, Tier.FIXED,
            "0004", "code: risk_limits.py", "never configurable"),
    Setting("guard.min_order_pct", "Minimum order (% of equity)", 1, None, None, Apply.DEPLOY, Tier.FIXED, "0004",
            "code: risk_limits.py", "not configurable"),
    # Leverage (ADR 0009). The grade ceilings and the slippage allowance are the operator's; the rest is fixed.
    _runtime_setting("leverage.grade_ceiling_a", "Leverage ceiling, setup grade A (x)", Tier.T_L, "0009",
                     "read by the orchestrator every minute; never above the mandate's or the 10x account maximum"),
    _runtime_setting("leverage.grade_ceiling_b", "Leverage ceiling, setup grade B (x)", Tier.T_L, "0009",
                     "read by the orchestrator every minute"),
    _runtime_setting("leverage.grade_ceiling_c", "Leverage ceiling, setup grade C (x)", Tier.T_L, "0009",
                     "read by the orchestrator every minute; an ungraded AI perp entry is held to this ceiling"),
    Setting("leverage.slippage_allowance_pct", "Stop slippage allowance (%)", Decimal("0.30"), Decimal("0.05"),
            Decimal("2.00"), Apply.HOT, Tier.T_L, "0009", SYSTEM_STORE,
            "priced into the risk at the stop and the liquidation buffer; a higher allowance is the tighter one",
            decimals=2, tighter=Tighter.HIGHER),
    _runtime_setting("debate.perp_mandate_daily_cap", "Scanner debates per perp mandate per day", Tier.T_L, "0010",
                     "read by the debate gate on every scanner candidate that would route to a perp mandate (1h bars "
                     "only); 0 stops them; lowering is free, raising needs TOTP"),
    _runtime_setting("debate.perp_routing", "Spot candidates: spot or perps first", Tier.T_L, "0010",
                     "read by the debate worker (cached up to 60 s) when a spot and a perps mandate both match a "
                     "candidate: SPOT_FIRST takes the spot mandate, PERP_FIRST the single perps mandate; an explicit "
                     "mandate always wins. PERP_FIRST sends more trades to leveraged perps, so it needs TOTP; "
                     "SPOT_FIRST is free",
                     choices=tuple((str(i), mode) for i, mode in enumerate(ROUTING_MODES))),
    # Auto-trade profiles (ADR 0011): enabling and raising are risk-adding (TOTP); switching off and lowering are free
    _runtime_setting("autotrade.enabled", "Auto-trade master switch", Tier.T_L, "0011",
                     "read at Start: while off, a profile in Veto window or Fully automatic cannot start and Ask "
                     "profiles still run; enabling needs TOTP, switching off is free",
                     choices=(("0", "Off"), ("1", "On"))),
    _runtime_setting("autotrade.max_allocated_pct", "Max capital allocated to running profiles (% of equity)",
                     Tier.T_L, "0011",
                     "the budgets of the Active, Paused and Stopping profiles add up to at most this share of account "
                     "equity (checked at Start); raising it needs TOTP"),
    Setting("guard.mandate_max_leverage", "Mandate leverage ceiling (x)", 10, None, None, Apply.DEPLOY, Tier.FIXED,
            "0009", "migration 048: chk_ai_mandates_leverage", "each mandate's own maximum (default 3x) sits under it"),
    Setting("guard.account_max_leverage", "Account leverage maximum (x)", 10, None, None, Apply.DEPLOY, Tier.FIXED,
            "0009", "code: leverage.py ACCOUNT_MAX_LEVERAGE", "the manual ticket and the dispatcher"),
    Setting("guard.perps_level", "Perps allowed at", "Any execution mode (ADR 0011)", None, None, Apply.DEPLOY,
            Tier.FIXED, "0011", "migration 050: chk_ai_mandates_perps_level dropped",
            "a profile trades perps when PERP is in its markets, in Ask, Veto window or Fully automatic"),
    Setting("guard.max_risk_per_trade_pct", "Risk per trade cap (% of the mandate budget)", 2, None, None,
            Apply.DEPLOY, Tier.FIXED, "0009", "migration 048: chk_ai_mandates_risk_per_trade",
            "default 1%; a mandate raises it with TOTP"),
    Setting("guard.liquidation_buffer_pct", "Stop within this share of the liquidation distance (%)", 50, None, None,
            Apply.DEPLOY, Tier.FIXED, "0009", "code: leverage.py LIQUIDATION_BUFFER_FRACTION",
            "stop distance + slippage allowance"),
    Setting("guard.perp_notional_pct", "Perp notional clamp (% of equity)", 25, None, None, Apply.DEPLOY, Tier.FIXED,
            "0009", "code: risk.py max_position_concentration_pct", "D1: notional, not margin"),
)

_RISK_KEYS = {
    "risk.max_daily_loss_pct": "max_daily_loss_pct",
    "risk.max_order_equity_pct": "max_order_equity_pct",
    "risk.cross_mandate_exposure_pct": "cross_mandate_exposure_pct",
}


def stored_text(raw: Any) -> str | None:
    """A stored value as text: the API writes a JSON string ("3.00"); a bare number is accepted too."""
    if raw is None:
        return None
    if isinstance(raw, str) and raw.strip().startswith('"'):
        return str(json.loads(raw))
    return str(raw)


def stored_decimal(raw: Any, setting: Setting) -> Decimal:
    """The value in force for a key: the stored one, or the default (also when the stored text does not parse).
    Quantized to the key's decimal places."""
    try:
        text = stored_text(raw)
        value = Decimal(text) if text is not None else Decimal(str(setting.default))
        return value.quantize(quantum(setting))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(str(setting.default)).quantize(quantum(setting))


def current_values(stored: Mapping[str, Any], row: Any) -> dict[str, Any]:
    """Values the API can read: the stored budget and exit thresholds, and the PAPER risk overrides. A key with no
    stored value reports None (its default applies), and the entry says so. Services that read at import report
    nothing here."""
    values: dict[str, Any] = {}
    budget = stored.get("LLM_DAILY_BUDGET_USD")
    values["LLM_DAILY_BUDGET_USD"] = None if budget is None else str(parse_cap(budget, DEFAULT_DAILY_CAP_USD))
    for key in (*SETTING_KEYS, *RUNTIME_BOUNDS):
        values[key] = stored_text(stored.get(key))
    effective_limits = effective(RiskLimitValues.from_row(row)).as_dict()
    for key, field in _RISK_KEYS.items():
        values[key] = effective_limits[field]
    return values


def heartbeat_exit_values(raw: Any) -> dict[str, str]:
    """The exit thresholds the monitor says it applies (its heartbeat), keyed by setting key. Empty when unknown."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    out: dict[str, str] = {}
    for key, field in SETTING_KEYS.items():
        if field in data:
            out[key] = format(Decimal(str(data[field])).normalize(), "f")
    return out


def exit_rule_violations(stored: Mapping[str, Any], setting: Setting, new: Decimal) -> list[str]:
    """The rules the exit set would break if ``setting`` took ``new``, with the other keys as stored (or default)."""
    values = {}
    for key, field in SETTING_KEYS.items():
        entry = setting if key == setting.key else next(s for s in REGISTRY if s.key == key)
        value = new if key == setting.key else stored_decimal(stored.get(key), entry)
        values[field] = float(value)
    return violations(ExitThresholds(**values))


def entry_view(setting: Setting, value: Any, effective_value: Any = None) -> dict[str, Any]:
    """One registry entry as the API returns it. ``value`` is None where a service owns the value or nothing is stored
    (then the default applies); ``effective_value`` is what the service reports it applies, when it reports one."""
    if value is not None:
        source = "api"
    elif setting.store == SERVICE_STORE:
        source = "service"
    elif setting.store == SYSTEM_STORE:
        source = "default"
    else:
        source = "code"
    return {
        "key": setting.key,
        "label": setting.label,
        "default": str(setting.default),
        "minimum": None if setting.minimum is None else str(setting.minimum),
        "maximum": None if setting.maximum is None else str(setting.maximum),
        "decimals": setting.decimals,
        "apply": setting.apply.value,
        "tier": setting.tier.value,
        "writable": setting.writable and setting.key in WRITE_PATHS,  # the tier allows it and a write path exists
        "adr": setting.adr,
        "store": setting.store,
        "note": setting.note,
        "value": value,
        "value_source": source,
        "effective": effective_value,
        "tighter": setting.tighter.value,
        "choices": [{"value": v, "label": label} for v, label in setting.choices],
    }


PROD_ENVS = frozenset({"PROD", "PRODUCTION"})  # the same names the login gate checks (auth.py)


def health_warnings(*, environment: str, dev_totp_login: bool, dev_totp_approvals: bool, secure_cookie: bool,
                    kill_switch_active: bool) -> list[dict[str, str]]:
    """Warnings for the Settings health panel. Each flag is computed from the environment by the route, so the
    panel says what the running process will do, not what the .env file says."""
    warnings: list[dict[str, str]] = []
    if dev_totp_approvals:
        warnings.append({"code": "DEV_TOTP_APPROVALS", "severity": "critical",
                         "text": "AI_ALLOW_DEV_TOTP is on: risk-adding AI approvals accept the 000000 code. Turn it off."})
    if dev_totp_login:
        warnings.append({"code": "DEV_TOTP_LOGIN", "severity": "warn",
                         "text": f"The operator login accepts the 000000 code (ALLOW_DEV_TOTP on, ENVIRONMENT={environment}). "
                                 "It is refused only when ENVIRONMENT is PROD."})
    if environment in PROD_ENVS and not secure_cookie:
        warnings.append({"code": "INSECURE_COOKIE", "severity": "warn",
                         "text": "SECURE_COOKIE is off in PROD: the session cookie is not marked Secure."})
    if kill_switch_active:
        warnings.append({"code": "KILL_SWITCH_LATCHED", "severity": "warn",
                         "text": "The kill switch is latched: new trades are halted until it is reset."})
    return warnings
