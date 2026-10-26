"""What models cost (spec 03): USD per million tokens, plus flat prices."""
from __future__ import annotations

import os

# model -> (in, out, cache_read) USD per million tokens
PRICES: dict[str, tuple[float, float, float]] = {
    "muse-spark-1.3": (1.25, 4.25, 0.15),
    "muse-wonder-1.0": (4.00, 20.00, 0.20),
    "muse-spark-1.3-contributor": (0.10, 0.20, 0.002),
}
IMAGE_USD = 0.01        # Muse Image, per image
SEARCH_USD = 0.0025     # web search, per search ($2.50 per 1,000)


def price_for(model: str) -> tuple[float, float, float] | None:
    if model in PRICES:
        return PRICES[model]
    override = os.environ.get("HOUSE_PRICE", "")
    parts = override.split(",")
    if len(parts) == 3:
        try:
            return (float(parts[0]), float(parts[1]), float(parts[2]))
        except ValueError:
            pass  # a malformed override is ignored
    return None


def cost(model: str, usage: dict | None) -> float | None:
    """USD for one call's usage, or None for an unknown model (counted as 0)."""
    price = price_for(model)
    if price is None or not usage:
        return None
    inn, out, cached = price
    return (float(usage.get("in", 0)) * inn + float(usage.get("out", 0)) * out
            + float(usage.get("cache_read", 0)) * cached) / 1_000_000
