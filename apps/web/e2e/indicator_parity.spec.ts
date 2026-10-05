import { test, expect } from "@playwright/test";
import fixture from "./fixtures/indicator_parity.json";
import { indicatorSeries, paneFor, type IndicatorType } from "../src/components/chart/indicators";

// R7.2: the chart (technicalindicators) and the scanner (Polars, TA-Lib conventions) must agree on the
// indicators they share. The fixture holds the scanner's values for the same bars; regenerate it with
// `uv run python scripts/build_indicator_parity_fixture.py`. Compared after a warm-up of 5 periods,
// where seeding conventions (e.g. EMA's first value) no longer matter. technicalindicators rounds RSI to
// 2 decimals, so RSI is compared to that rounding; the others to 1e-6 relative.
const tolerance = (type: IndicatorType, want: number) => (type === "RSI" ? 0.005 : 1e-6 * Math.max(1, Math.abs(want)));
const SHARED: IndicatorType[] = ["SMA", "EMA", "RSI", "ATR", "SD"];
const { period, bars, scanner } = fixture as {
  period: number; bars: Record<string, number>[]; scanner: Record<string, (number | null)[]>;
};

test.describe("chart and scanner indicator parity", () => {
  for (const type of SHARED) {
    test(`${type} matches the scanner`, () => {
      const points = indicatorSeries({ id: type, type, period, color: "#fff", pane: paneFor(type) }, bars);
      expect(points).not.toBeNull();
      const chart = points!.map((p) => p.value);
      const expected = scanner[type];
      const offset = bars.length - chart.length;
      let compared = 0;
      for (let i = 5 * period; i < bars.length; i++) {
        const want = expected[i];
        if (want === null || i < offset) continue;
        const got = chart[i - offset];
        expect(Math.abs(got - want), `${type} at bar ${i}: chart ${got} vs scanner ${want}`)
          .toBeLessThanOrEqual(tolerance(type, want));
        compared++;
      }
      expect(compared).toBeGreaterThan(200);
    });
  }
});
