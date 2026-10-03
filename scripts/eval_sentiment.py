"""A6.3 tagging eval: sentiment service models vs LLM taggers on labelled headlines/posts.

Datasets:
- ``news``: balanced sample of zeroshot/twitter-financial-news-sentiment ``sent_valid.csv`` (MIT;
  0 bearish, 1 bullish, 2 neutral), independent of FinBERT's training data. Download it to
  ``--news-csv`` first.
- ``crypto``: hand-labelled, clear-cut crypto headlines and posts below. CryptoBERT's own training
  set (StockTwits crypto) is deliberately not used: scoring it there would flatter it.

Taggers: ``service:news`` (FinBERT) and ``service:social`` (CryptoBERT) via the sentiment service, and
any gateway model through the ``tagging`` role (``SentimentTag`` structured output). Reports accuracy
and macro-F1 per dataset.

    docker compose -f infra/docker-compose.yml run --rm --no-deps -e OLLAMA_BASE_URL=http://172.17.0.1:11434/v1 \\
        -v $HOME/models/datasets:/datasets:ro test uv run python scripts/eval_sentiment.py \\
        --news-csv /datasets/twitter_fin_news_valid.csv service:news service:social \\
        ollama:gemma4:12b-it-qat google:gemini-2.5-flash-lite
"""

import argparse
import asyncio
import csv
import json
import random
import time

from cryptonite_agents import news
from cryptonite_agents.gateway import BudgetGuard, Gateway
from cryptonite_schemas.sentiment import SentimentTag

LABELS = ("bearish", "bullish", "neutral")
INSTRUCTIONS = ("Tag the market sentiment of one crypto or financial headline or post for traders: bullish "
                "(likely to push the asset's price up), bearish (likely to push it down) or neutral (no clear "
                "price direction). Give your confidence from 0 to 1.")

CRYPTO = [
    ("Bitcoin smashes through $70,000 as spot ETF inflows hit a record", "bullish"),
    ("Ethereum surges 9% after the upgrade ships without issues", "bullish"),
    ("BlackRock expands its bitcoin fund as institutional demand accelerates", "bullish"),
    ("Whales accumulate ETH at the fastest pace in two years", "bullish"),
    ("BTC breaks its all-time high on heavy volume, shorts liquidated", "bullish"),
    ("SEC approves new crypto ETFs, market rallies", "bullish"),
    ("Exchange reserves of bitcoin fall to a five-year low as holders withdraw coins", "bullish"),
    ("Major bank launches bitcoin custody for wealthy clients", "bullish"),
    ("ETH staking demand soars, supply on exchanges shrinks", "bullish"),
    ("Just loaded more BTC here, this breakout looks strong, targeting new highs", "bullish"),
    ("Crypto market rebounds strongly as inflation cools faster than expected", "bullish"),
    ("Bitcoin miners report record revenue and expand capacity", "bullish"),
    ("Exchange hacked, $400M in ETH stolen, withdrawals halted", "bearish"),
    ("Bitcoin plunges 12% as leveraged longs are wiped out", "bearish"),
    ("Regulator sues the largest crypto exchange and freezes assets", "bearish"),
    ("ETH breaks below key support, analysts warn of further downside", "bearish"),
    ("Stablecoin loses its peg, contagion fears spread across crypto", "bearish"),
    ("Spot bitcoin ETFs see their biggest outflows on record", "bearish"),
    ("Country bans crypto trading and orders exchanges to shut down", "bearish"),
    ("Dumped all my ETH, this chart is breaking down hard", "bearish"),
    ("Major crypto lender files for bankruptcy, customer funds locked", "bearish"),
    ("Bitcoin slides as the Fed signals more rate hikes", "bearish"),
    ("Miners capitulate and sell reserves as hashprice collapses", "bearish"),
    ("Defi protocol exploited, token crashes 80% in an hour", "bearish"),
    ("Bitcoin trades flat around $65,000 ahead of the weekend", "neutral"),
    ("Ethereum developers schedule the next community call for Thursday", "neutral"),
    ("Here is what happened in crypto today", "neutral"),
    ("Crypto exchange updates its fee schedule for API users", "neutral"),
    ("How bitcoin's difficulty adjustment works, explained", "neutral"),
    ("ETH gas fees steady this week, network activity unchanged", "neutral"),
    ("Analysts split on bitcoin's next move as volatility stays low", "neutral"),
    ("New wallet app adds support for more tokens", "neutral"),
    ("Bitcoin conference announces speakers for next year's event", "neutral"),
    ("Ethereum foundation publishes its quarterly research report", "neutral"),
    ("Watching BTC range between 64k and 66k, no position yet", "neutral"),
    ("Crypto tax deadline reminder: what traders should know", "neutral"),
]


def macro_f1(gold: list[str], pred: list[str]) -> float:
    f1s = []
    for label in LABELS:
        tp = sum(g == p == label for g, p in zip(gold, pred, strict=True))
        fp = sum(p == label != g for g, p in zip(gold, pred, strict=True))
        fn = sum(g == label != p for g, p in zip(gold, pred, strict=True))
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1s.append(2 * prec * rec / (prec + rec) if prec + rec else 0.0)
    return round(sum(f1s) / len(f1s), 3)


def load_news(path: str, per_class: int, seed: int) -> list[tuple[str, str]]:
    names = {"0": "bearish", "1": "bullish", "2": "neutral"}
    by: dict[str, list[str]] = {v: [] for v in names.values()}
    for row in csv.DictReader(open(path)):
        by[names[row["label"]]].append(row["text"])
    rng = random.Random(seed)
    out = [(t, label) for label, texts in by.items() for t in rng.sample(texts, min(per_class, len(texts)))]
    rng.shuffle(out)
    return out


async def run_tagger(tagger: str, items: list[tuple[str, str]], gw: Gateway) -> tuple[list[str], float]:
    started = time.monotonic()
    texts = [t for t, _ in items]
    if tagger.startswith("service:"):
        preds = []
        for i in range(0, len(texts), 32):
            res = news.tag(texts[i:i + 32], tagger.split(":", 1)[1])
            if res is None:
                raise RuntimeError("sentiment service unavailable")
            preds += [r["label"] for r in res]
    else:
        preds = []
        for t in texts:
            try:
                out = (await gw.run("tagging", t, instructions=INSTRUCTIONS, output_type=SentimentTag,
                                    model_override=tagger)).output
                preds.append(out.label)
            except Exception:
                preds.append("error")
    return preds, (time.monotonic() - started) / max(len(texts), 1)


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("taggers", nargs="+")
    p.add_argument("--news-csv", required=True)
    p.add_argument("--per-class", type=int, default=60)
    p.add_argument("--seed", type=int, default=5)
    args = p.parse_args()
    datasets = {"news": load_news(args.news_csv, args.per_class, args.seed), "crypto": CRYPTO}
    gw = Gateway(budget=BudgetGuard(default_cap_usd=5))
    for tagger in args.taggers:
        for name, items in datasets.items():
            try:
                preds, sec = await run_tagger(tagger, items, gw)
            except Exception as e:
                print(json.dumps({"tagger": tagger, "dataset": name, "error": f"{type(e).__name__}: {e}"[:200]}), flush=True)
                continue
            gold = [label for _, label in items]
            acc = round(sum(g == q for g, q in zip(gold, preds, strict=True)) / len(gold), 3)
            print(json.dumps({"tagger": tagger, "dataset": name, "n": len(gold), "accuracy": acc,
                              "macro_f1": macro_f1(gold, preds), "errors": preds.count("error"),
                              "sec_per_item": round(sec, 3)}), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
