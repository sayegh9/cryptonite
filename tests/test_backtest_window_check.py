"""A backtest window the catalog cannot supply is refused before it is queued (pure checks, no catalog)."""

from cryptonite_backtester.coverage import instrument_defined, window_problem

HOUR_NS = 3_600 * 10**9
DAY_NS = 24 * HOUR_NS
BAR = "XRPUSDT.BINANCE-1-HOUR-LAST-EXTERNAL"
INST = "XRPUSDT.BINANCE"
START = 1_000 * DAY_NS
END = START + 30 * DAY_NS


def _problem(**over):
    args = dict(instrument_id=INST, bar_type=BAR, instrument_defined=True, history=(START - 5 * DAY_NS, END),
                step_ns=HOUR_NS, window_start_ns=START, window_end_ns=END)
    args.update(over)
    return window_problem(**args)


def test_covered_window_passes():
    assert _problem() is None


def test_newest_bar_may_lag_the_window_end_by_one_step():
    assert _problem(history=(START, END - HOUR_NS)) is None


def test_missing_instrument_definition_is_refused():
    assert "no instrument definition" in _problem(instrument_defined=False)


def test_no_bars_at_all_is_refused():
    assert "no bars" in _problem(history=None)


def test_window_starting_before_the_history_is_refused():
    assert "start" in _problem(history=(START + DAY_NS, END))


def test_window_ending_after_the_history_is_refused():
    assert "end" in _problem(history=(START, END - 3 * DAY_NS))


def test_instrument_defined_needs_a_parquet_definition(tmp_path):
    assert not instrument_defined(tmp_path, INST)
    folder = tmp_path / "data" / "instruments" / INST
    folder.mkdir(parents=True)
    assert not instrument_defined(tmp_path, INST)  # empty folder is not a definition
    (folder / "2026-10-11.parquet").write_bytes(b"x")
    assert instrument_defined(tmp_path, INST)
