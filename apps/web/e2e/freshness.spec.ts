import { test, expect } from "@playwright/test";
import { ageLabel, asOfMs, freshness, knownPositive } from "../src/lib/freshness";

// Pure unit tests (no page): the shared freshness rules behind <Fresh>.
test.describe("freshness rules", () => {
  const now = 1_760_000_000_000;

  test("unknown without a valid observation time", () => {
    expect(freshness(null, 5000, now)).toBe("unknown");
    expect(freshness(undefined, 5000, now)).toBe("unknown");
    expect(freshness("", 5000, now)).toBe("unknown");
    expect(freshness("not a date", 5000, now)).toBe("unknown");
    expect(freshness(0, 5000, now)).toBe("unknown");
  });

  test("live, delayed and stale by age", () => {
    expect(freshness(now - 1000, 5000, now)).toBe("live");
    expect(freshness(now - 5000, 5000, now)).toBe("live");
    expect(freshness(now - 5001, 5000, now)).toBe("delayed");
    expect(freshness(now - 15000, 5000, now)).toBe("delayed");
    expect(freshness(now - 15001, 5000, now)).toBe("stale");
  });

  test("accepts seconds, ISO strings and Dates", () => {
    expect(asOfMs(1_760_000_000)).toBe(1_760_000_000_000);
    expect(freshness(new Date(now - 1000).toISOString(), 5000, now)).toBe("live");
    expect(freshness(new Date(now - 60_000), 5000, now)).toBe("stale");
  });

  test("a future timestamp counts as live, not negative age", () => {
    expect(freshness(now + 10_000, 5000, now)).toBe("live");
  });

  test("knownPositive rejects placeholders", () => {
    expect(knownPositive(null)).toBeNull();
    expect(knownPositive(0)).toBeNull();
    expect(knownPositive(-5)).toBeNull();
    expect(knownPositive(Number.NaN)).toBeNull();
    expect(knownPositive("12.5")).toBe(12.5);
    expect(knownPositive(100)).toBe(100);
  });

  test("age labels", () => {
    expect(ageLabel(null, now)).toBe("unknown");
    expect(ageLabel(now - 5000, now)).toBe("5s ago");
    expect(ageLabel(now - 120_000, now)).toBe("2m ago");
  });
});
