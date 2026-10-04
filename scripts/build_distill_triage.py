"""A7.4: turn rule-labelled triage cases into chat-format SFT data for triage distillation.

Input: JSONL from ``build_triage_eval_set.py`` (``prompt``, ``expect``, ``label_reason``, ``facts``).
Output: ``{"messages": [system, user, assistant]}`` per case. The system prompt is the production
TRIAGE persona's instructions, the user turn the market brief, the assistant turn the TriageDecision
JSON with a one-sentence reason naming the deciding rule (what the persona asks for).

Later-data holdout (blueprint A7.4): build training cases from older history and holdout cases from
the most recent weeks, e.g.

    build_triage_eval_set.py --source lake --until-days-ago 14 --max-states 2000 --per-class 250 --seed 31 --out train_lake.jsonl
    build_triage_eval_set.py --source lake --since-days-ago 14 --max-states 1500 --per-class 40 --seed 41 --out holdout_lake.jsonl
    build_triage_eval_set.py --source synthetic --per-class 200 --seed 101 --out train_synth.jsonl
    build_distill_triage.py train_lake.jsonl train_synth.jsonl --exclude holdout_lake.jsonl sets/triage_v3.jsonl --out train.jsonl
"""

import argparse
import json

from cryptonite_agents.debate_fleet import TRIAGE

REASONS = {
    "reward_to_risk_below_1.5": "Reward-to-risk {rr} is below the 1.5 minimum.",
    "stale_data": "The last closed bar is {age_min} min old, older than two 15m bars, so the data is stale.",
    "both_timeframes_against": "Both shown timeframes trend down, against the long.",
    "chop": "Volume is {volume_15m}x average with price inside the bands: chop.",
    "clean_setup": "Clean setup: reward-to-risk {rr}, fresh data, no timeframe against the trade, volume {volume_15m}x.",
}


def assistant_turn(case: dict) -> str:
    worth = case["expect"]["worth_debate"][0]
    first_reason = case["label_reason"].split("+")[0]
    reason = REASONS[first_reason].format(**case["facts"])
    return json.dumps({"worth_debate": worth, "confidence": 0.9, "reason": reason})


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("inputs", nargs="+")
    p.add_argument("--exclude", nargs="*", default=[], help="eval/holdout sets whose prompts must not be trained on")
    p.add_argument("--out", required=True)
    args = p.parse_args()
    excluded = {json.loads(line)["prompt"] for path in args.exclude for line in open(path) if line.strip()}
    seen: set[str] = set()
    rows, dropped = [], 0
    for path in args.inputs:
        for line in open(path):
            if not line.strip():
                continue
            case = json.loads(line)
            if case["prompt"] in excluded or case["prompt"] in seen:
                dropped += 1
                continue
            seen.add(case["prompt"])
            rows.append({"messages": [{"role": "system", "content": TRIAGE.instructions},
                                      {"role": "user", "content": case["prompt"]},
                                      {"role": "assistant", "content": assistant_turn(case)}],
                         "label": case["expect"]["worth_debate"][0], "reason": case["label_reason"]})
    with open(args.out, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    print(json.dumps({"written": len(rows), "dropped_overlap_or_duplicate": dropped,
                      "worth_debate_true": sum(r["label"] for r in rows)}))


if __name__ == "__main__":
    main()
