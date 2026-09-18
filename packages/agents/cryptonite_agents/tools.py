import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List

import duckdb
from sqlalchemy import create_engine, text

logger = logging.getLogger(__name__)

# Configured to use the explicit read-only role injected via docker-compose
DB_URL = os.getenv("DATABASE_URL", "postgresql://cryptonite_agent_ro:agent_ro_pass@postgres:5432/cryptonite")
CATALOG_PATH = os.getenv("NAUTILUS_CATALOG_PATH", "/app/data/nautilus_catalog")

_engine = None

def get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(DB_URL)
    return _engine

def query_postgres_readonly(sql: str) -> str:
    """Executes a SELECT query against the PostgreSQL database.
    
    The connection enforces default_transaction_read_only = 'on' via RBAC.
    """
    logger.info("Agent executing DB query: %s", sql)
    try:
        with get_engine().connect() as conn:
            result = conn.execute(text(sql))
            rows = [dict(row._mapping) for row in result]
            return json.dumps(rows, default=str)
    except Exception as e:
        return f"Database Error: {e}"

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
                if not resolved_literal.startswith(str(Path(CATALOG_PATH).resolve())):
                    return f"Security Error: Unauthorized file access attempted outside catalog path. Blocked: {literal}"
        
        result = duckdb.query(full_query).df()
        return result.to_json(orient="records")
    except Exception as e:
        return f"DuckDB Error: {e}"

def read_nautilus_tearsheet(backtest_id: str) -> str:
    """Reads the JSON tearsheet for a specific backtest_id."""
    tearsheet_path = Path(f"/app/data/backtest_artifacts/{backtest_id}/tearsheet.json")
    if not tearsheet_path.exists():
        return f"Error: Tearsheet for {backtest_id} not found."
    try:
        return tearsheet_path.read_text()
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
        return f"Analysis Error: {e}"
