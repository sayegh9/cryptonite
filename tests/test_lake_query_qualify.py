"""query_bars_df keeps one row per bar with ``row_number() ... QUALIFY`` instead of ``DISTINCT ON`` (host stability,
docs/runbooks/host-stability.md §6). DISTINCT ON cannot spill to disk, so the full-history read failed at 1.6 GB.

The pick must not change: every tie-break branch is seeded here (an open-stamped REST row, a row received while its
kline was still open, a later receipt, a higher batch id, a higher checksum, a later ts_event_ns), spread over
several files written in an order that does not favour the winner, and the result is checked both by hand and
against the old DISTINCT ON query run on the same files. Also: the read retries once when a file vanishes between
DuckDB's glob and its open, and the DuckDB settings come from the environment only when set.
"""

import random
from decimal import Decimal
from pathlib import Path

import duckdb
import polars as pl
import pytest
from cryptonite_schemas.bar import EnrichedBar
from cryptonite_storage.analytics_catalog import AnalyticsLakeWriter
from cryptonite_storage.queries import (
    EARLY_RECEIPT_TOLERANCE_NS,
    AnalyticsQueryEngine,
    open_stamped_receipt_lag_ns,
    timeframe_ns,
)

MIN = 60 * 10**9
T0 = 1_791_104_400 * 10**9  # 2026-10-04 09:00:00 UTC
TF_NS = timeframe_ns("1m")


def old_distinct_on_query(glob_path: str, start_ns: int | None = None, end_ns: int | None = None) -> str:
    """query_bars_df's SQL before the QUALIFY rewrite, verbatim apart from the window."""
    where = [f"ts_event_ns >= {start_ns}"] if start_ns is not None else []
    where += [f"ts_event_ns <= {end_ns}"] if end_ns is not None else []
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    return f"""
        SELECT DISTINCT ON (ts_init_ns) *, ts_event_ns::BIGINT - {TF_NS} AS bar_open_ns
        FROM read_parquet('{glob_path}')
        {where_sql}
        ORDER BY ts_init_ns ASC,
            CASE
                WHEN received_at_ns::BIGINT - ts_event_ns::BIGINT = {open_stamped_receipt_lag_ns(TF_NS)} THEN 2
                WHEN received_at_ns::BIGINT < ts_event_ns::BIGINT - {EARLY_RECEIPT_TOLERANCE_NS} THEN 1
                ELSE 0
            END ASC,
            ts_event_ns DESC, received_at_ns DESC, ingest_batch_id DESC, payload_checksum DESC
    """


def row(close_ns: int, label: str, *, received_ns: int | None = None, event_ns: int | None = None,
        batch: str = "batch-0", checksum: str = "sum-0") -> EnrichedBar:
    """A lake row for the bar closing at ``close_ns`` (its ts_init_ns); ``label`` rides in ``close_raw``."""
    price = Decimal("100.5")
    return EnrichedBar(
        instrument_id="BTCUSDT.BINANCE", bar_type="BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL",
        payload_checksum=checksum, ingest_batch_id=batch,
        ts_event_ns=close_ns if event_ns is None else event_ns, ts_init_ns=close_ns,
        received_at_ns=close_ns - 325_000_000 if received_ns is None else received_ns,
        price_precision=2, size_precision=5, open=price, high=price, low=price, close=price,
        volume=Decimal("1"), quote_volume=Decimal("1"), taker_buy_base_volume=Decimal("0"),
        taker_buy_quote_volume=Decimal("0"), trades_count=1,
        open_raw="100.5", high_raw="100.5", low_raw="100.5", close_raw=label, volume_raw="1",
    )


def c(i: int) -> int:
    return T0 + (i + 1) * MIN


# Each group: the bar's close, its candidate rows (winner labelled "win"), and the branch that decides it.
GROUPS = {
    "early receipt beats open-stamped": [
        row(c(0), "lose-open-stamped", received_ns=c(0) + open_stamped_receipt_lag_ns(TF_NS)),
        row(c(0), "win", received_ns=c(0) - EARLY_RECEIPT_TOLERANCE_NS - 1),
    ],
    "on-time receipt beats early receipt": [
        row(c(1), "lose-early", received_ns=c(1) - 30 * 10**9),
        row(c(1), "win", received_ns=c(1) - 325_000_000),
    ],
    "on-time beats open-stamped although received earlier": [
        row(c(2), "lose-open-stamped", received_ns=c(2) + open_stamped_receipt_lag_ns(TF_NS)),
        row(c(2), "win", received_ns=c(2) + 1),
    ],
    "the receipt just inside the tolerance is on time": [
        row(c(3), "lose-early", received_ns=c(3) - EARLY_RECEIPT_TOLERANCE_NS - 1),
        row(c(3), "win", received_ns=c(3) - EARLY_RECEIPT_TOLERANCE_NS),
    ],
    "later ts_event_ns": [
        row(c(4), "lose-earlier-event", event_ns=c(4) - 1, received_ns=c(4) + 10**9),
        row(c(4), "win", received_ns=c(4) + 10**9),
    ],
    "later receipt": [
        row(c(5), "lose-earlier-receipt", received_ns=c(5) + 1 * 10**9),
        row(c(5), "win", received_ns=c(5) + 5 * 10**9),
        row(c(5), "lose-earliest-receipt", received_ns=c(5) - 10**9),
    ],
    "higher batch id": [
        row(c(6), "lose-batch-a", batch="batch-a"),
        row(c(6), "win", batch="batch-b"),
    ],
    "higher checksum": [
        row(c(7), "lose-sum-1", batch="batch-x", checksum="sum-1"),
        row(c(7), "win", batch="batch-x", checksum="sum-2"),
    ],
    "two early receipts: the later one": [
        row(c(8), "lose-early", received_ns=c(8) - 50 * 10**9),
        row(c(8), "win", received_ns=c(8) - 20 * 10**9),
    ],
    "a single row": [row(c(9), "win")],
}


@pytest.fixture
def tied_lake(tmp_path: Path) -> Path:
    """Every group's rows spread over three files, each file holding one candidate per group, losers first."""
    files: list[list[EnrichedBar]] = [[], [], []]
    for rows in GROUPS.values():
        ordered = sorted(rows, key=lambda r: r.close_raw == "win")  # the winner lands in the last file it can
        for i, r in enumerate(ordered):
            files[i].append(r)
    writer = AnalyticsLakeWriter(tmp_path)
    for i, batch in enumerate(files):
        if batch:
            writer.write_batch(sorted(batch, key=lambda r: r.ts_init_ns), symbol="BTCUSDT", timeframe="1m",
                               batch_id=f"file-{i}")
    return tmp_path


def test_every_tie_break_branch_picks_the_same_row_as_before(tied_lake):
    engine = AnalyticsQueryEngine(tied_lake)
    df = engine.query_bars_df("BTCUSDT", "1m")
    assert df.height == len(GROUPS)
    assert df["ts_init_ns"].to_list() == sorted(df["ts_init_ns"].to_list())  # output order: by bar
    assert df["close_raw"].to_list() == ["win"] * len(GROUPS), dict(zip(GROUPS, df["close_raw"].to_list()))
    assert (df["bar_open_ns"] == df["ts_event_ns"] - TF_NS).all()

    old = pl.from_arrow(duckdb.connect().execute(old_distinct_on_query(engine._get_glob_path("BTCUSDT"))).to_arrow_table())
    assert df.to_dicts() == old.to_dicts()


def test_a_window_picks_the_same_rows_as_before(tied_lake):
    engine = AnalyticsQueryEngine(tied_lake)
    start, end = c(2), c(6)
    df = engine.query_bars_df("BTCUSDT", "1m", start_ns=start, end_ns=end)
    old = duckdb.connect().execute(old_distinct_on_query(engine._get_glob_path("BTCUSDT"), start, end)).to_arrow_table()
    assert df.to_dicts() == pl.from_arrow(old).to_dicts()
    assert df.height == 5 and set(df["close_raw"].to_list()) == {"win"}


def test_random_duplicates_pick_the_same_rows_as_before(tmp_path):
    """Hundreds of bars with 1-5 candidates each, drawn from every branch, never tied on the full key."""
    rng = random.Random(20261020)
    kinds = ["on_time", "late", "early", "open_stamped"]
    batches: list[list[EnrichedBar]] = [[] for _ in range(6)]
    n = 0
    for i in range(400):
        close = c(i)
        for _ in range(rng.randint(1, 5)):
            kind = rng.choice(kinds)
            received = {
                "on_time": close - rng.randint(0, EARLY_RECEIPT_TOLERANCE_NS),
                "late": close + rng.randint(1, 120) * 10**9,
                "early": close - EARLY_RECEIPT_TOLERANCE_NS - rng.randint(1, 50) * 10**9,
                "open_stamped": close + open_stamped_receipt_lag_ns(TF_NS),
            }[kind]
            if rng.random() < 0.3:  # collide on the receipt, so batch id and checksum decide
                received = close
            n += 1
            batches[rng.randrange(6)].append(row(
                close, f"r{n}", received_ns=received,
                event_ns=close - rng.choice([0, 0, 0, 1]),
                batch=f"batch-{rng.randint(0, 2)}", checksum=f"sum-{n:05d}",  # unique: no full-key ties
            ))
    writer = AnalyticsLakeWriter(tmp_path)
    for i, b in enumerate(batches):
        writer.write_batch(sorted(b, key=lambda r: r.ts_init_ns), symbol="BTCUSDT", timeframe="1m", batch_id=f"rand-{i}")

    engine = AnalyticsQueryEngine(tmp_path)
    df = engine.query_bars_df("BTCUSDT", "1m")
    old = duckdb.connect().execute(old_distinct_on_query(engine._get_glob_path("BTCUSDT"))).to_arrow_table()
    assert df.height == 400
    assert df.to_dicts() == pl.from_arrow(old).to_dicts()


# --- a file that vanishes between glob and open ---------------------------------------------------------------


class _FlakyCursor:
    def __init__(self, real, failures: list[Exception]):
        self.real, self.failures, self.calls = real, failures, 0

    def execute(self, query):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return self.real.execute(query)

    def close(self):
        self.real.close()


class _FlakyConnection:
    def __init__(self, real, failures: list[Exception]):
        self.real, self.failures, self.cursors = real, failures, []

    def cursor(self):
        cur = _FlakyCursor(self.real.cursor(), self.failures)
        self.cursors.append(cur)
        return cur


def _moved() -> duckdb.IOException:
    return duckdb.IOException('IO Error: Cannot open file "part-x.parquet": No such file or directory')


def test_a_file_moved_between_glob_and_open_is_read_again(tied_lake):
    engine = AnalyticsQueryEngine(tied_lake)
    engine._con = _FlakyConnection(engine._con, [_moved()])
    df = engine.query_bars_df("BTCUSDT", "1m")
    assert df.height == len(GROUPS)
    assert engine._con.cursors[0].calls == 2


def test_two_failed_reads_return_an_empty_frame(tied_lake, caplog):
    engine = AnalyticsQueryEngine(tied_lake)
    engine._con = _FlakyConnection(engine._con, [_moved(), _moved()])
    assert engine.query_bars_df("BTCUSDT", "1m").is_empty()
    assert engine._con.cursors[0].calls == 2
    assert any("failed twice" in r.getMessage() for r in caplog.records)


def test_an_empty_lake_is_not_retried(tmp_path):
    engine = AnalyticsQueryEngine(tmp_path)
    engine._con = _FlakyConnection(engine._con, [])
    assert engine.query_bars_df("BTCUSDT", "1m").is_empty()
    assert engine._con.cursors[0].calls == 1


def test_other_duckdb_errors_still_raise(tied_lake):
    engine = AnalyticsQueryEngine(tied_lake)
    engine._con = _FlakyConnection(engine._con, [duckdb.OutOfMemoryException("Out of Memory Error: could not allocate")])
    with pytest.raises(duckdb.OutOfMemoryException):
        engine.query_bars_df("BTCUSDT", "1m")


# --- DuckDB settings from the environment ---------------------------------------------------------------------


def _settings(engine: AnalyticsQueryEngine) -> dict[str, str]:
    names = ("threads", "memory_limit", "temp_directory")
    vals = engine._con.execute(f"SELECT {', '.join(f'current_setting({n!r})' for n in names)}").fetchone()
    return dict(zip(names, map(str, vals)))


def test_unset_env_keeps_duckdb_defaults(tmp_path, monkeypatch):
    for k in ("DUCKDB_THREADS", "DUCKDB_MEMORY_LIMIT", "DUCKDB_TEMP_DIR"):
        monkeypatch.delenv(k, raising=False)
    plain = duckdb.connect(":memory:")
    want = dict(zip(("threads", "memory_limit", "temp_directory"), map(str, plain.execute(
        "SELECT current_setting('threads'), current_setting('memory_limit'), current_setting('temp_directory')"
    ).fetchone())))
    assert _settings(AnalyticsQueryEngine(tmp_path)) == want
    monkeypatch.setenv("DUCKDB_THREADS", "")  # empty counts as unset
    monkeypatch.setenv("DUCKDB_MEMORY_LIMIT", " ")
    assert _settings(AnalyticsQueryEngine(tmp_path)) == want


def test_env_sets_threads_memory_limit_and_spill_dir(tmp_path, monkeypatch, tied_lake):
    spill = tmp_path / "spill"
    monkeypatch.setenv("DUCKDB_THREADS", "3")
    monkeypatch.setenv("DUCKDB_MEMORY_LIMIT", "512MB")
    monkeypatch.setenv("DUCKDB_TEMP_DIR", str(spill))
    engine = AnalyticsQueryEngine(tied_lake)
    s = _settings(engine)
    assert s["threads"] == "3"
    assert s["memory_limit"] in ("512.0 MiB", "488.2 MiB", "512.0 MB", "488.2 MB")  # 512MB, as DuckDB prints it
    assert s["temp_directory"] == str(spill)
    # the settings are database-wide: the per-call cursor sees them too
    cur = engine._con.cursor()
    assert cur.execute("SELECT current_setting('threads')").fetchone()[0] == 3
    assert engine.query_bars_df("BTCUSDT", "1m").height == len(GROUPS)


def test_arguments_override_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DUCKDB_THREADS", "3")
    assert _settings(AnalyticsQueryEngine(tmp_path, threads=2))["threads"] == "2"


@pytest.mark.parametrize("bad", ["0", "-1", "eight"])
def test_a_bad_thread_count_is_refused(tmp_path, monkeypatch, bad):
    monkeypatch.setenv("DUCKDB_THREADS", bad)
    with pytest.raises(ValueError):
        AnalyticsQueryEngine(tmp_path)
