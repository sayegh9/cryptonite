import { test, expect } from "@playwright/test";
import { computeModeBanner, STALE_MS, type ModeBannerInput } from "../src/lib/mission/modeBanner";
import type { AiStatus, Mandate } from "../src/lib/mission/types";

// Pure unit tests (no page): the G1 banner rules behind <ModeBanner>.
const now = 1_760_000_000_000;

const m = (autonomy_level: number, status = "ACTIVE") => ({ autonomy_level, status }) as unknown as Mandate;
const st = (latched = false) =>
  ({ kill_switch: { latched, reason: latched ? "drawdown backstop" : null, latched_by: null } }) as unknown as AiStatus;

const input = (over: Partial<ModeBannerInput> = {}): ModeBannerInput => ({
  mandates: [m(1)],
  status: st(),
  mandatesOk: true,
  statusOk: true,
  mandatesAt: now - 1000,
  statusAt: now - 1000,
  denied: false,
  now,
  ...over,
});

test.describe("mode banner rules", () => {
  test("authority phrase before the counts", () => {
    const b = computeModeBanner(input({ mandates: [m(1), m(2)] }));
    expect(b.text).toBe("PAPER · acts after veto window · 1 mandate at L1, 1 at L2");
    expect(b.tone).toBe("acting");
    expect(b.flags).toEqual([]);
  });

  test("weakening qualifiers are flags, ahead of the summary", () => {
    const b = computeModeBanner(input({ mandates: [m(1), m(1, "HALTED"), m(1, "PAUSED")], statusOk: false }));
    expect(b.flags.map((f) => [f.label, f.kind])).toEqual([
      ["kill switch unknown", "warn"],
      ["1 halted", "info"],
      ["1 paused", "info"],
    ]);
    expect(b.summary).toBe("approvals required · 1 mandate at L1");
    expect(b.text).toBe("PAPER · kill switch unknown · 1 halted · 1 paused · approvals required · 1 mandate at L1");
  });

  test("any unknown part is never the gated (cyan) tone", () => {
    expect(computeModeBanner(input()).tone).toBe("gated");
    expect(computeModeBanner(input({ statusOk: false })).tone).toBe("unknown");
    expect(computeModeBanner(input({ mandatesOk: false })).tone).toBe("unknown");
    expect(computeModeBanner(input({ mandates: [], statusOk: false })).tone).toBe("unknown");
    expect(computeModeBanner(input({ mandatesOk: false, statusOk: false })).tone).toBe("unknown");
  });

  test("data older than two polls is unknown, not current", () => {
    const old = now - STALE_MS - 1;
    const ks = computeModeBanner(input({ statusAt: old }));
    expect(ks.text).toBe("PAPER · kill switch unknown · approvals required · 1 mandate at L1");
    expect(ks.tone).toBe("unknown");
    expect(ks.detail).toContain("has not updated in over 30 s");

    const both = computeModeBanner(input({ statusAt: old, mandatesAt: old }));
    expect(both.text).toBe("PAPER · status unknown");
    expect(both.detail).toContain("have not updated in over 30 s");

    // a stale latch is not reported as current either
    expect(computeModeBanner(input({ status: st(true), statusAt: old })).tone).toBe("unknown");
    expect(computeModeBanner(input({ status: st(true), statusAt: now - STALE_MS })).tone).toBe("latched");
  });

  test("not fetched yet is a neutral loading state, not an unknown one", () => {
    const b = computeModeBanner(input({ mandatesOk: null, statusOk: null, mandatesAt: null, statusAt: null }));
    expect(b.text).toBe("PAPER · loading");
    expect(b.tone).toBe("pending");
  });

  test("a latched kill switch outranks mandates", () => {
    const b = computeModeBanner(input({ status: st(true), mandates: [m(3)] }));
    expect(b.text).toBe("PAPER · kill switch latched");
    expect(b.tone).toBe("latched");
    expect(b.detail).toContain("latched: drawdown backstop");
  });
});
