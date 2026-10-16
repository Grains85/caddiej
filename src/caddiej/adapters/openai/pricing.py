"""What a reply probably cost, in dollars, and nothing that depends on it.

CaddieJ has always counted tokens rather than money, because prices change and
a per-token figure written into the source goes quietly wrong and becomes a
number somebody trusts. That argument still holds, and this file is shaped by
it rather than in spite of it.

**An estimate, labelled as one, in one place.** The table below is the only
place a price exists. Nothing in coaching reads it, nothing is decided by it,
and nothing stored is computed from it: the token counts remain the record,
and a dollar figure in a log line is a convenience for whoever is reading the
log tonight. When the table is wrong the cost is a wrong estimate, which is a
much smaller thing than a wrong decision.

**Unknown is an answer.** A model missing from the table, or a reply that did
not say how many tokens it used, is None. Guessing a price for a model nobody
priced is how the quiet wrongness above starts.

**An operator can correct it without a release.** `OPENAI_PRICE_INPUT_PER_MTOK`
and `OPENAI_PRICE_OUTPUT_PER_MTOK`, set together in the process environment,
replace the table for whichever model is asked about. Read at call time so a
restart is all it takes, and ignored unless both parse as non-negative numbers,
because half an override is a price for half a reply.
"""

import math
import os
import re
from collections.abc import Mapping

INPUT_OVERRIDE = "OPENAI_PRICE_INPUT_PER_MTOK"
OUTPUT_OVERRIDE = "OPENAI_PRICE_OUTPUT_PER_MTOK"

# USD per million tokens, (input, output), as OpenAI listed them for standard
# processing. Reasoning tokens are already inside `output_tokens` and are
# billed as output, so they need no row of their own. Cached input is cheaper
# and is ignored: an estimate that errs high is the safe direction.
PRICES_PER_MILLION: Mapping[str, tuple[float, float]] = {
    "gpt-5": (1.25, 10.00),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5-nano": (0.05, 0.40),
    # Three times the input and over twice the output of gpt-5-mini, which is
    # worth knowing before reading its latency as a free win.
    "gpt-5.4-mini": (0.75, 4.50),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
}

# The API answers with a dated snapshot ("gpt-5-mini-2025-08-07") even when it
# was asked for the alias, and both are priced the same.
_SNAPSHOT = re.compile(r"-\d{4}-\d{2}-\d{2}$")


def estimate_cost(
    model: str, input_tokens: int | None, output_tokens: int | None
) -> float | None:
    """USD for one reply, or None when the price or the usage is unknown."""

    if input_tokens is None or output_tokens is None:
        return None
    if input_tokens < 0 or output_tokens < 0:
        return None
    prices = _override() or _listed(model)
    if prices is None:
        return None
    input_price, output_price = prices
    return (input_tokens * input_price + output_tokens * output_price) / 1_000_000


def _listed(model: str) -> tuple[float, float] | None:
    name = model.strip().lower()
    return PRICES_PER_MILLION.get(name) or PRICES_PER_MILLION.get(
        _SNAPSHOT.sub("", name)
    )


def _override() -> tuple[float, float] | None:
    input_price = _price(os.environ.get(INPUT_OVERRIDE))
    output_price = _price(os.environ.get(OUTPUT_OVERRIDE))
    if input_price is None or output_price is None:
        return None
    return input_price, output_price


def _price(raw: str | None) -> float | None:
    if raw is None or not raw.strip():
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return value
