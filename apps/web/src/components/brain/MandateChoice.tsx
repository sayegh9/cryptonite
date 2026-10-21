"use client";

import { editMandate, pauseMandate } from "@/lib/mission/client";
import type { MandateChoice } from "@/lib/brain/types";

const usd0 = (n: number) => n.toLocaleString("en-US", { minimumFractionDigits: 0, maximumFractionDigits: 0 });

/** BTCUSDT / btcusdt.binance -> BTCUSDT.BINANCE, as the API stores a mandate's instruments */
export function canonicalInstrument(raw: string): string {
  const s = raw.trim().toUpperCase();
  return s.includes(".") ? s : `${s}.BINANCE`;
}

/** The first mandate that can take the whole order, else the first that can take any of it ("" when none can) */
export function firstMandateChoice(candidates: MandateChoice[], requestedPct: number): string {
  return (candidates.find((c) => !c.blocked && c.room_pct >= requestedPct) ?? candidates.find((c) => !c.blocked))?.id ?? "";
}

/** The candidates other than the chosen one that can be taken off the instrument (never the holder) */
export function overlapping(candidates: MandateChoice[], chosen: MandateChoice | null): MandateChoice[] {
  return candidates.filter((c) => c.id !== chosen?.id && c.tidy);
}

/**
 * Stop the overlap, so the chosen mandate is the only ACTIVE one on the instrument: each other candidate is paused
 * when the instrument is all it covers, else the instrument is removed from it. Both take room away, so neither needs
 * TOTP. Run before the approve, so a refusal here stops before any order is sent. Returns what was done, in words.
 */
export async function stopOverlap(
  others: MandateChoice[],
  chosen: MandateChoice,
  instrumentId: string,
  { pauseReason, source }: { pauseReason: string; source?: string },
): Promise<string[]> {
  const symbol = instrumentId.split(".")[0];
  const instrument = canonicalInstrument(instrumentId);
  const done: string[] = [];
  for (const c of others) {
    if (c.tidy === "PAUSE") {
      await pauseMandate(c.id, pauseReason);
      done.push(`paused ${c.name}`);
    } else {
      await editMandate(c.id, { instruments: c.instruments.filter((i) => i !== instrument), source });
      done.push(`removed ${symbol} from ${c.name}`);
    }
  }
  return done;
}

/**
 * AMBIGUOUS_MANDATE: more than one ACTIVE mandate allows the coin, so the operator picks the one the order trades in
 * (#101). Each mandate shows what the order would size to in it; one that cannot take it is disabled with the reason.
 * Choosing changes no mandate; the optional "stop the overlap" box takes the coin off the others.
 */
export function MandateChoicePicker({
  candidates,
  symbol,
  name,
  chosenId,
  onChoose,
  tidyOthers,
  onTidy,
  sizedPct = (c) => c.allocation_pct,
}: {
  candidates: MandateChoice[];
  symbol: string;
  /** The radio group's name, unique on the page */
  name: string;
  chosenId: string;
  onChoose: (id: string) => void;
  tidyOthers: boolean;
  onTidy: (tidy: boolean) => void;
  /** What the order would size to in a mandate, % of its budget (default: the server's figure) */
  sizedPct?: (c: MandateChoice) => number;
}) {
  const chosen = candidates.find((c) => c.id === chosenId && !c.blocked) ?? null;
  const others = overlapping(candidates, chosen);
  return (
    <>
      <fieldset className="mb-2 flex flex-col gap-1 text-zinc-300">
        <legend className="mb-1 text-zinc-400">
          {candidates.length} ACTIVE mandates allow {symbol}. Trade this order in:
        </legend>
        {candidates.map((c) => {
          const pct = sizedPct(c);
          return (
            <label key={c.id} className={`flex items-start gap-1.5 ${c.blocked ? "text-zinc-500" : ""}`}>
              <input
                type="radio"
                className="mt-0.5"
                name={name}
                disabled={!!c.blocked}
                checked={chosenId === c.id}
                onChange={() => onChoose(c.id)}
              />
              <span>
                {c.name} (L{c.autonomy_level}, {c.max_position_pct}% limit of ${usd0(c.capital_budget_usd)})
                {c.blocked
                  ? ` · ${c.blocked}`
                  : ` · sized to ${pct}% ($${usd0((c.capital_budget_usd * pct) / 100)})${c.holds ? `, adds to its ${symbol}` : ""}`}
              </span>
            </label>
          );
        })}
      </fieldset>
      {chosen && others.length > 0 && (
        <label className="mb-2 flex items-start gap-1.5 text-zinc-300">
          <input type="checkbox" className="mt-0.5" checked={tidyOthers} onChange={(e) => onTidy(e.target.checked)} />
          <span>
            Also stop the overlap, so {chosen.name} is the only ACTIVE mandate on {symbol}:{" "}
            {others.map((c) => (c.tidy === "PAUSE" ? `pause ${c.name} (${symbol} is all it covers)` : `remove ${symbol} from ${c.name}`)).join("; ")}.
            This takes room away, so it needs no TOTP.
          </span>
        </label>
      )}
    </>
  );
}
