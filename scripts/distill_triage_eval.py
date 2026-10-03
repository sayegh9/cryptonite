"""A7.4 certification: does the distilled triage student beat its own base model on held-out data?

Loads the same 4-bit base twice (plain, and with the LoRA adapter), answers every holdout brief with
greedy decoding under the production TRIAGE instructions, parses the TriageDecision JSON and scores
agreement with the rule labels. Holdouts: recent real history not seen in training (later-data
holdout) and the synthetic ``triage_v3`` eval set. Stale-data cases never reach triage in production
(the debate gate rejects them first), so the certifying score excludes them; both are reported.

Certified only if the student's production-relevant agreement beats the base on every holdout and its
valid-JSON rate is not lower.

    ~/cryptonite-train/.venv/bin/python scripts/distill_triage_eval.py --base ~/models/hf/google/gemma-4-12B-it \\
        --adapter ~/cryptonite-train/out/triage-lora --train ~/cryptonite-train/data/train.jsonl \\
        --holdout ~/cryptonite-train/data/holdout_lake.jsonl \\
        packages/agents/cryptonite_agents/evals/sets/triage_v3.jsonl --out ~/cryptonite-train/out/certification.json
"""

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path


def parse(text: str) -> bool | None:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        value = json.loads(m.group(0)).get("worth_debate")
    except (ValueError, AttributeError):
        return None
    return value if isinstance(value, bool) else None


def score(answers: list[bool | None], cases: list[dict]) -> dict:
    by = defaultdict(lambda: [0, 0])
    for a, c in zip(answers, cases, strict=True):
        reason = c.get("label_reason", "?").split("+")[0]
        by[reason][1] += 1
        by[reason][0] += a is c["expect"]["worth_debate"][0]
    ok = sum(v[0] for v in by.values())
    n = sum(v[1] for v in by.values())
    ex_ok = ok - by["stale_data"][0]
    ex_n = n - by["stale_data"][1]
    return {"agreement": round(ok / n, 4), "agreement_excl_stale": round(ex_ok / ex_n, 4) if ex_n else None,
            "valid": round(sum(a is not None for a in answers) / len(answers), 4),
            "by_rule": {k: f"{v[0]}/{v[1]}" for k, v in sorted(by.items())}}


def answer_all(model, tokenizer, system: str, cases: list[dict], max_new: int = 96) -> list[bool | None]:
    import torch

    text_tok = getattr(tokenizer, "tokenizer", tokenizer)
    out = []
    for c in cases:
        prompt = text_tok.apply_chat_template([{"role": "system", "content": system},
                                               {"role": "user", "content": c["prompt"]}],
                                              tokenize=False, add_generation_prompt=True)
        ids = text_tok(prompt, return_tensors="pt").to(model.device)
        with torch.inference_mode():
            gen = model.generate(**ids, max_new_tokens=max_new, do_sample=False)
        out.append(parse(text_tok.decode(gen[0][ids["input_ids"].shape[1]:], skip_special_tokens=True)))
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--base", required=True)
    p.add_argument("--adapter", required=True)
    p.add_argument("--holdout", nargs="+", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--train", required=True, help="training JSONL: its system turn is the production TRIAGE prompt")
    args = p.parse_args()

    from unsloth import FastModel

    with open(Path(args.train).expanduser()) as f:  # identical prompt in training and evaluation
        system = json.loads(f.readline())["messages"][0]["content"]
    sets = {Path(h).stem: [json.loads(line) for line in open(Path(h).expanduser()) if line.strip()] for h in args.holdout}
    report: dict = {"base": args.base, "adapter": args.adapter, "sets": {}}
    for who, path in (("base", args.base), ("student", args.adapter)):
        model, tokenizer = FastModel.from_pretrained(model_name=str(Path(path).expanduser()), max_seq_length=1024,
                                                     load_in_4bit=True)
        FastModel.for_inference(model)
        for name, cases in sets.items():
            report["sets"].setdefault(name, {})[who] = score(answer_all(model, tokenizer, system, cases), cases)
            print(json.dumps({"model": who, "set": name, **report["sets"][name][who]}), flush=True)
        del model
        import gc

        import torch
        gc.collect()
        torch.cuda.empty_cache()
    report["certified"] = all(
        s["student"]["agreement_excl_stale"] > s["base"]["agreement_excl_stale"] and s["student"]["valid"] >= s["base"]["valid"]
        for s in report["sets"].values())
    Path(args.out).expanduser().write_text(json.dumps(report, indent=1))
    print(json.dumps({"certified": report["certified"]}))


if __name__ == "__main__":
    main()
