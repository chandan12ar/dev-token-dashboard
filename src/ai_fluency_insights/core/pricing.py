"""API list-price lookup per model (the 'what would this cost' estimate)."""

from ..config import DEFAULT_PRICE, PRICING


def price_for(model: str):
    m = (model or "").lower()
    for key, p in PRICING:
        if key in m:
            return p
    return DEFAULT_PRICE
