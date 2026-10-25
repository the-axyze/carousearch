"""Rule-based screening: pure functions that tag a listing from its title, description and price. No I/O.

Each scorer returns a `Tag` or None. Adding a rule means adding one function here, calling it from
`description_tags` (or from `screening.pipeline` for card-level rules), and adding one test.

Prices are read by `find_price_mentions`. A number counts as a price when it is marked as money ("$850",
"S$1,299", "SGD 650", "650 sgd", "1.2k") or sits in a price context ("selling at 850", "850 nett",
"15 Pro 256GB - 850"). Bare numbers outside such a context are ignored, which is what keeps model numbers,
years ("bought in 2023") and counts out. Storage sizes, percentages and units after a number, and
Singapore phone numbers, are never prices.
"""

import math
import re
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from carousearch.screening.models import PriceSource, ResolvedPrice, Tag, TagCode, build_tag, format_sgd

# --- thresholds -----------------------------------------------------------------------------------
# A title naming this many distinct variants (models, or storage sizes) is a multi-item post.
MIN_TITLE_VARIANTS = 2
# A description offering this many distinct prices is a price list ("$579 ... $650 ... $850").
MIN_DISTINCT_DESCRIPTION_PRICES = 3
# ... and so is one with this many "<variant> - $<amount>" lines, even with fewer distinct amounts.
MIN_PRICED_VARIANT_LINES = 2
# Below this fraction of the other results' median price, a listing is flagged as below market: usually a
# catalogue post's cheapest variant, a deposit or instalment, a placeholder price, or scam bait.
BELOW_MARKET_FRACTION = 0.6
# The median of fewer other results is too noisy to compare against.
MIN_PEERS_FOR_MARKET = 5
# Sizes under this are RAM ("8GB/256GB", "16GB/512GB"), not storage variants.
MIN_STORAGE_VARIANT_GB = 64
# Listed and description prices closer than this (SGD) are the same price.
SAME_PRICE_TOLERANCE = 0.5
# How many variants or prices a tag reason lists before "and N more".
MAX_EVIDENCE_ITEMS = 5
MAX_EVIDENCE_TEXT_LENGTH = 30

# --- price mentions ---------------------------------------------------------------------------------

_AMOUNT = r"(?P<amount>\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)(?P<thousands>k)?"
_NOT_AFTER_NUMBER = r"(?<![\w.,$/])"  # so "R15", "1,299" and "9/10" are not read from their middle
_CURRENCY = r"(?:s\$|\bsgd|\$)"

# "$850", "S$1,299", "SGD 650", "$1.2k"
_CURRENCY_BEFORE_AMOUNT = re.compile(rf"{_CURRENCY}\s*{_AMOUNT}(?!\w)", re.IGNORECASE)
# "650 sgd", "900 dollars"
_CURRENCY_AFTER_AMOUNT = re.compile(rf"{_NOT_AFTER_NUMBER}{_AMOUNT}\s*(?:sgd|dollars?|bucks)\b", re.IGNORECASE)
# "1.2k" (a decimal before k reads as money; a bare "4k" is more often a resolution)
_DECIMAL_THOUSANDS = re.compile(rf"{_NOT_AFTER_NUMBER}(?P<amount>\d+\.\d{{1,2}})(?P<thousands>k)\b", re.IGNORECASE)
# "selling at 850", "price: 850", "nett 850", "@ 850"
_AMOUNT_AFTER_PRICE_WORD = re.compile(
    rf"(?:\b(?:price[ds]?|priced at|selling(?: at| for)?|sell(?: at| for)?|asking(?: price)?|going (?:at|for)"
    rf"|let(?:ting)? go (?:at|for)|nett?|offer|only|bought (?:at|for)|paid|retail|rrp|worth|was|now)|@)"
    rf"\s*(?:is|at|for)?\s*[:=-]?\s*{_NOT_AFTER_NUMBER}{_AMOUNT}(?![\w%])",
    re.IGNORECASE,
)
# "850 nett", "900 nego", "1.2k firm"
_AMOUNT_BEFORE_PRICE_WORD = re.compile(
    rf"{_NOT_AFTER_NUMBER}{_AMOUNT}\s*(?:nett?|neg|nego|negotiable|firm|obo)\b", re.IGNORECASE
)
# A line "<label> - <amount>", e.g. "15 Pro 256GB - 850" or "15 Pro Max: $1,050".
_PRICED_LINE = re.compile(
    rf"^[ \t]*(?P<label>[^\n]*?[a-z][^\n]*?)[ \t]*[-:=–—][ \t]*(?P<currency>{_CURRENCY})?[ \t]*{_AMOUNT}"
    rf"[ \t]*(?:sgd|nett?|each|ea|only)?[ \t.!]*$",
    re.IGNORECASE | re.MULTILINE,
)
# Singapore numbers: 8 digits starting 3, 6, 8 or 9, optionally "+65" and a space in the middle.
_PHONE_NUMBER = re.compile(r"(?:\+?65[\s-]?)?(?<!\d)[3689]\d{3}[\s-]?\d{4}(?!\d)")
# What may follow a bare number that is a quantity or a measure rather than a price.
_UNIT_AFTER_NUMBER = re.compile(
    r"\s*(?:%|\"|(?:gb|tb|mb|mah|cm|mm|kg|g|ml|hz|inch|inches|years?|yrs?|months?|mths?|days?|weeks?|hours?|hrs?"
    r"|mins?|km|cc|pcs|pieces|units|sets|pm|am|x)\b)",
    re.IGNORECASE,
)
_YEAR = re.compile(r"^(?:199\d|20[0-3]\d)$")
# Words in a "<label> - <number>" line that make the number a statistic, not a price.
_STATISTIC_LABEL_WORDS = frozenset(
    {
        "battery", "health", "bh", "cycle", "cycles", "count", "warranty", "mileage", "km", "odometer", "size",
        "qty", "quantity", "age", "year", "years", "storage", "ram", "capacity", "model", "contact", "call",
        "whatsapp", "wa", "tel", "hp", "mobile", "phone", "postal", "unit", "condition", "rating", "purchased",
    }
)
# Labels made only of these words are a price line about the listing itself ("Price: $850"), not a variant.
_GENERIC_PRICE_LABEL_WORDS = frozenset(
    {
        "price", "prices", "selling", "sell", "asking", "nett", "net", "now", "offer", "cost", "rrp", "retail",
        "u.p", "u.p.", "sale", "my", "final", "total", "only", "at", "for", "is", "the", "deal", "special",
        "promo", "usual", "original", "was", "listed", "listing",
    }
)
# A price in a clause with one of these words is what the item cost new or before, not what it is offered for.
_REFERENCE_PRICE_WORDS = re.compile(
    r"\b(?:retail|rrp|u\.p\.?|usual(?:ly)?|original(?:ly)?|bought|paid|worth|msrp|was|market price|new price)\b",
    re.IGNORECASE,
)
_CLAUSE_DELIMITERS = ".!?;,(\n"
# "from $579", "starting from $579", "starts at S$579"
_FROM_PRICE = re.compile(
    rf"\b(?:from|start(?:s|ing)?(?: at| from)?)\s*{_CURRENCY}\s*\d[\d,]*(?:\.\d+)?k?", re.IGNORECASE
)


@dataclass(frozen=True)
class PriceMention:
    amount: float  # SGD
    start: int  # where the amount's digits begin in the text
    is_reference_price: bool  # "bought at $1,299", "retail $1,599": not what the listing is offered for


def extract_prices(text: str) -> list[float]:
    """Every dollar amount in the text, in order of appearance (repeats kept)."""
    return [mention.amount for mention in find_price_mentions(text)]


def find_price_mentions(text: str) -> list[PriceMention]:
    searchable = _blank_out_phone_numbers(text)
    matches = [*_CURRENCY_BEFORE_AMOUNT.finditer(searchable), *_CURRENCY_AFTER_AMOUNT.finditer(searchable)]
    matches.extend(_DECIMAL_THOUSANDS.finditer(searchable))
    for pattern in (_AMOUNT_AFTER_PRICE_WORD, _AMOUNT_BEFORE_PRICE_WORD):
        matches += [match for match in pattern.finditer(searchable) if _is_plausible_bare_amount(searchable, match)]
    matches += [match for match in _PRICED_LINE.finditer(searchable) if _is_price_line(searchable, match)]
    mentions_by_start: dict[int, PriceMention] = {}
    for match in matches:
        start = match.start("amount")
        mentions_by_start.setdefault(start, _mention_from_match(searchable, match))
    return [mentions_by_start[start] for start in sorted(mentions_by_start)]


def offered_prices(text: str) -> list[float]:
    """The distinct amounts the listing is offered for, in order of appearance (reference prices left out)."""
    amounts = [mention.amount for mention in find_price_mentions(text) if not mention.is_reference_price]
    return list(dict.fromkeys(amounts))


def _blank_out_phone_numbers(text: str) -> str:
    """Phone numbers replaced by spaces of the same length, so positions in the text stay valid."""
    return _PHONE_NUMBER.sub(lambda match: " " * len(match.group(0)), text)


def _mention_from_match(text: str, match: re.Match[str]) -> PriceMention:
    amount = float(match.group("amount").replace(",", ""))
    if match.group("thousands"):
        amount *= 1000
    start = match.start("amount")
    is_reference = _REFERENCE_PRICE_WORDS.search(_preceding_clause(text, start)) is not None
    return PriceMention(amount=amount, start=start, is_reference_price=is_reference)


def _preceding_clause(text: str, position: int) -> str:
    clause_start = max(text.rfind(delimiter, 0, position) for delimiter in _CLAUSE_DELIMITERS) + 1
    return text[clause_start:position]


def _is_plausible_bare_amount(text: str, match: re.Match[str]) -> bool:
    """A number found only by its context must not be followed by a unit ("2 months", "256GB", "50%")."""
    number_end = match.end("thousands") if match.group("thousands") else match.end("amount")
    return _UNIT_AFTER_NUMBER.match(text, number_end) is None


def _is_price_line(text: str, match: re.Match[str]) -> bool:
    if match.group("currency"):
        return True
    if _YEAR.match(match.group("amount")) or not _is_plausible_bare_amount(text, match):
        return False
    return not (_label_words(match.group("label")) & _STATISTIC_LABEL_WORDS)


def _label_words(label: str) -> set[str]:
    return set(re.findall(r"[a-z0-9.]+", label.lower()))


# --- title rule -------------------------------------------------------------------------------------

_TITLE_SEPARATOR = re.compile(r"\s*(?:[/|,&]|\bor\b)\s*", re.IGNORECASE)
_WITH_ABBREVIATION = re.compile(r"\bw/", re.IGNORECASE)
_PRICE_OR_PERCENT_IN_TITLE = re.compile(rf"{_CURRENCY}\s*\d[\d,.]*k?|\d+(?:\.\d+)?\s*%", re.IGNORECASE)
_CATALOGUE_PHRASE = re.compile(
    rf"\b(?:all (?:models?|storages?|capacit(?:y|ies)|colou?rs|sizes|series|variants)"
    rf"|many (?:sets|units|models|colou?rs)|starting from|from\s*{_CURRENCY}\s*(?:\d[\d,.]*k?)?)",
    re.IGNORECASE,
)
_TITLE_TOKEN = re.compile(r"[a-z0-9]+\+?")
_BRACKETED_ASIDE = re.compile(r"\([^()]*\)|\[[^\[\]]*\]")
_DISPLAY_STRIP_CHARACTERS = " -()[]|,&/"
_STORAGE_TOKEN = re.compile(r"^(?P<size>\d+)(?P<unit>gb|tb)$")
_MEASUREMENT_TOKEN = re.compile(r"^\d+(?:\.\d+)?(?:cm|mm|kg|ml|mah|hz|inch|cc|km|w|v|l|g|mp|fps)$")
# Words that, after a number, make it a duration or a count ("6 months warranty", "2 units left").
_QUANTITY_WORDS = frozenset(
    {"month", "months", "mth", "mths", "year", "years", "yr", "yrs", "day", "days", "week", "weeks",
     "pc", "pcs", "unit", "units", "set", "sets", "x"}
)
# Words that name a variant without a digit ("15 Pro", "Switch Lite").
_VARIANT_WORDS = frozenset({"pro", "max", "plus", "mini", "ultra", "lite", "oled", "air", "se", "fe", "slim"})


@dataclass(frozen=True)
class TitleSegment:
    """One part of a title between separators, e.g. "15 Pro Max" in "15 / 15 Pro / 15 Pro Max"."""

    text: str  # as written, for the tag reason
    words: frozenset[str]
    model_tokens: frozenset[str]  # tokens that name a model or variant: "15", "r15m", "pro", "oled"
    storage_sizes_gb: frozenset[int]


def title_lists_multiple_variants(title: str, query: str | None) -> Tag | None:
    """Tag a title that advertises several models or storage sizes, e.g. "iPhone 15 / 15 Pro / 15 Pro Max".

    The title is split at "/", "|", ",", "&" and "or". A segment names a variant when it has a model token
    (a token with a digit that is not a year, size or count, or a word such as Pro or Max), or repeats a word
    of the search query that another segment also has (the "Switch" in "Nintendo Switch / Switch Lite").
    Segments with the same model tokens are one variant, so "15 Pro Max Blue / 15 Pro Max Black" is one.
    "iPhone 15 Pro Max 256GB" has no separator and is one variant; "15 / 15 Pro / 15 Pro Max" is three.
    """
    segments = split_title_segments(title)
    variants = distinct_variant_segments(segments, _product_words(query))
    storage_sizes = sorted({size for segment in segments for size in segment.storage_sizes_gb})
    catalogue_phrase = _CATALOGUE_PHRASE.search(title)
    evidence = []
    if len(variants) >= MIN_TITLE_VARIANTS:
        evidence.append(f"names {len(variants)} variants: {_list_evidence([segment.text for segment in variants])}")
    if len(storage_sizes) >= MIN_TITLE_VARIANTS:
        sizes = [_describe_storage(size) for size in storage_sizes]
        evidence.append(f"names {len(storage_sizes)} storage sizes: {_list_evidence(sizes)}")
    if catalogue_phrase:
        evidence.append(f"says '{catalogue_phrase.group(0).strip()}'")
    if not evidence:
        return None
    return build_tag(TagCode.MULTI_ITEM_TITLE, "title " + "; ".join(evidence))


def split_title_segments(title: str) -> list[TitleSegment]:
    cleaned = _PRICE_OR_PERCENT_IN_TITLE.sub(" ", _WITH_ABBREVIATION.sub("with ", title))
    segments = []
    for part in _TITLE_SEPARATOR.split(cleaned):
        tokens = _TITLE_TOKEN.findall(part.lower())
        if not tokens:
            continue
        segments.append(
            TitleSegment(
                text=_display_text(part),
                words=frozenset(tokens),
                model_tokens=frozenset(_model_tokens(tokens)),
                storage_sizes_gb=frozenset(_storage_sizes(tokens)),
            )
        )
    return segments


def distinct_variant_segments(segments: Sequence[TitleSegment], product_words: set[str]) -> list[TitleSegment]:
    """The first segment of each distinct variant, in title order."""
    repeated_product_words = {
        word for word in product_words if sum(word in segment.words for segment in segments) >= 2
    }
    first_segment_by_signature: dict[frozenset[str], TitleSegment] = {}
    for segment in segments:
        signature = segment.model_tokens | (segment.words & repeated_product_words)
        if signature and signature not in first_segment_by_signature:
            first_segment_by_signature[signature] = segment
    return list(first_segment_by_signature.values())


def _model_tokens(tokens: list[str]) -> list[str]:
    model_tokens = []
    for index, token in enumerate(tokens):
        next_token = tokens[index + 1] if index + 1 < len(tokens) else ""
        is_model_number = _is_model_number(token) and next_token not in _QUANTITY_WORDS
        if token in _VARIANT_WORDS or is_model_number:
            model_tokens.append(token)
    return model_tokens


def _is_model_number(token: str) -> bool:
    has_digit = any(character.isdigit() for character in token)
    is_size_or_year = _STORAGE_TOKEN.match(token) or _MEASUREMENT_TOKEN.match(token) or _YEAR.match(token)
    return has_digit and not is_size_or_year


def _storage_sizes(tokens: list[str]) -> list[int]:
    """Storage sizes in GB, leaving out RAM ("16GB RAM") and anything under MIN_STORAGE_VARIANT_GB."""
    sizes = []
    for index, token in enumerate(tokens):
        match = _STORAGE_TOKEN.match(token)
        next_token = tokens[index + 1] if index + 1 < len(tokens) else ""
        if match is None or next_token == "ram":
            continue
        size_gb = int(match.group("size")) * (1024 if match.group("unit") == "tb" else 1)
        if size_gb >= MIN_STORAGE_VARIANT_GB:
            sizes.append(size_gb)
    return sizes


def _product_words(query: str | None) -> set[str]:
    """The query's plain words ("iphone", "switch"); its model tokens are found in the title anyway."""
    if not query:
        return set()
    return {word for word in _TITLE_TOKEN.findall(query.lower()) if word.isalpha() and word not in _VARIANT_WORDS}


def _describe_storage(size_gb: int) -> str:
    return f"{size_gb // 1024}TB" if size_gb >= 1024 and size_gb % 1024 == 0 else f"{size_gb}GB"


def _display_text(segment_text: str) -> str:
    """The segment as written, without catalogue phrases, bracketed asides ("(Accept CDC)") or leading emoji."""
    without_asides = _BRACKETED_ASIDE.sub(" ", _CATALOGUE_PHRASE.sub(" ", segment_text))
    text = " ".join(without_asides.split()).strip(_DISPLAY_STRIP_CHARACTERS)
    return _shorten(re.sub(r"^\W+", "", text))


def _shorten(text: str) -> str:
    return text if len(text) <= MAX_EVIDENCE_TEXT_LENGTH else text[: MAX_EVIDENCE_TEXT_LENGTH - 1] + "…"


def _list_evidence(items: Sequence[str]) -> str:
    shown = ", ".join(items[:MAX_EVIDENCE_ITEMS])
    hidden_count = len(items) - MAX_EVIDENCE_ITEMS
    return f"{shown} and {hidden_count} more" if hidden_count > 0 else shown


# --- description rules ------------------------------------------------------------------------------


def description_tags(listed_price: float | None, description: str) -> list[Tag]:
    """Every description rule's tag for one listing."""
    tags = [description_has_price_list(description), listed_price_is_floor(listed_price, description)]
    return [tag for tag in tags if tag is not None]


def description_has_price_list(description: str) -> Tag | None:
    """Tag a description pricing several variants: 3+ distinct offered prices, or 2+ "<variant> - $<amount>" lines."""
    amounts = offered_prices(description)
    priced_lines = priced_variant_lines(description)
    evidence = []
    if len(amounts) >= MIN_DISTINCT_DESCRIPTION_PRICES:
        evidence.append(f"lists {len(amounts)} prices: {_list_evidence([format_sgd(amount) for amount in amounts])}")
    if len(priced_lines) >= MIN_PRICED_VARIANT_LINES:
        evidence.append(f"has {len(priced_lines)} priced lines, e.g. '{_shorten(priced_lines[0])}'")
    if not evidence:
        return None
    return build_tag(TagCode.PRICE_LIST_IN_DESCRIPTION, "description " + "; ".join(evidence))


def priced_variant_lines(description: str) -> list[str]:
    """Lines shaped like "<variant> - $<amount>" or "<variant>: <amount>" whose label is not just "Price"."""
    searchable = _blank_out_phone_numbers(description)
    return [
        match.group(0).strip()
        for match in _PRICED_LINE.finditer(searchable)
        if _is_price_line(searchable, match) and not _label_words(match.group("label")) <= _GENERIC_PRICE_LABEL_WORDS
    ]


def listed_price_is_floor(listed_price: float | None, description: str) -> Tag | None:
    """Tag a listed price that is the cheapest of several the description offers, or a "from $X" price.

    Takes the description rather than pre-extracted amounts because the "from $X" wording is part of the evidence.
    """
    from_price = _FROM_PRICE.search(description)
    if from_price:
        return build_tag(TagCode.LISTED_PRICE_IS_FLOOR, f"description says '{from_price.group(0)}'")
    if listed_price is None:
        return None
    amounts = sorted(offered_prices(description))
    if len(amounts) >= 2 and abs(listed_price - amounts[0]) <= SAME_PRICE_TOLERANCE:
        reason = (
            f"listed {format_sgd(listed_price)} is the lowest of {len(amounts)} prices in the description "
            f"(up to {format_sgd(amounts[-1])})"
        )
        return build_tag(TagCode.LISTED_PRICE_IS_FLOOR, reason)
    return None


# --- price rules ------------------------------------------------------------------------------------


def price_below_market(listed_price: float | None, peer_prices: Sequence[float | None]) -> Tag | None:
    """Tag a price under BELOW_MARKET_FRACTION of the median of the other results in the same search."""
    known_peer_prices = [price for price in peer_prices if price is not None]
    if listed_price is None or len(known_peer_prices) < MIN_PEERS_FOR_MARKET:
        return None
    median_price = statistics.median(known_peer_prices)
    if listed_price >= BELOW_MARKET_FRACTION * median_price:
        return None
    reason = (
        f"listed {format_sgd(listed_price)} is {math.floor(100 * listed_price / median_price)}% of the median "
        f"{format_sgd(median_price)} of {len(known_peer_prices)} other results"
    )
    return build_tag(TagCode.PRICE_BELOW_MARKET, reason)


def price_not_for_your_item(resolved: ResolvedPrice, query: str) -> Tag | None:
    """Tag a listing whose description was read and gives no price for the searched variant."""
    if resolved.source != PriceSource.NOT_STATED:
        return None
    reason = f"the description states no price for '{query}'"
    return build_tag(TagCode.PRICE_NOT_FOR_YOUR_ITEM, f"{reason} ({resolved.note})" if resolved.note else reason)
