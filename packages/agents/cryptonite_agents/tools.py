import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List

import duckdb
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
    from cryptonite_schemas.strategist import MultiTimeframeSnapshot
    from cryptonite_agents.strategist_engine import StrategistEngine
    
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
    import os
    import urllib.request
    import urllib.error
    import json
    
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
        except:
            live_data = pg_fills
            
        try:
            bt_data = json.loads(bt_tearsheet)
        except:
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
NEWS_SENTIMENT = _register(RiskClass.READ)(get_news_sentiment)  # A6.3: headlines + local FinBERT tags
READ_TEARSHEET = _register(RiskClass.READ)(read_nautilus_tearsheet)
EXECUTION_ANALYSIS = _register(RiskClass.READ)(run_execution_analysis)
STAGE_DEBATE_PROPOSAL = _register(RiskClass.READ, side_effects=True)(stage_debate_proposal)
COMPARE_LIVE_VS_BACKTEST = _register(RiskClass.READ)(compare_live_vs_backtest)
