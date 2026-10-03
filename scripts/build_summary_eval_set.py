"""Build the ``summarize`` eval set (blueprint A6.2): Brain conversation compaction.

Each case is the exact prompt ``brain.memory.compact`` sends (previous summary + the turns being
folded, rendered with the production ``transcript`` / ``describe_widget``), built from templated
operator-Brain conversations with known facts mixed into chatter. The summary must keep every
``must_keep`` fact: each order card's paper command id, quantity and status, the operator's stated
limit, and the base symbols discussed, including facts that only exist in the previous summary.

    docker compose -f infra/docker-compose.yml run --rm --no-deps test \\
        uv run python scripts/build_summary_eval_set.py --out packages/agents/cryptonite_agents/evals/sets/summarize_v1.jsonl

Deterministic for a given seed.
"""

import argparse
import json
import random

from cryptonite_agents.brain.memory import transcript

SYMBOLS = {"BTC": (58_000, 72_000, [0.01, 0.02, 0.05, 0.1]), "ETH": (2_900, 3_700, [0.25, 0.5, 1.0, 1.5])}
TIMEFRAMES = ["15m", "1h", "4h"]
LIMITS = [
    ("never risk more than {n}% of equity on one trade", [2, 3, 4]),
    ("keep total exposure under {n}% of equity", [20, 30, 40]),
    ("no new positions if the daily loss passes {n}%", [2, 3]),
]
CHATTER = ["Good morning!", "Thanks, that's helpful.", "Ok, give me a second.", "Nice, appreciate it.",
           "Let me think about it.", "Cool."]
ANALYSES = [  # (question, answer, RSI range consistent with the answer)
    ("Is {sym} trending on the {tf}?", "On the {tf}, {sym} is above its 20 and 50 EMAs with RSI {rsi}; the trend is up but extended.", (62, 74)),
    ("What does the {tf} say for {sym}?", "{sym} on the {tf} is ranging: RSI {rsi}, price inside the bands, volume below average.", (45, 55)),
    ("Any divergence on {sym} {tf}?", "Yes, a bearish RSI divergence on the {tf} for {sym}: price made a higher high, RSI ({rsi}) did not.", (56, 68)),
]
STATUSES = ["PENDING", "APPROVED", "REJECTED", "EXPIRED"]


def card(rng: random.Random, sym: str) -> dict:
    lo, hi, sizes = SYMBOLS[sym]
    status = rng.choice(STATUSES)
    return {"kind": "order_card", "id": f"card-{rng.randrange(16**6):06x}", "side": "BUY", "quantity": rng.choice(sizes),
            "instrument_id": f"{sym}USDT.BINANCE", "order_type": "MARKET", "allocation_pct": rng.choice([2, 3, 5]),
            "status": status, "command_id": f"cmd-{rng.randrange(16**8):08x}" if status == "APPROVED" else None}


def card_facts(c: dict) -> list[str]:
    """Size, status and (when queued) the paper command id: what the summary instructions ask to keep."""
    return [str(c["quantity"]), c["status"].lower()] + ([c["command_id"]] if c["command_id"] else [])


def build_case(rng: random.Random, i: int) -> dict:
    syms = rng.sample(list(SYMBOLS), k=rng.choice([1, 2]))
    limit_tpl, limit_values = rng.choice(LIMITS)
    limit_n = rng.choice(limit_values)
    msgs: list[dict] = [{"role": "user", "content": rng.choice(CHATTER)},
                        {"role": "assistant", "content": "Hi! What would you like to look at today?"},
                        {"role": "user", "content": f"Before anything: {limit_tpl.format(n=limit_n)}."},
                        {"role": "assistant", "content": f"Noted: {limit_tpl.format(n=limit_n)}. I'll size cards within that."}]
    must = {f"{limit_n}%"} | set(syms)
    cards: list[dict] = []
    for sym in syms:
        tf = rng.choice(TIMEFRAMES)
        q, a, (rsi_lo, rsi_hi) = rng.choice(ANALYSES)
        rsi = rng.randint(rsi_lo, rsi_hi)
        msgs += [{"role": "user", "content": q.format(sym=sym, tf=tf)},
                 {"role": "assistant", "content": a.format(sym=sym, tf=tf, rsi=rsi)}]
        if rng.random() < 0.85:
            c = card(rng, sym)
            cards.append(c)
            msgs += [{"role": "user", "content": f"Draft a small {sym} buy."},
                     {"role": "assistant", "content": f"Here is a {sym} order card within your limit.", "widgets": [c]}]
            if c["status"] in ("APPROVED", "REJECTED"):
                verb = "approve" if c["status"] == "APPROVED" else "reject"
                msgs += [{"role": "user", "content": f"I {verb} it."},
                         {"role": "assistant", "content": f"Card {c['id']} {c['status'].lower()}."
                          + (f" Paper command {c['command_id']} queued." if c["command_id"] else "")}]
        msgs += [{"role": "user", "content": rng.choice(CHATTER)}, {"role": "assistant", "content": "Anytime."}]
    for c in cards:
        must.update(card_facts(c))

    previous = "(none yet)"
    if rng.random() < 0.5:  # a fact that lives only in the previous summary must survive the merge
        old = card(rng, rng.choice(list(SYMBOLS)))
        old["status"], old["command_id"] = "APPROVED", f"cmd-{rng.randrange(16**8):08x}"
        previous = (f"Earlier the operator approved a {old['instrument_id'].split('USDT')[0]} buy of {old['quantity']} "
                    f"(card {old['id']}), queued as paper command {old['command_id']}.")
        must.update([old["command_id"], str(old["quantity"])])
    prompt = (f"## Previous summary\n{previous}\n\n## New turns to merge\n{transcript(msgs)}\n\n"
              "Write the updated summary.")
    return {"id": f"summary-{i}", "kind": "summary", "persona": "summarizer", "prompt": prompt,
            "must_keep": sorted(must), "max_words": 220}  # the instruction says 200; allow 10% slack


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--out", required=True)
    p.add_argument("--n", type=int, default=24)
    p.add_argument("--seed", type=int, default=11)
    args = p.parse_args()
    rng = random.Random(args.seed)
    cases = [build_case(rng, i) for i in range(1, args.n + 1)]
    with open(args.out, "w") as f:
        for c in cases:
            f.write(json.dumps(c) + "\n")
    print(json.dumps({"written": len(cases), "avg_must_keep": round(sum(len(c["must_keep"]) for c in cases) / len(cases), 1)}))


if __name__ == "__main__":
    main()
