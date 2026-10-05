import { test, expect } from "@playwright/test";
import { bucketSecondsOf, formatSeriesData, nextProvisional, seriesPoint } from "../src/components/chart/bars";
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

  test("indicators align to the newest bars and pick their pane", () => {
    const bars = [1, 2, 3, 4, 5].map((c, i) => ({ time: i * 60, close: c, high: c, low: c, volume: 1 }));
    const sma = indicatorSeries({ id: "x", type: "SMA", period: 3, color: "#fff", pane: "main" }, bars);
    expect(sma).toEqual([{ time: 120, value: 2 }, { time: 180, value: 3 }, { time: 240, value: 4 }]);
    expect(paneFor("SMA")).toBe("main");
    expect(paneFor("RSI")).toBe("bottom");
  });
});
