"""Model prices (USD per 1M tokens) for the cost ledger and budget guard.

Source: https://ai.google.dev/gemini-api/docs/pricing (paid tier, checked 2026-09-25). Gemini's
``output_tokens`` already include thinking tokens (verified against a live call), so thinking is
billed at the output rate here too. Local models cost nothing per token.
"""

import logging
from dataclasses import dataclass
from decimal import Decimal

logger = logging.getLogger("cryptonite.agents.gateway.pricing")

MILLION = Decimal(1_000_000)
LONG_PROMPT_TOKENS = 200_000  # Pro models charge a higher rate above this prompt size


@dataclass(frozen=True)
class Price:
    input: Decimal
    output: Decimal
    cached_input: Decimal
    long_input: Decimal | None = None  # prompts > 200k tokens
    long_output: Decimal | None = None


def _p(i, o, c, li=None, lo=None) -> Price:
    d = lambda v: Decimal(str(v)) if v is not None else None  # noqa: E731
    return Price(d(i), d(o), d(c), d(li), d(lo))


PRICES: dict[str, Price] = {
    "google:gemini-2.5-pro": _p(1.25, 10.00, 0.125, 2.50, 15.00),
    "google:gemini-2.5-flash": _p(0.30, 2.50, 0.03),
    "google:gemini-2.5-flash-lite": _p(0.10, 0.40, 0.01),
    "google:gemini-3.1-pro-preview": _p(2.00, 12.00, 0.20, 4.00, 18.00),
    "google:text-embedding-004": _p(0.20, 0, 0),  # priced as Gemini Embedding (conservative)
}
# Unknown hosted models are costed at the most expensive known rate so the budget errs safe.
UNKNOWN_PRICE = PRICES["google:gemini-3.1-pro-preview"]
FREE = _p(0, 0, 0)


FREE_PREFIXES = ("ollama:", "test:", "function:")


def is_free(model_id: str) -> bool:
    """Local (Ollama) and test models cost nothing per token."""
    return model_id.startswith(FREE_PREFIXES)


def price_for(model_id: str) -> Price:
    if is_free(model_id):
        return FREE
    price = PRICES.get(model_id)
    if price is None:
        logger.warning("No price for %s; costing it at the highest known rate", model_id)
        return UNKNOWN_PRICE
    return price


def cost_usd(model_id: str, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0) -> Decimal:
    """Cost of one run. ``input_tokens`` includes the cached part, which is billed at the cache rate."""
    price = price_for(model_id)
    long_prompt = input_tokens > LONG_PROMPT_TOKENS and price.long_input is not None
    in_rate = price.long_input if long_prompt else price.input
    out_rate = price.long_output if long_prompt and price.long_output is not None else price.output
    uncached = max(0, input_tokens - cached_input_tokens)
    total = (uncached * in_rate + cached_input_tokens * price.cached_input + output_tokens * out_rate) / MILLION
    return total.quantize(Decimal("0.000001"))
