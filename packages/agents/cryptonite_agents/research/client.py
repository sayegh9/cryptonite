"""HTTP client for the research loop: strategies and backtests go through the API.

The API authenticates the agent (machine token, ``operator`` scope), validates generated code, and
enqueues backtests that the backtester runs in its sandbox. The research loop never touches the
database or Nautilus directly.
"""

import asyncio
import os
import time
from typing import Any

import httpx

TERMINAL_STATUSES = {"COMPLETED", "FAILED", "INTEGRITY_FAILED", "CANCELLED"}


class ResearchApiError(RuntimeError):
    pass


class ResearchApiClient:
    def __init__(self, base_url: str | None = None, token: str | None = None,
                 http: httpx.AsyncClient | None = None, poll_s: float = 3.0) -> None:
        self.base_url = (base_url or os.getenv("API_BASE_URL", "http://api:8000")).rstrip("/")
        token = token if token is not None else os.getenv("CRYPTONITE_AGENT_API_TOKEN", "")
        self.http = http or httpx.AsyncClient(timeout=30.0)
        self.headers = {"X-API-Token": token} if token else {}
        self.poll_s = poll_s

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        resp = await self.http.request(method, f"{self.base_url}/api/v1{path}", headers=self.headers, **kwargs)
        if resp.status_code >= 400:
            raise ResearchApiError(f"{method} {path} -> {resp.status_code}: {resp.text[:500]}")
        return resp.json()

    async def save_report(self, report: dict[str, Any]) -> dict[str, Any]:
        """Store a research report in the API's queryable history (LE-6); idempotent per report id."""
        body = {k: report.get(k) for k in ("id", "idea", "status", "reason", "strategy_slug", "strategy_name",
                                           "in_sample", "out_of_sample", "cost_usd", "created_at",
                                           "folds", "dsr", "holdout", "robustness")}
        body["report"] = report
        return await self._request("POST", "/research/reports", json=body)

    async def list_reports(self, hours: int = 24) -> list[dict[str, Any]]:
        return (await self._request("GET", "/research/reports", params={"hours": hours}))["reports"]

    async def trials_tested(self, instrument_id: str, timeframe: str, days: int) -> int:
        """Parameter trials earlier research ran on this instrument/timeframe (feeds the DSR, R5.4)."""
        return int((await self._request("GET", "/research/trials", params={
            "instrument_id": instrument_id, "timeframe": timeframe, "days": days}))["trials"])

    async def coverage(self, bar_type: str) -> dict[str, Any]:
        """Contiguous catalog history for a bar type: {"bar_type", "start_ns", "end_ns"} (R5.0)."""
        return await self._request("GET", "/backtests/coverage", params={"bar_type": bar_type})

    async def save_strategy(self, name: str, description: str, python_code: str) -> dict[str, Any]:
        return await self._request("POST", "/strategies", json={
            "name": name, "description": description, "python_code": python_code,
        })

    async def submit_backtest(self, slug: str, instrument_id: str, bar_type: str, timeframe: str,
                              parameters: dict[str, Any], window: tuple[int, int, int],
                              execution_profile: str = "conservative") -> str:
        warmup, start, end = window
        run = await self._request("POST", "/backtests", json={
            "strategy_slug": slug, "instrument_id": instrument_id, "bar_type": bar_type,
            "timeframe": timeframe, "parameters": parameters, "warmup_start_ns": warmup,
            "evaluation_start_ns": start, "evaluation_end_ns": end, "execution_profile": execution_profile,
        })
        return run["id"]

    async def wait(self, run_id: str, timeout_s: float = 1200) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        while True:
            info = await self._request("GET", f"/backtests/{run_id}")
            if info.get("status") in TERMINAL_STATUSES:
                return info
            if time.monotonic() > deadline:
                raise ResearchApiError(f"backtest {run_id} still {info.get('status')} after {timeout_s:.0f} s")
            await asyncio.sleep(self.poll_s)

    async def metrics(self, run_id: str) -> dict[str, Any]:
        return (await self._request("GET", f"/backtests/{run_id}/results"))["metrics"]

    async def backtest(self, slug: str, instrument_id: str, bar_type: str, timeframe: str,
                       parameters: dict[str, Any], window: tuple[int, int, int]) -> dict[str, Any]:
        """Submit, wait and return metrics; raises ResearchApiError when the run did not complete."""
        run_id = await self.submit_backtest(slug, instrument_id, bar_type, timeframe, parameters, window)
        info = await self.wait(run_id)
        if info.get("status") != "COMPLETED":
            raise ResearchApiError(f"backtest {run_id} {info.get('status')}: {info.get('error_message') or ''}")
        return {"run_id": run_id, **await self.metrics(run_id)}
