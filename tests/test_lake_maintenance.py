"""The nightly ``lake_maintenance`` service (host-stability runbook §6): compaction, the verified quarantine prune,
lock retries, alerts to ``channel:system_alerts`` and the healthcheck. Everything runs on a lake in tmp_path; Redis
is a stand-in."""

import json
import os
import shutil
import threading
from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest
from cryptonite_schemas.bar import EnrichedBar
from cryptonite_storage import lake_compaction as core
from cryptonite_storage import lake_maintenance as lm
from cryptonite_storage.analytics_catalog import AnalyticsLakeWriter
from cryptonite_storage.lake_lock import lake_maintenance_lock
from cryptonite_storage.queries import AnalyticsQueryEngine

MIN = 60 * 10**9
DAY = 24 * 60 * MIN
D1 = 1_791_072_000 * 10**9  # 2026-10-04 00:00 UTC
NOW = datetime(2026, 10, 7, 2, 0, tzinfo=UTC)  # 10-04 .. 10-06 are closed


def bar(close_ns: int, batch: str, received_ns: int | None = None) -> EnrichedBar:
    p = Decimal("100") + Decimal(close_ns // MIN % 1000) / 4
    return EnrichedBar(
        instrument_id="BTCUSDT.BINANCE", bar_type="BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL", ingest_batch_id=batch,
        payload_checksum=f"sum-{batch}", ts_event_ns=close_ns, ts_init_ns=close_ns,
        received_at_ns=close_ns - 325_000_000 if received_ns is None else received_ns, price_precision=2,
        size_precision=5, open=p, high=p + 1, low=p - 1, close=p, volume=Decimal("1"), quote_volume=Decimal("100"),
        taker_buy_base_volume=Decimal("0"), taker_buy_quote_volume=Decimal("0"), trades_count=1, open_raw=str(p),
        high_raw=str(p + 1), low_raw=str(p - 1), close_raw=str(p), volume_raw="1",
    )


def write(lake: Path, first_close_ns: int, n: int, batch: str, **kw) -> None:
    AnalyticsLakeWriter(lake).write_batch([bar(first_close_ns + i * MIN, batch, **kw) for i in range(n)],
                                          symbol="BTCUSDT", timeframe="1m", batch_id=batch)


def part(lake: Path, day: str) -> Path:
    return lake / "binance_klines_enriched" / "binance" / "spot" / "1m" / "BTCUSDT" / f"date={day}"


def bars(lake: Path) -> list[dict]:
    return AnalyticsQueryEngine(lake).query_bars_df("BTCUSDT", "1m").to_dicts()


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    lake = tmp_path / "analytics_lake"
    for i in range(4):
        write(lake, D1 + MIN + i * 60 * MIN, 60, f"a{i}")
    for i in range(3):
        write(lake, D1 + DAY + MIN + i * 60 * MIN, 60, f"b{i}")
    return lake


@pytest.fixture
def cfg(lake: Path) -> lm.Config:
    return lm.Config(lake=lake, quarantine=lake.parent / "analytics_lake_quarantine", lock_retry_s=0, lock_retries=2)


def manifests(q: Path) -> list[Path]:
    return sorted(q.rglob("compaction-*.json"))


# --- quarantine prune -------------------------------------------------------------------------------------------


def test_nothing_is_pruned_before_14_days(cfg):
    core.run(cfg.lake, cfg.quarantine, now=NOW)
    before = sorted(p for p in cfg.quarantine.rglob("*"))
    assert core.prune_quarantine(cfg.lake, cfg.quarantine, now=NOW + timedelta(days=13)) == []
    assert sorted(p for p in cfg.quarantine.rglob("*")) == before


def test_old_quarantine_is_deleted_once_its_rows_are_proven_in_the_lake(cfg):
    core.run(cfg.lake, cfg.quarantine, now=NOW)
    want = bars(cfg.lake)
    assert len(list(cfg.quarantine.rglob("*.parquet"))) == 7
    results = core.prune_quarantine(cfg.lake, cfg.quarantine, now=NOW + timedelta(days=15))
    assert [(r.status, r.files) for r in results] == [("pruned", 4), ("pruned", 3)]
    assert list(cfg.quarantine.rglob("*")) == []  # files, manifests and the emptied folders
    assert cfg.quarantine.is_dir()  # the root stays
    assert bars(cfg.lake) == want  # the lake is untouched


def test_a_recompacted_partition_is_proven_through_the_later_manifest(cfg):
    core.run(cfg.lake, cfg.quarantine, now=NOW)
    write(cfg.lake, D1 + 5 * MIN, 2, "straggler", received_ns=D1 + 9 * MIN)
    core.run(cfg.lake, cfg.quarantine, now=NOW + timedelta(days=1))
    want = bars(cfg.lake)
    qdir = cfg.quarantine / part(cfg.lake, "2026-10-04").relative_to(cfg.lake)
    assert len(manifests(qdir)) == 2  # the first compaction's file was itself quarantined by the second
    results = core.prune_quarantine(cfg.lake, cfg.quarantine, now=NOW + timedelta(days=16))
    assert {r.status for r in results} == {"pruned"} and len(results) == 3
    assert list(cfg.quarantine.rglob("*.parquet")) == []
    assert bars(cfg.lake) == want


def test_only_the_old_manifest_is_pruned_when_the_later_one_is_recent(cfg):
    core.run(cfg.lake, cfg.quarantine, now=NOW)
    write(cfg.lake, D1 + 5 * MIN, 2, "straggler", received_ns=D1 + 9 * MIN)
    core.run(cfg.lake, cfg.quarantine, now=NOW + timedelta(days=10))
    results = core.prune_quarantine(cfg.lake, cfg.quarantine, now=NOW + timedelta(days=15))
    qdir = cfg.quarantine / part(cfg.lake, "2026-10-04").relative_to(cfg.lake)
    assert [r.status for r in results] == ["pruned", "pruned"]  # both day-1 manifests of the first run
    left = manifests(qdir)
    assert len(left) == 1 and len(list(qdir.glob("*.parquet"))) == 2  # the recent run keeps its 2 originals


def test_a_changed_compacted_file_keeps_its_quarantine(cfg):
    core.run(cfg.lake, cfg.quarantine, now=NOW)
    compacted = next(part(cfg.lake, "2026-10-04").glob("part-compacted-*.parquet"))
    duckdb.connect().execute(f"COPY (SELECT * FROM read_parquet('{compacted}') LIMIT 5) TO '{compacted}.x' "
                             "(FORMAT parquet)")
    os.replace(f"{compacted}.x", compacted)
    results = {r.manifest.parent.name: r for r in core.prune_quarantine(cfg.lake, cfg.quarantine,
                                                                        now=NOW + timedelta(days=15))}
    assert results["date=2026-10-04"].status == "unverified"
    assert "no longer matches" in results["date=2026-10-04"].detail
    assert results["date=2026-10-05"].status == "pruned"
    qdir = cfg.quarantine / part(cfg.lake, "2026-10-04").relative_to(cfg.lake)
    assert len(list(qdir.glob("*.parquet"))) == 4  # kept


def test_a_missing_compacted_file_keeps_its_quarantine(cfg):
    core.run(cfg.lake, cfg.quarantine, now=NOW)
    next(part(cfg.lake, "2026-10-05").glob("part-compacted-*.parquet")).unlink()
    results = {r.manifest.parent.name: r for r in core.prune_quarantine(cfg.lake, cfg.quarantine,
                                                                        now=NOW + timedelta(days=15))}
    assert results["date=2026-10-05"].status == "unverified"
    assert "neither in the lake nor recompacted" in results["date=2026-10-05"].detail


@pytest.mark.parametrize("tamper", ["outside", "garbage"])
def test_a_bad_manifest_deletes_nothing(cfg, tamper):
    core.run(cfg.lake, cfg.quarantine, now=NOW)
    victim = cfg.lake.parent / "victim.txt"
    victim.write_text("keep me")
    m = manifests(cfg.quarantine)[0]
    if tamper == "outside":
        data = json.loads(m.read_text())
        data["quarantined_as"].append("../victim.txt")
        m.write_text(json.dumps(data))
    else:
        m.write_text("{not json")
        os.utime(m, (0, 0))
    results = core.prune_quarantine(cfg.lake, cfg.quarantine, now=NOW + timedelta(days=15))
    bad = next(r for r in results if r.manifest == m)
    assert bad.status == "unverified"
    assert victim.read_text() == "keep me"
    assert m.exists()


# --- the nightly run --------------------------------------------------------------------------------------------


class Alerts:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def __call__(self, title, message):
        self.sent.append((title, message))


def test_next_run_is_the_next_0130_utc():
    at = dtime(1, 30)
    assert lm.next_run(datetime(2026, 10, 7, 1, 0, tzinfo=UTC), at) == datetime(2026, 10, 7, 1, 30, tzinfo=UTC)
    assert lm.next_run(datetime(2026, 10, 7, 1, 30, tzinfo=UTC), at) == datetime(2026, 10, 8, 1, 30, tzinfo=UTC)
    assert lm.next_run(datetime(2026, 10, 7, 23, 0, tzinfo=UTC), at) == datetime(2026, 10, 8, 1, 30, tzinfo=UTC)


def test_a_clean_night_compacts_prunes_and_sends_no_alert(cfg):
    want = bars(cfg.lake)
    alerts = Alerts()
    status = lm.nightly(cfg, now=lambda: NOW, publish=alerts, sleep=lambda s: True)
    assert status == "ok" and alerts.sent == []
    assert len(list(part(cfg.lake, "2026-10-04").glob("*.parquet"))) == 1
    assert bars(cfg.lake) == want
    state = lm.read_state(cfg.state_path)
    assert state["last_status"] == "ok" and state["last_summary"]["partitions"] == {"compacted": 2}
    assert lm.healthy(state, NOW)[0]
    # 15 days later the same service prunes what it quarantined
    later = NOW + timedelta(days=15)
    assert lm.nightly(cfg, now=lambda: later, publish=alerts, sleep=lambda s: True) == "ok"
    assert lm.read_state(cfg.state_path)["last_summary"]["quarantine_files_deleted"] == 7
    assert alerts.sent == []


def test_a_failed_check_alerts_and_makes_the_service_unhealthy(cfg, monkeypatch):
    real = core.fingerprint
    calls = []

    def lying(con, files):
        calls.append(1)
        fp = real(con, files)
        return {**fp, "row_hash": "0"} if len(calls) == 2 else fp

    monkeypatch.setattr(core, "fingerprint", lying)
    alerts = Alerts()
    assert lm.nightly(cfg, now=lambda: NOW, publish=alerts, sleep=lambda s: True) == "failed"
    assert len(alerts.sent) == 1 and "compaction failed" in alerts.sent[0][1]
    state = lm.read_state(cfg.state_path)
    assert "last_success_at" not in state
    assert lm.healthy(state, NOW) == (False, f"last run failed: {state['last_problem']}")


def test_a_held_lock_is_retried_then_reported(cfg):
    alerts = Alerts()
    slept: list[float] = []
    with lake_maintenance_lock(cfg.lake):  # a backfill holds it all night
        status = lm.nightly(cfg, now=lambda: NOW, publish=alerts, sleep=lambda s: slept.append(s) or True)
    assert status == "locked" and len(slept) == 2
    assert alerts.sent and "skipped tonight" in alerts.sent[0][0]
    assert len(list(part(cfg.lake, "2026-10-04").glob("*.parquet"))) == 4  # untouched


def test_the_run_goes_ahead_once_the_backfill_releases_the_lock(cfg):
    holder = lake_maintenance_lock(cfg.lake)
    holder.__enter__()

    def sleep(seconds):
        holder.__exit__(None, None, None)  # the backfill finishes during the wait
        return True

    alerts = Alerts()
    assert lm.nightly(cfg, now=lambda: NOW, publish=alerts, sleep=sleep) == "ok"
    assert alerts.sent == []


def test_stopping_during_the_wait_ends_the_night(cfg):
    with lake_maintenance_lock(cfg.lake):
        assert lm.nightly(cfg, now=lambda: NOW, publish=Alerts(), sleep=lambda s: False) == "stopped"


def test_an_unexpected_error_alerts_and_is_recorded(cfg, monkeypatch):
    monkeypatch.setattr(core, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    alerts = Alerts()
    assert lm.nightly(cfg, now=lambda: NOW, publish=alerts, sleep=lambda s: True) == "failed"
    assert "disk full" in alerts.sent[0][1]
    assert lm.read_state(cfg.state_path)["last_status"] == "failed"


def test_unproven_quarantine_alerts_but_stays_healthy(cfg):
    core.run(cfg.lake, cfg.quarantine, now=NOW)
    next(part(cfg.lake, "2026-10-05").glob("part-compacted-*.parquet")).unlink()
    alerts = Alerts()
    later = NOW + timedelta(days=15)
    assert lm.nightly(cfg, now=lambda: later, publish=alerts, sleep=lambda s: True) == "problems"
    assert "quarantine kept" in alerts.sent[0][1]
    assert lm.healthy(lm.read_state(cfg.state_path), later)[0]


# --- health, alerts, service ------------------------------------------------------------------------------------


def test_health_windows():
    start = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
    assert lm.healthy({}, start) == (False, "no state yet")
    fresh = {"started_at": start.isoformat()}
    assert lm.healthy(fresh, start + timedelta(hours=35))[0]  # a new service waits for its first night
    assert not lm.healthy(fresh, start + timedelta(hours=37))[0]
    ran = {**fresh, "last_status": "ok", "last_success_at": (start + timedelta(hours=14)).isoformat()}
    assert lm.healthy(ran, start + timedelta(hours=49))[0]
    assert not lm.healthy(ran, start + timedelta(hours=51))[0]


def test_the_check_command_reads_the_state(cfg, monkeypatch, capsys):
    monkeypatch.setenv("ANALYTICS_LAKE_PATH", str(cfg.lake))
    monkeypatch.setenv("LAKE_QUARANTINE_PATH", str(cfg.quarantine))
    assert lm.main(["--check"]) == 1
    lm.write_state(cfg.state_path, started_at=datetime.now(UTC).isoformat())
    assert lm.main(["--check"]) == 0 and "ok" in capsys.readouterr().out


def test_an_alert_reaches_the_notifier_as_a_critical_system_alert(monkeypatch):
    from cryptonite_notifier import render

    sent = []

    class FakeRedis:
        def publish(self, channel, payload):
            sent.append((channel, payload))

        def close(self):
            pass

    import redis

    monkeypatch.setattr(redis.Redis, "from_url", classmethod(lambda cls, url, **kw: FakeRedis()))
    lm.redis_publisher("redis://example:6379/0")("Lake maintenance needs a look", "compaction failed: x")
    channel, payload = sent[0]
    alert = json.loads(payload)
    assert channel == "channel:system_alerts" and alert["severity"] == "CRITICAL"  # what on_system_alert forwards
    title, body = render.alert_text(alert)
    assert title == "Lake Maintenance" and "compaction failed: x" in body


def test_a_dead_redis_never_stops_maintenance():
    lm.redis_publisher("redis://127.0.0.1:1/9")("t", "m")  # refused: logged, not raised
    lm.redis_publisher(None)("t", "m")


def test_the_service_records_its_start_and_stops_on_request(cfg):
    stop = threading.Event()
    stop.set()
    lm.serve(cfg, Alerts(), stop)
    assert "started_at" in lm.read_state(cfg.state_path)


def test_config_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ANALYTICS_LAKE_PATH", str(tmp_path / "lake"))
    monkeypatch.delenv("LAKE_QUARANTINE_PATH", raising=False)
    monkeypatch.setenv("LAKE_MAINTENANCE_AT", "02:15")
    c = lm.Config.from_env()
    assert c.quarantine == tmp_path / "analytics_lake_quarantine" and c.at == dtime(2, 15) and c.keep_days == 14


def test_copying_a_lake_keeps_proof_valid(cfg, tmp_path):
    """A restored backup (lake + quarantine copied elsewhere) still proves itself: nothing depends on inodes."""
    core.run(cfg.lake, cfg.quarantine, now=NOW)
    root = tmp_path / "restored"
    shutil.copytree(cfg.lake, root / "analytics_lake")
    shutil.copytree(cfg.quarantine, root / "analytics_lake_quarantine")
    results = core.prune_quarantine(root / "analytics_lake", root / "analytics_lake_quarantine",
                                    now=NOW + timedelta(days=15))
    assert {r.status for r in results} == {"pruned"}
