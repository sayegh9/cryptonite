import { test, expect } from "@playwright/test";
import {
  CALIBRATION_MIN_POINTS,
  calibrationReadable,
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

  test("fmtQuantity uses the step size, else 4 significant digits", () => {
    // the review's raw values (BrainContextRail)
    expect(fmtQuantity("0.009399821403393336")).toBe("0.0094");
    expect(fmtQuantity("0E-18")).toBe("0");
    expect(fmtQuantity("0.009399821403393336", "0.0001")).toBe("0.0094");
    expect(fmtQuantity("0.009399821403393336", "0.00001000")).toBe("0.00940");
    expect(fmtQuantity(1.5, "0.001")).toBe("1.500");
    expect(fmtQuantity(1234.56789)).toBe("1,234.5679");
    expect(fmtQuantity(12, "1")).toBe("12");
    expect(fmtQuantity(-0.0000001234567)).toBe("-0.0000001235");
    expect(fmtQuantity("0.00004", "0.001")).toBe("0"); // rounds to zero at the step: no "-0" or "0.000"
    expect(fmtQuantity(null)).toBe("—");
    expect(fmtQuantity("not a number")).toBe("—");
  });

  test("isZeroQuantity treats 0E-18 and sub-step dust as flat", () => {
    expect(isZeroQuantity("0E-18")).toBe(true);
    expect(isZeroQuantity(0)).toBe(true);
    expect(isZeroQuantity("0.0004", "0.001")).toBe(true);
    expect(isZeroQuantity("0.0005", "0.001")).toBe(false);
    expect(isZeroQuantity("0.009399821403393336")).toBe(false);
    expect(isZeroQuantity(null)).toBe(false);
  });

  test("fmtPercentUnits formats values already in percent", () => {
    expect(fmtPercentUnits("0.1933")).toBe("0.19%");
    expect(fmtPercentUnits(6.2)).toBe("6.20%");
    expect(fmtPercentUnits("not measured")).toBe("not measured");
    expect(fmtPercentUnits(null)).toBe("—");
  });

  test("fmtSortino never prints inf", () => {
    expect(fmtSortino("inf", 0)).toBe("n/a (0 closed trades)");
    expect(fmtSortino(1.23, 0)).toBe("n/a (0 closed trades)");
    expect(fmtSortino("inf", 4)).toBe("n/a (no downside yet)");
    expect(fmtSortino(Infinity)).toBe("n/a (no downside yet)");
    expect(fmtSortino("-inf", 3)).toBe("n/a (no downside yet)");
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
  });
});
