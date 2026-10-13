import datetime
import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from email.utils import parsedate_to_datetime
from pathlib import Path

import duckdb
from cryptonite_schemas.universe import INGEST_UNIVERSE_KEY, IngestUniverse
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

logger = logging.getLogger(__name__)

# Model-callable SQL runs as the read-only agent role, never as the worker's own DATABASE_URL role
# (debate_worker's is cryptonite_api, which can write execution_commands and system_settings).
DB_URL = os.getenv(
    "AGENT_TOOLS_DATABASE_URL",
    "postgresql://cryptonite_agent_ro:agent_ro_dev_pass@postgres:5432/cryptonite",
)
CATALOG_PATH = os.getenv("NAUTILUS_CATALOG_PATH", "/app/data/nautilus_catalog")
STATEMENT_TIMEOUT_MS = 10_000

_engine = None

def get_engine():
    global _engine
    if _engine is None:
        # NullPool: a fresh connection per call, so nothing a query leaves behind (advisory locks,
        # session settings) outlives the tool call
        _engine = create_engine(DB_URL, poolclass=NullPool)
    return _engine

def _single_statement(sql: str) -> str | None:
    """The statement without a trailing ';', or None when it contains more than one."""
    body = sql.strip().rstrip(";").strip()
    return None if not body or ";" in body else body

def query_postgres_readonly(sql: str) -> str:
    """Executes one SELECT query against the PostgreSQL database and returns the rows as JSON.

    Runs as the read-only agent role inside a READ ONLY transaction (which is always rolled back),
    one statement per call, with a 10 s statement timeout.
    """
    logger.info("Agent executing DB query: %s", sql)
    statement = _single_statement(sql)
    if statement is None:
        return "Database Error: exactly one SQL statement per call is allowed"
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SET TRANSACTION READ ONLY"))
            conn.execute(text(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}"))
            result = conn.execute(text(statement))
            rows = [dict(row._mapping) for row in result]
            conn.rollback()
            return json.dumps(rows, default=str)
    except Exception as e:
        return f"Database Error: {e}"

def get_scanner_context(instrument_id: str) -> str:
    """Gets the most recent signal candidates from the scanner for the instrument."""
    inst = instrument_id.replace("'", "''")
    sql = f"SELECT * FROM signal_candidates WHERE instrument_id = '{inst}' ORDER BY created_at DESC LIMIT 5"
    return query_postgres_readonly(sql)

def get_live_market_data(symbols: str = "BTCUSDT") -> str:
    """Fetches real-time live 24h ticker prices, 24h change %, and volume from Binance for one or more comma-separated symbols (e.g. 'BTCUSDT' or 'BTCUSDT,ETHUSDT')."""
    sym_list = [s.strip().split(".")[0].upper() for s in symbols.split(",") if s.strip()]
    if not sym_list:
        return "Error: No symbols provided"
    try:
        import httpx
        if len(sym_list) == 1:
            params = {"symbol": sym_list[0]}
        else:
            params = {"symbols": "[" + ",".join(f'"{s}"' for s in sym_list) + "]"}
        resp = httpx.get("https://api.binance.com/api/v3/ticker/24hr", params=params, timeout=5.0)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict):
                data = [data]
            results = [
                {
                    "symbol": item["symbol"],
                    "last_price": float(item["lastPrice"]),
                    "price_change_24h_pct": float(item["priceChangePercent"]),
                    "high_24h": float(item["highPrice"]),
                    "low_24h": float(item["lowPrice"]),
                    "volume_24h": float(item["volume"]),
                }
            for item in data
            ]
            return json.dumps(results, indent=2)
        return f"Binance API returned status {resp.status_code}: {resp.text}"
    except Exception as e:
        return f"Error fetching live market data: {e}"


_SYMBOL_REGEX = re.compile(r"^[A-Z0-9]{3,20}$")
_ALLOWED_INTERVALS = frozenset({"1m", "5m", "15m", "1h", "4h", "1d"})

_EXCHANGE_INFO_CACHE: set[str] | None = None
_EXCHANGE_INFO_FETCHED_AT: float = 0.0
_EXCHANGE_INFO_TTL_S: float = 86400.0  # 24h

# Bounded LRU cache (C2): exact-key hits are served within TTL; the cache never grows past
# _KLINES_CACHE_MAX. All reads/writes happen under _KLINES_CACHE_LOCK (see get_klines).
_KLINES_CACHE: OrderedDict[tuple[str, str, int], tuple[float, list[dict]]] = OrderedDict()
_KLINES_CACHE_TTL_S: float = 60.0  # 60s
_KLINES_CACHE_MAX = 256

# Serializes the preflight budget gate, the cache and the HTTP fetch so parallel Brain calls cannot
# interleave around the budget check (M1/C2): a request is only issued when the gate passed for that
# exact request, and the weight recorded by the previous response is visible to every waiter.
_KLINES_CACHE_LOCK = threading.Lock()

# Shared rate-limit budget: the last X-MBX-USED-WEIGHT-1M weight seen and the time it was recorded.
# New requests are refused up-front for _WEIGHT_WINDOW_S while the recorded weight is at/over the cap.
_WEIGHT_LIMIT = 3000
_LAST_USED_WEIGHT: int = 0
_LAST_WEIGHT_AT: float = 0.0
_WEIGHT_WINDOW_S: float = 60.0

# in_feed truthfulness (T1): the authoritative *live* ingest universe is Redis `ingest:universe`
# (published by the ingest service, TTL 180s). Cached 60s here; an empty/unreachable/unreadable key
# means "unknown" and callers must report in_feed="unknown" rather than guess.
_IN_FEED_CACHE: tuple[float, frozenset[str]] | None = None
_IN_FEED_TTL_S: float = 60.0


def _get_valid_symbols() -> set[str]:
    global _EXCHANGE_INFO_CACHE, _EXCHANGE_INFO_FETCHED_AT
    now = time.time()
    if _EXCHANGE_INFO_CACHE is not None and (now - _EXCHANGE_INFO_FETCHED_AT) < _EXCHANGE_INFO_TTL_S:
        return _EXCHANGE_INFO_CACHE
    try:
        import httpx
        resp = httpx.get("https://api.binance.com/api/v3/exchangeInfo", timeout=10.0)
        if resp.status_code == 200:
            data = resp.json()
            valid = {s["symbol"].upper() for s in data.get("symbols", []) if s.get("status") == "TRADING"}
            _EXCHANGE_INFO_CACHE = valid
            _EXCHANGE_INFO_FETCHED_AT = now
            return valid
    except Exception as e:
        logger.warning("Could not refresh Binance exchangeInfo: %s", e)
    if _EXCHANGE_INFO_CACHE is not None:
        return _EXCHANGE_INFO_CACHE
    return set()


def _parse_server_date_ms(raw: str | None) -> int | None:
    """Epoch milliseconds from an RFC 7231 HTTP 'Date' header (the Binance response's own time).

    ``None`` when the header is missing or unparseable: callers must FAIL CLOSED rather than fall
    back to the client clock for the closed-candle boundary (C1).
    """
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.UTC)
    return int(dt.timestamp() * 1000)


def _fed_symbols() -> frozenset[str]:
    """The authoritative live ingest universe (Redis ``ingest:universe``), cached for 60s (T1).

    Returns an EMPTY set when the key is missing, unreadable, or Redis is unreachable: callers must
    then report ``in_feed="unknown"`` rather than guess. Never raises.
    """
    global _IN_FEED_CACHE
    now = time.time()
    if _IN_FEED_CACHE is not None and (now - _IN_FEED_CACHE[0]) < _IN_FEED_TTL_S:
        return _IN_FEED_CACHE[1]
    universe: frozenset[str] = frozenset()
    try:
        import redis  # local import: Redis is a service runtime dep (agent_worker), not a package dep
        client = redis.Redis.from_url(
            os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            socket_timeout=0.5,
            socket_connect_timeout=0.5,
            decode_responses=True,
        )
        try:
            raw = client.get(INGEST_UNIVERSE_KEY)
        finally:
            client.close()
        parsed = IngestUniverse.parse(raw) if raw else None
        if parsed is not None:
            universe = frozenset(parsed.symbols)
    except Exception as e:  # pragma: no cover - environment dependent
        logger.warning("Could not read %s for in_feed; reporting 'unknown': %s", INGEST_UNIVERSE_KEY, e)
    _IN_FEED_CACHE = (time.time(), universe)
    return universe


def get_klines(symbol: str, interval: str = "1h", limit: int = 200) -> str:
    """Fetches historical klines/candles for any valid trading symbol on Binance.

    Parameters:
        symbol: Trading pair symbol (e.g. 'BTCUSDT', 'SOLUSDT', 'WLDUSDT').
        interval: Candle timeframe ('1m', '5m', '15m', '1h', '4h', '1d'). Default is '1h'.
        limit: Number of candles to retrieve (1 to 500). Default is 200.
    """
    clean_symbol = symbol.strip().split(".")[0].upper()
    if not _SYMBOL_REGEX.match(clean_symbol):
        return f"Error: Invalid symbol format '{symbol}'. Symbol must be alphanumeric (3-20 chars)."

    # Deterministic parameter validation runs BEFORE the network-dependent membership lookup, so
    # interval/limit errors surface identically whether or not Binance is reachable.
    if interval not in _ALLOWED_INTERVALS:
        return f"Error: Invalid interval '{interval}'. Allowed intervals: {sorted(_ALLOWED_INTERVALS)}"

    try:
        limit_val = int(limit)
    except (ValueError, TypeError):
        return f"Error: Limit must be an integer, got '{limit}'."

    if not (1 <= limit_val <= 500):
        return f"Error: Limit must be between 1 and 500, got {limit_val}."

    # M2 fail-closed membership: an empty/unavailable exchangeInfo lookup => refusal, no unverified
    # symbol lookup is ever issued (stale cache is still honored on refresh failure).
    valid_symbols = _get_valid_symbols()
    if not valid_symbols:
        return (
            f"Error: Cannot verify symbol '{clean_symbol}' against Binance exchangeInfo "
            f"(symbol lookup unavailable); refusing unverified symbol lookup."
        )
    if clean_symbol not in valid_symbols:
        return f"Error: Symbol '{clean_symbol}' is not a valid active trading pair on Binance."

    now = time.time()
    cache_key = (clean_symbol, interval, limit_val)

    # Serialized critical section (M1/C2): the cache, the preflight budget gate and the HTTP fetch
    # run under one lock, so parallel Brain calls cannot interleave around the budget check. A
    # request is only issued when the gate passed for that exact request, and the weight recorded by
    # the previous response is visible to every waiter. The bounded cache doubles as a request
    # coalescer: identical concurrent calls share a single response.
    global _LAST_USED_WEIGHT, _LAST_WEIGHT_AT
    with _KLINES_CACHE_LOCK:
        if cache_key in _KLINES_CACHE:
            cached_time, cached_result = _KLINES_CACHE[cache_key]
            if (now - cached_time) < _KLINES_CACHE_TTL_S:
                _KLINES_CACHE.move_to_end(cache_key)
                return json.dumps(cached_result, indent=2)

        # Pre-flight shared-budget gate: refuse BEFORE issuing a new request while the recorded
        # 1-minute weight is at/over the cap (the recorded weight is refreshed on every response).
        if _LAST_USED_WEIGHT >= _WEIGHT_LIMIT and (now - _LAST_WEIGHT_AT) < _WEIGHT_WINDOW_S:
            return (
                f"Error: Rate limit protection triggered (weight {_LAST_USED_WEIGHT} >= {_WEIGHT_LIMIT} "
                f"within the current 1-minute window). Request skipped before sending."
            )

        try:
            import httpx
            # Fetch slightly extra candles in case the last candle is still open
            fetch_limit = min(limit_val + 2, 500)
            params = {"symbol": clean_symbol, "interval": interval, "limit": fetch_limit}
            resp = httpx.get("https://api.binance.com/api/v3/klines", params=params, timeout=10.0)

            # Record the shared-budget weight from every response so the NEXT call pre-flights.
            used_weight_str = resp.headers.get("X-MBX-USED-WEIGHT-1M") or resp.headers.get("x-mbx-used-weight-1m")
            if used_weight_str:
                try:
                    _LAST_USED_WEIGHT = int(used_weight_str)
                    _LAST_WEIGHT_AT = time.time()
                except ValueError:
                    pass

            # 429/418 first: Retry-After is the actionable guidance and must not be masked by the
            # weight gate below.
            if resp.status_code in (429, 418):
                retry_after = resp.headers.get("Retry-After") or resp.headers.get("retry-after") or "60"
                return f"Error: Rate limited by Binance API (HTTP {resp.status_code}). Retry-After: {retry_after}s."

            if resp.status_code != 200:
                return f"Binance API returned status {resp.status_code}: {resp.text}"

            # Post-response gate for a served-but-over-budget request (kept so a 200 that pushed the
            # budget over the cap still fails closed).
            if _LAST_USED_WEIGHT > _WEIGHT_LIMIT:
                return f"Error: Rate limit protection triggered (weight {_LAST_USED_WEIGHT} > 3000). Request rejected."

            # C1: the closed-candle boundary comes from the SERVER's time (the RFC 7231 'Date'
            # header on the Binance response), never from the client clock. Second granularity is
            # conservative in the safe direction: a candle that closed inside the current server
            # second is dropped (never reported closed prematurely), and any candle closed at or
            # before the Date second is included. Missing/unparseable server time FAILS CLOSED.
            server_time_ms = _parse_server_date_ms(resp.headers.get("Date") or resp.headers.get("date"))
            if server_time_ms is None:
                return (
                    "Error: Binance server time unavailable ('Date' header missing or unparseable); "
                    "refusing to report a closed-candle boundary."
                )

            raw_klines = resp.json()
            closed_klines = [k for k in raw_klines if int(k[6]) <= server_time_ms]
            trimmed = closed_klines[-limit_val:] if len(closed_klines) > limit_val else closed_klines

            if not trimmed:
                return f"Error: No closed candles available for {clean_symbol} {interval}."

            fetched_at_iso = datetime.datetime.now(datetime.UTC).isoformat()
            last_closed_close_time_iso = datetime.datetime.fromtimestamp(trimmed[-1][6] / 1000, tz=datetime.UTC).isoformat()
            in_feed = "fed" if clean_symbol in _fed_symbols() else "unknown"

            candles = [
                {
                    "open_time_ms": k[0],
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5]),
                    "close_time_ms": k[6],
                }
                for k in trimmed
            ]

            output_data = {
                "source": "binance_spot_klines",
                "symbol": clean_symbol,
                "interval": interval,
                "fetched_at": fetched_at_iso,
                "last_closed_close_time": last_closed_close_time_iso,
                "in_feed": in_feed,
                "candle_count": len(candles),
                "candles": candles,
            }

            # Bounded LRU write: the newest entry goes to the MRU end; the oldest entry past the cap
            # is evicted so the cache size stays <= _KLINES_CACHE_MAX.
            _KLINES_CACHE[cache_key] = (time.time(), output_data)
            _KLINES_CACHE.move_to_end(cache_key)
            while len(_KLINES_CACHE) > _KLINES_CACHE_MAX:
                _KLINES_CACHE.popitem(last=False)
            return json.dumps(output_data, indent=2)

        except Exception as e:
            return f"Error fetching klines data: {e}"

def query_parquet_duckdb(query: str) -> str:
    """Executes a DuckDB SQL query against the massive Parquet data catalog.
    
    Use the glob path 'data/bars/**/*.parquet' inside the query to target historical bars.
    Do NOT attempt to SELECT * without a LIMIT or aggregation, as it will exhaust memory.
    """
    logger.info("Agent executing DuckDB query: %s", query)
    
    # 1. Block destructive operations and external I/O
    forbidden_keywords = r"(?i)\b(copy|write|insert|update|delete|drop|alter|create|read_csv|read_json|httpfs)\b"
    if re.search(forbidden_keywords, query):
        return "Security Error: Destructive commands, external I/O, and COPY are strictly prohibited."
        
    try:
        # Resolve path relative to CATALOG_PATH
        full_query = query.replace("data/bars", str(Path(CATALOG_PATH) / "data/bars"))
        
        # 2. Strict path validation: ensure ANY string literal containing a path is within CATALOG_PATH
        string_literals = re.findall(r"'([^']*)'", full_query)
        for literal in string_literals:
            if "/" in literal or "\\" in literal:
                resolved_literal = str(Path(literal).resolve())
                if not Path(resolved_literal).resolve().is_relative_to(Path(CATALOG_PATH).resolve()):
                    return f"Security Error: Unauthorized file access attempted outside catalog path. Blocked: {literal}"
        
        rel = duckdb.query(full_query)
        columns = rel.columns
        records = [dict(zip(columns, row)) for row in rel.fetchall()]
        # Serialize datetime objects using default str if necessary
        return json.dumps(records, default=str)
    except Exception as e:
        return f"DuckDB Error: {e}"

def read_nautilus_tearsheet(backtest_id: str) -> str:
    """Reads the JSON tearsheet or manifest for a specific backtest_id."""
    artifact_root = Path("/app/data/backtest_artifacts").resolve()
    
    # If no backtest_id is provided or requested latest, find the most recent artifact
    target_id = (backtest_id or "").strip()
    if not target_id or target_id.lower() in ("latest", "last", "none", "default"):
        if artifact_root.exists():
            dirs = [d for d in artifact_root.iterdir() if d.is_dir()]
            if dirs:
                dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
                target_id = dirs[0].name
            else:
                return "No backtest runs found in backtest_artifacts directory."
        else:
            return "Backtest artifacts directory does not exist."

    intended_path = (artifact_root / target_id / "tearsheet.json").resolve()
    
    # Strict containment check
    if not intended_path.is_relative_to(artifact_root):
        return f"Security Error: Path traversal detected for {target_id}."
        
    if not intended_path.exists():
        run_dir = artifact_root / target_id
        manifest_path = run_dir / "manifest.json"
        equity_path = run_dir / "equity_curve.parquet"
        trades_path = run_dir / "trades.parquet"

        summary = {"backtest_id": target_id}
        if manifest_path.exists():
            try:
                m = json.loads(manifest_path.read_text())
                summary["strategy"] = m.get("strategy_slug")
                summary["instrument_id"] = m.get("instrument_id")
                summary["timeframe"] = m.get("timeframe")
                summary["evaluation_start_ns"] = m.get("evaluation_start_ns")
                summary["evaluation_end_ns"] = m.get("evaluation_end_ns")
                # None = legacy metrics (equity only at fills): do not compare its drawdown/Sharpe with v2 runs
                summary["metrics_version"] = m.get("metrics_version")
                summary["benchmark"] = m.get("benchmark")
            except Exception:
                pass

        try:
            conn = duckdb.connect()
            if equity_path.exists():
                eq = conn.execute(f"SELECT equity, drawdown FROM '{equity_path}' ORDER BY ts_event_ns ASC").fetchall()
                if eq:
                    summary["initial_equity"] = eq[0][0]
                    summary["final_equity"] = eq[-1][0]
                    summary["total_return_pct"] = round(((eq[-1][0] - eq[0][0]) / eq[0][0]) * 100, 4)
                    summary["max_drawdown_pct"] = round(max(r[1] for r in eq) * 100, 4)

            if trades_path.exists():
                tr = conn.execute(f"SELECT realized_pnl, realized_return, entry_price, exit_price FROM '{trades_path}'").fetchall()
                summary["total_trades"] = len(tr)
                if tr:
                    wins = [r for r in tr if r[0] > 0]
                    summary["winning_trades"] = len(wins)
                    summary["win_rate_pct"] = round((len(wins) / len(tr)) * 100, 2)
                    summary["total_realized_pnl"] = round(sum(r[0] for r in tr), 4)

            return json.dumps(summary, indent=2)
        except Exception as e:
            if manifest_path.exists():
                return manifest_path.read_text()
            return f"Error: Tearsheet for backtest '{target_id}' not found ({e})."
        
    try:
        return intended_path.read_text()
    except Exception as e:
        return f"File Error: {e}"


def run_execution_analysis(snapshot_json: str) -> str:
    """
    Executes the deterministic StrategistEngine on a JSON representation of a MultiTimeframeSnapshot.
    Use this to run the 5-rule hierarchical protocol and obtain a precise trading signal.
    """
    from cryptonite_agents.strategist_engine import StrategistEngine
    from cryptonite_schemas.strategist import MultiTimeframeSnapshot
    
    try:
        snapshot = MultiTimeframeSnapshot.model_validate_json(snapshot_json)
        engine = StrategistEngine()
        signal = engine.analyze(snapshot)
        return signal.model_dump_json(indent=2)
    except Exception as e:
        return f"Error executing StrategistEngine: {e}"

def stage_debate_proposal(instrument_id: str, timeframe: str, recommendation: str, conviction_score: float, reasoning: str) -> str:
    """
    Stages a proposed trading strategy into the AI Brain Action Inbox for user approval.
    Use this when you have formulated a strategy that you believe should be executed.
    This routes the proposal through the Debate Pipeline and strict risk guardrails.
    """
    import json
    import os
    import urllib.error
    import urllib.request
    
    payload = {
        "symbol": instrument_id,
        "timeframe": timeframe,
        "custom_thesis": f"Recommendation: {recommendation}\nScore: {conviction_score}\nReasoning: {reasoning}"
    }
    
    data_bytes = json.dumps(payload).encode('utf-8')
    
    # Try endpoints in priority order (Env var -> Docker DNS -> Host localhost)
    candidate_urls = []
    if os.getenv("API_BASE_URL"):
        candidate_urls.append(f"{os.getenv('API_BASE_URL').rstrip('/')}/api/v1/agents/debates/trigger")
    candidate_urls.extend([
        "http://api:8000/api/v1/agents/debates/trigger",
        "http://cryptonite_api:8000/api/v1/agents/debates/trigger",
        "http://127.0.0.1:8000/api/v1/agents/debates/trigger",
        "http://localhost:8000/api/v1/agents/debates/trigger",
    ])
    
    # The trigger route requires operator auth; agents authenticate with a
    # machine token (machine_api_tokens row with the 'operator' scope).
    headers = {'Content-Type': 'application/json'}
    api_token = os.getenv("CRYPTONITE_AGENT_API_TOKEN")
    if not api_token:
        return "Error staging proposal: CRYPTONITE_AGENT_API_TOKEN is not set; the agent cannot authenticate to the API."
    headers['X-API-Token'] = api_token

    last_err = None
    for url in candidate_urls:
        try:
            req = urllib.request.Request(
                url,
                data=data_bytes,
                headers=headers,
                method='POST'
            )
            with urllib.request.urlopen(req, timeout=5.0) as response:
                res = json.loads(response.read().decode('utf-8'))
                return f"Success: Proposal staged to Action Inbox (Msg ID: {res.get('id')})"
        except urllib.error.HTTPError as e:
            err_body = e.read().decode('utf-8') if hasattr(e, 'read') else str(e)
            return f"Error staging proposal: Server returned {e.code} - {err_body}"
        except urllib.error.URLError as e:
            last_err = f"URL Error: {e.reason}"
            continue
        except Exception as e:
            last_err = str(e)
            continue
            
    return f"Error staging proposal: {last_err}"

def compare_live_vs_backtest(instrument_id: str, backtest_id: str) -> str:
    """
    Compares live trading fills from PostgreSQL against a specific Nautilus backtest.
    Use this to analyze slippage, performance delta, and drift between simulation and reality.
    """
    import json
    
    try:
        inst = instrument_id.replace("'", "''")
        pg_fills = query_postgres_readonly(f"SELECT fill_price, quantity, side, created_at FROM agent_safe_orders WHERE instrument_id = '{inst}' AND status = 'FILLED' ORDER BY created_at DESC LIMIT 50")
        bt_tearsheet = read_nautilus_tearsheet(backtest_id)
        
        try:
            live_data = json.loads(pg_fills)
        except Exception:  # not JSON: keep the tool's text
            live_data = pg_fills
            
        try:
            bt_data = json.loads(bt_tearsheet)
        except Exception:  # not JSON: keep the tool's text
            bt_data = bt_tearsheet
            
        result = {
            "live_fills_summary": live_data,
            "backtest_metrics": bt_data,
            "analysis_directive": "Compare the fill_price of the live trades against the entry_price/exit_price of the backtest. Calculate the average slippage and determine if the live execution is degrading the Sharpe Ratio."
        }
        return json.dumps(result, indent=2)
    except Exception as e:
        return f"Error comparing live vs backtest: {e}"



# --- Gateway tool registry (AI-first blueprint A1.5) ---------------------------------------------
# Every tool an agent may call, with the risk it can create. None of these changes exposure:
# stage_debate_proposal only queues a debate whose decision still needs operator approval.
from cryptonite_agents.gateway import RiskClass  # noqa: E402
from cryptonite_agents.gateway import tool as _register  # noqa: E402
from cryptonite_agents.news import get_news_sentiment  # noqa: E402

QUERY_POSTGRES = _register(RiskClass.READ)(query_postgres_readonly)
QUERY_PARQUET = _register(RiskClass.READ)(query_parquet_duckdb)
SCANNER_CONTEXT = _register(RiskClass.READ)(get_scanner_context)
LIVE_MARKET_DATA = _register(RiskClass.READ)(get_live_market_data)
GET_KLINES = _register(RiskClass.READ)(get_klines)
NEWS_SENTIMENT = _register(RiskClass.READ)(get_news_sentiment)  # A6.3: headlines + local FinBERT tags
READ_TEARSHEET = _register(RiskClass.READ)(read_nautilus_tearsheet)
EXECUTION_ANALYSIS = _register(RiskClass.READ)(run_execution_analysis)
STAGE_DEBATE_PROPOSAL = _register(RiskClass.READ, side_effects=True)(stage_debate_proposal)
COMPARE_LIVE_VS_BACKTEST = _register(RiskClass.READ)(compare_live_vs_backtest)
