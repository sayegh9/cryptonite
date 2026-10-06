import { test, expect } from "@playwright/test";
import { bucketSecondsOf, closedBarFromEvent, formatSeriesData, nextProvisional, seriesPoint } from "../src/components/chart/bars";
import closedBarEvent from "./fixtures/closed_bar_committed.json";
import { indicatorSeries, paneFor } from "../src/components/chart/indicators";

// Pure unit tests (no page) for the modules split out of TradingViewChart (R6.7).
const NS = 1_000_000_000;
const tick = (openSec: number, o: number, h: number, l: number, c: number, v: number) => ({
  bar_open_ns: openSec * NS, open: String(o), high: String(h), low: String(l), close: String(c), volume: String(v),
});

test.describe("chart modules", () => {
  test("1m ticks replace the provisional bar", () => {
    const a = nextProvisional(null, tick(600, 10, 12, 9, 11, 1), "1m");
    const b = nextProvisional(a, tick(600, 10, 13, 9, 12, 2), "1m");
    expect(b).toEqual({ time: 600, open: 10, high: 13, low: 9, close: 12, volume: 2 });
  });

  test("higher timeframes aggregate ticks inside their bucket and start a new bar at the next one", () => {
    let p = nextProvisional(null, tick(900, 10, 11, 9, 10.5, 1), "5m"); // bucket 900
    p = nextProvisional(p, tick(960, 10.5, 14, 8, 13, 2), "5m"); // same bucket
    expect(p).toEqual({ time: 900, open: 10, high: 14, low: 8, close: 13, volume: 3 });
    const q = nextProvisional(p, tick(1200, 13, 13.5, 12, 12.5, 1), "5m"); // next bucket
    expect(q).toEqual({ time: 1200, open: 13, high: 13.5, low: 12, close: 12.5, volume: 1 });
  });

  test("bucket sizes", () => {
    expect(bucketSecondsOf("1h")).toBe(3600);
    expect(bucketSecondsOf("1d")).toBe(86400);
    expect(bucketSecondsOf("weird")).toBe(60);
  });

  test("series data per style", () => {
    const bars = [{ time: 60, open: "1", high: "2", low: "0.5", close: "1.5" }];
    expect(formatSeriesData(bars, "candlestick")).toEqual([{ time: 60, open: 1, high: 2, low: 0.5, close: 1.5 }]);
    expect(formatSeriesData(bars, "line")).toEqual([{ time: 60, value: 1.5 }]);
    expect(seriesPoint(60, { open: 1, high: 2, low: 0.5, close: 1.5 }, "area")).toEqual({ time: 60, value: 1.5 });
  });

  // closed_bar_committed as the coordinator emits it (tests/test_closed_bar_event.py pins the same JSON)
  test("a committed minute replaces the provisional candle with the same open", () => {
    const openSec = closedBarEvent.bar_open_ns / NS;
    const prov = nextProvisional(null, tick(openSec, 100, 100.8, 99.2, 100.4, 2.4), "1m");
    expect(closedBarFromEvent(closedBarEvent, prov, null, "1m")).toEqual({
      time: openSec, open: 100, high: 101, low: 99, close: 100.5, volume: 2.5,
    });
  });

  test("the commit still lands after the next minute's first tick", () => {
    const openSec = closedBarEvent.bar_open_ns / NS;
    const done = nextProvisional(null, tick(openSec, 100, 101, 99, 100.5, 2.5), "1m");
    const next = nextProvisional(done, tick(openSec + 60, 100.5, 100.6, 100.4, 100.5, 0.1), "1m");
    const { open, high, low, close, volume, ...noValues } = closedBarEvent; // an event without OHLCV
    void [open, high, low, close, volume];
    expect(closedBarFromEvent(noValues, next, done, "1m")).toEqual(done);
    expect(closedBarFromEvent(noValues, next, null, "1m")).toBeNull();
  });

  test("a batch commits its last bar; old events fall back to the exclusive end", () => {
    const end = closedBarEvent.bar_open_ns + 3 * 60 * NS;
    const lastOpenSec = end / NS - 60;
    const prov = nextProvisional(null, tick(lastOpenSec, 7, 8, 6, 7.5, 1), "1m");
    const batch = { bar_open_ns: closedBarEvent.bar_open_ns, interval_end_exclusive_ns: end, last_bar_open_ns: end - 60 * NS };
    expect(closedBarFromEvent(batch, prov, null, "1m")?.time).toBe(lastOpenSec);
    expect(closedBarFromEvent({ bar_open_ns: 0, interval_end_exclusive_ns: end }, prov, null, "1m")?.time).toBe(lastOpenSec);
  });

  test("the pre-fix event (close as bar_open_ns, end one step late) never matched the candle", () => {
    const openSec = closedBarEvent.bar_open_ns / NS;
    const prov = nextProvisional(null, tick(openSec, 100, 101, 99, 100.5, 2.5), "1m");
    const closeNs = closedBarEvent.interval_end_exclusive_ns;
    expect(Math.floor(closeNs / NS)).not.toBe(prov.time); // what the old handler compared
    expect(closedBarFromEvent({ bar_open_ns: closeNs, interval_end_exclusive_ns: closeNs + 60 * NS }, prov, null, "1m")).toBeNull();
  });

  test("indicators align to the newest bars and pick their pane", () => {
    const bars = [1, 2, 3, 4, 5].map((c, i) => ({ time: i * 60, close: c, high: c, low: c, volume: 1 }));
    const sma = indicatorSeries({ id: "x", type: "SMA", period: 3, color: "#fff", pane: "main" }, bars);
    expect(sma).toEqual([{ time: 120, value: 2 }, { time: 180, value: 3 }, { time: 240, value: 4 }]);
    expect(paneFor("SMA")).toBe("main");
    expect(paneFor("RSI")).toBe("bottom");
  });
});
