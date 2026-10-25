"""Price ranges as text: what the price step's buttons carry and what the user may type. Pure functions."""

import math

from carousearch.filters.payload import format_price

# (price_min, price_max) in SGD; None is an open end.
PriceRange = tuple[float | None, float | None]


def parse_price_range(text: str) -> PriceRange | None:
    """Read "150-400", "<250", ">1000", "800+", "-500", "100-", "S$1,000 – 2,000", or "" / "any" for no limit.

    Returns None when the text is not a price range (including a lower price above the upper one).
    """
    cleaned = _normalise(text)
    if cleaned in ("", "any"):
        return None, None
    if cleaned.startswith("<"):
        return _price_range_from_ends("", cleaned[1:])
    if cleaned.startswith(">"):
        return _price_range_from_ends(cleaned[1:], "")
    if cleaned.endswith("+"):
        return _price_range_from_ends(cleaned[:-1], "")
    low_text, separator, high_text = cleaned.partition("-")
    if not separator:
        return None
    return _price_range_from_ends(low_text, high_text)


def format_price_range(price_min: float | None, price_max: float | None) -> str:
    """The inverse of parse_price_range: (100, 300) -> "100-300", (None, 100) -> "-100", (None, None) -> ""."""
    if price_min is None and price_max is None:
        return ""
    low = format_price(price_min) if price_min is not None else ""
    high = format_price(price_max) if price_max is not None else ""
    return f"{low}-{high}"


def _normalise(text: str) -> str:
    """Lower case, without currency signs, thousands commas or spaces, and with dashes as plain hyphens."""
    cleaned = text.strip().lower()
    for removed in ("s$", "$", ",", " "):
        cleaned = cleaned.replace(removed, "")
    return cleaned.replace("–", "-").replace("—", "-")


def _price_range_from_ends(low_text: str, high_text: str) -> PriceRange | None:
    """Both ends as text, where an empty end is open. At least one end must be given."""
    if not low_text and not high_text:
        return None
    price_min = _read_price(low_text) if low_text else None
    price_max = _read_price(high_text) if high_text else None
    if (low_text and price_min is None) or (high_text and price_max is None):
        return None
    if price_min is not None and price_max is not None and price_min > price_max:
        return None
    return price_min, price_max


def _read_price(number_text: str) -> float | None:
    try:
        price = float(number_text)
    except ValueError:
        return None
    if not math.isfinite(price) or price < 0:
        return None
    return price
