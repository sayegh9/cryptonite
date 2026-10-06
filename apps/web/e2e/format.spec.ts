import { test, expect } from "@playwright/test";
import {
  CALIBRATION_MIN_POINTS,
  calibrationReadable,
  calibrationThreshold,
  fmtGateActual,
  fmtPercentUnits,
  fmtQuantity,
  fmtSortino,
  fmtSortinoVsHold,
  isZeroQuantity,
  stepDecimals,
} from "../src/lib/format";

// G4: unit tests of the shared formatting helpers (no browser; the web has no separate unit runner).

test.describe("format helpers (G4)", () => {
  test("stepDecimals reads the step size's decimals", () => {
    expect(stepDecimals("0.00001000")).toBe(5);
    expect(stepDecimals("0.001")).toBe(3);
    expect(stepDecimals("1E-8")).toBe(8);
    expect(stepDecimals("0.5")).toBe(1);
    expect(stepDecimals(0.00025)).toBe(5);
    expect(stepDecimals("1")).toBe(0);
    expect(stepDecimals("10")).toBe(0);
    expect(stepDecimals("0")).toBeNull();
    expect(stepDecimals(null)).toBeNull();
    expect(stepDecimals("abc")).toBeNull();
  });

  test("fmtQuantity truncates toward zero: never overstates a position", () => {
    // the review's raw values (BrainContextRail)
    expect(fmtQuantity("0.009399821403393336")).toBe("0.009399");
    expect(fmtQuantity("0E-18")).toBe("0");
    expect(fmtQuantity("0.009399821403393336", "0.0001")).toBe("0.0093");
    expect(fmtQuantity("0.009399821403393336", "0.00001000")).toBe("0.00939"); // what the venue can close
    expect(fmtQuantity(1.5, "0.001")).toBe("1.500");
    expect(fmtQuantity(0.29, "0.01")).toBe("0.29"); // no binary-float off-by-one
    expect(fmtQuantity(1234.56789)).toBe("1,234.5678");
    expect(fmtQuantity(0.99996)).toBe("0.9999");
    expect(fmtQuantity(12, "1")).toBe("12");
    expect(fmtQuantity(-0.0000001234567)).toBe("-0.0000001234");
    expect(fmtQuantity(null)).toBe("—");
    expect(fmtQuantity(" ")).toBe("—");
    expect(fmtQuantity("not a number")).toBe("—");
  });

  test("fmtQuantity prints a held amount below one step as <step, never 0", () => {
    expect(fmtQuantity("0.00004", "0.001")).toBe("<0.001");
    expect(fmtQuantity("0.0005", "0.001")).toBe("<0.001"); // was rounded up to 0.001 (2x)
    expect(fmtQuantity(5e-9, "1E-8")).toBe("<0.00000001");
    expect(fmtQuantity(0.4, "1")).toBe("<1");
    expect(fmtQuantity(-0.00004, "0.001")).toBe(">-0.001");
  });

  test("ledger quantities keep 6 decimals, truncated", () => {
    expect(fmtQuantity(0.1234567891, null, { maxFractionDigits: 6 })).toBe("0.123456");
    expect(fmtQuantity(0.03, null, { maxFractionDigits: 6 })).toBe("0.03");
    expect(fmtQuantity(1234.9999999, null, { maxFractionDigits: 6 })).toBe("1,234.999999");
    expect(fmtQuantity(1e-7, null, { maxFractionDigits: 6 })).toBe("<0.000001");
  });

  test("isZeroQuantity: only true zero (1e-12), independent of step; unreadable is not zero", () => {
    expect(isZeroQuantity("0E-18")).toBe(true);
    expect(isZeroQuantity(0)).toBe(true);
    expect(isZeroQuantity("0.0004")).toBe(false); // dust below a 0.001 step is still held
    expect(isZeroQuantity("0.009399821403393336")).toBe(false);
    expect(isZeroQuantity(null)).toBe(false);
    expect(isZeroQuantity(undefined)).toBe(false);
    expect(isZeroQuantity(" ")).toBe(false);
    expect(isZeroQuantity("abc")).toBe(false);
  });

  test("fmtPercentUnits formats values already in percent", () => {
    expect(fmtPercentUnits("0.1933")).toBe("0.19%");
    expect(fmtPercentUnits(6.2)).toBe("6.20%");
    expect(fmtPercentUnits("not measured")).toBe("not measured");
    expect(fmtPercentUnits(null)).toBe("—");
    expect(fmtPercentUnits(" ")).toBe("—");
    expect(fmtPercentUnits("-0.004")).toBe("0.00%"); // never "-0.00%"
    expect(fmtPercentUnits("-1.5")).toBe("-1.50%");
  });

  test("fmtSortino never prints inf", () => {
    expect(fmtSortino("inf", 0)).toBe("n/a (0 closed trades)");
    expect(fmtSortino(1.23, 0)).toBe("n/a (0 closed trades)");
    expect(fmtSortino("inf", 4)).toBe("n/a (no downside yet)");
    expect(fmtSortino(Infinity)).toBe("n/a (no downside yet)");
    expect(fmtSortino("-inf", 3)).toBe("n/a (not finite)");
    expect(fmtSortino(-Infinity, 3)).toBe("n/a (not finite)");
    expect(fmtSortino(" ", 5)).toBe("not measured"); // blank is unknown, not 0
    expect(fmtSortino("-0.001", 5)).toBe("0.00");
    expect(fmtSortino("1.8", 30)).toBe("1.80");
    expect(fmtSortino("not measured", 30)).toBe("not measured");
    expect(fmtSortino(null, 30)).toBe("not measured");
  });

  test("fmtSortinoVsHold keeps the benchmark side independent of closed trades", () => {
    expect(fmtSortinoVsHold("inf vs not measured", 0)).toBe("n/a (0 closed trades) vs not measured");
    expect(fmtSortinoVsHold("inf vs 0.90", 5)).toBe("n/a (no downside yet) vs 0.90");
    expect(fmtSortinoVsHold("1.80 vs 0.90", 41)).toBe("1.80 vs 0.90");
    expect(fmtSortinoVsHold("1.80 vs 0.90", undefined)).toBe("1.80 vs 0.90");
  });

  test("fmtGateActual adds units per check", () => {
    const m = { closed_trades: 0 };
    expect(fmtGateActual("max_drawdown_pct", "0.1933", m)).toBe("0.19%");
    expect(fmtGateActual("veto_rate_pct", "12.5", m)).toBe("12.50%");
    expect(fmtGateActual("sortino_vs_btc_hold", "inf vs not measured", m)).toBe("n/a (0 closed trades) vs not measured");
    expect(fmtGateActual("days_at_level", "4.2 d", m)).toBe("4.2 d");
    expect(fmtGateActual("max_drawdown_pct", "not measured", null)).toBe("not measured");
  });

  test("calibration needs 20 points", () => {
    expect(CALIBRATION_MIN_POINTS).toBe(20);
    expect(calibrationReadable(6)).toBe(false);
    expect(calibrationReadable(19)).toBe(false);
    expect(calibrationReadable(20)).toBe(true);
    expect(calibrationReadable(null)).toBe(false);
    // never readable before the server's ECE min_samples either
    expect(calibrationThreshold(30)).toBe(30);
    expect(calibrationThreshold(5)).toBe(20);
    expect(calibrationReadable(25, 30)).toBe(false);
    expect(calibrationReadable(20, 5)).toBe(true);
  });
});
