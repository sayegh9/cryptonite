"""A6.4 recall eval: does a candidate embedding model retrieve reflections at least as well as the
current one? Acceptance: "recall eval not worse".

A labelled corpus of trading lessons (3 per topic, phrased differently) and debate-style queries that
each belong to one topic. For every model: embed lessons as documents and queries as queries (the
production ``Gateway.embed_tagged`` path), rank by cosine similarity, and report hit@1 (top lesson
is on topic) and recall@3 (at least one on-topic lesson in the top 3, the number ``recall_lessons``
feeds a debate).

    docker compose -f infra/docker-compose.yml run --rm --no-deps -e OLLAMA_BASE_URL=http://172.17.0.1:11434/v1 \\
        test uv run python scripts/eval_embeddings.py google:text-embedding-004 ollama:embeddinggemma
"""

import asyncio
import json
import math
import sys

from cryptonite_agents.gateway import BudgetGuard, Gateway

LESSONS = {
    "funding": [
        "Longs paid very high funding into the move; the squeeze reversed sharply once funding turned positive and crowded.",
        "Extreme positive funding with rising open interest was a warning of a long squeeze we ignored.",
        "When perp funding spikes while spot volume lags, leverage is driving price and the move is fragile.",
    ],
    "low_volume_breakout": [
        "The breakout above resistance happened on half the average volume and failed within two candles.",
        "Range breaks without volume confirmation kept trapping us; require volume above average before entering.",
        "A thin-liquidity push through the high reversed straight back into the range.",
    ],
    "tight_stop": [
        "The stop sat inside normal noise; with ATR this wide it was hit before the trade had room to work.",
        "Stops must scale with volatility: a fixed 0.5 percent stop in a high-ATR session got wicked out.",
        "We were stopped by an ordinary wick, then price went to target; the stop ignored the true range.",
    ],
    "overbought_chase": [
        "Buying with RSI above 80 after a vertical run meant buying the local top.",
        "Chasing an extended move far above the upper Bollinger band gave a poor entry and an immediate pullback.",
        "Momentum was exhausted when we entered; waiting for a retest would have given a better price.",
    ],
    "weekend": [
        "Weekend order books are thin; a small sell wall moved price far more than on weekdays.",
        "Saturday night liquidity was poor and spreads widened, so slippage ate the edge.",
        "Low weekend participation produced fake moves that reversed on Monday's open.",
    ],
    "news_spike": [
        "The spike came from a headline and faded within the hour; news candles are not trend signals.",
        "Trading the first reaction to a macro release meant entering at the worst volatility.",
        "An exchange-listing rumour pumped price, then it fully retraced when the rumour was denied.",
    ],
    "mixed_regime": [
        "Trend-following entries kept failing while the higher timeframes were mixed and choppy.",
        "With the 1h and 4h disagreeing, the setup had no edge; flat was the right position.",
        "Conflicting timeframe trends produced whipsaws; only trade when the higher frames align.",
    ],
    "correlation": [
        "ETH followed BTC's drop despite its own bullish setup; check BTC direction before trading alts.",
        "Our ETH long ignored that BTC was breaking down; correlation dominated the alt-specific signal.",
        "When BTC dumps, ETH setups fail regardless of their own chart; correlation risk was underrated.",
    ],
    "range_reversion": [
        "In a clear range, buying the lower band and selling the upper band worked better than breakouts.",
        "Mean reversion inside the established range paid; the breakout trades did not.",
        "Price respected the range edges for days; fading the extremes was the profitable approach.",
    ],
    "early_exit": [
        "We closed the winner at the first small pullback and missed the larger move to target.",
        "Taking profit far too early cut the reward-to-risk; a trailing stop would have kept the trend.",
        "Fear of giving back gains made us exit early; the plan's target was reached hours later.",
    ],
}
QUERIES = [
    ("BTC perp funding is at 0.09% per interval and open interest keeps climbing while price rallies.", "funding"),
    ("Leverage looks crowded: funding very positive, longs dominant, spot volume weak.", "funding"),
    ("ETH is breaking above the 50-bar high, but volume is only 0.5x average.", "low_volume_breakout"),
    ("A breakout candidate with volume well below average just cleared resistance.", "low_volume_breakout"),
    ("Proposed stop is 0.3% away while the 15m ATR is 0.8% of price.", "tight_stop"),
    ("Volatility is high today; the candidate's stop looks narrow relative to the ATR.", "tight_stop"),
    ("RSI is 84 after a straight-line rally and price is above the upper band.", "overbought_chase"),
    ("The move is very extended; entering now means chasing momentum.", "overbought_chase"),
    ("It is Saturday night; the order book is thin and spreads are wider than usual.", "weekend"),
    ("Weekend session with low participation, candidate triggered on a small volume spike.", "weekend"),
    ("A sudden 3% candle just printed right after a regulatory headline.", "news_spike"),
    ("Price spiked on CPI release minutes ago; the scanner flagged a breakout.", "news_spike"),
    ("The 15m trend is up but the 1h and 4h are mixed; the market looks choppy.", "mixed_regime"),
    ("Higher timeframes disagree with each other and recent trend entries were whipsawed.", "mixed_regime"),
    ("ETH has a clean bullish setup but BTC is breaking down hard on the 1h.", "correlation"),
    ("Alt long candidate while bitcoin is dumping; how much does BTC direction matter?", "correlation"),
    ("Price has bounced between the same support and resistance for three days.", "range_reversion"),
    ("Sideways market with clear range edges; the candidate is a breakout of the upper edge.", "range_reversion"),
    ("The open position is up 1.2% and pulling back slightly; should we close now?", "early_exit"),
    ("We have a winner running toward target; tempted to take profit on this first dip.", "early_exit"),
]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)) or 1.0)


async def score(gw: Gateway, model: str) -> dict:
    docs = [(topic, text) for topic, texts in LESSONS.items() for text in texts]
    doc_vecs, _ = await gw.embed_tagged([t for _, t in docs], kind="document", model_override=model)
    q_vecs, _ = await gw.embed_tagged([q for q, _ in QUERIES], kind="query", model_override=model)
    hit1 = rec3 = 0
    for (_, topic), qv in zip(QUERIES, q_vecs, strict=True):
        ranked = sorted(range(len(docs)), key=lambda i: -cosine(qv, doc_vecs[i]))
        hit1 += docs[ranked[0]][0] == topic
        rec3 += any(docs[i][0] == topic for i in ranked[:3])
    n = len(QUERIES)
    return {"model": model, "dims": len(doc_vecs[0]), "hit@1": round(hit1 / n, 3), "recall@3": round(rec3 / n, 3)}


async def main() -> None:
    gw = Gateway(budget=BudgetGuard(default_cap_usd=5))
    for model in sys.argv[1:]:
        try:
            print(json.dumps(await score(gw, model)), flush=True)
        except Exception as e:  # one unavailable model must not hide the others
            print(json.dumps({"model": model, "error": f"{type(e).__name__}: {e}"[:300]}), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
