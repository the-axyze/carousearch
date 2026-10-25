"""The page's query string, to and from a SearchRequest. Pure functions: one tested mapping in both directions.

Names: q, price_min, price_max, condition (repeated), delivery (repeated), sort, category and cursor are the
search; hide_multi_item is a display option; shown, multi_item, hidden and screened carry the running counts
from one "Load more" page to the next. A malformed value is dropped rather than rejected, so a hand-edited or
stale URL still gives a search.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from enum import StrEnum
from urllib.parse import urlencode

from carousearch.bot.keyboard import SORT_CHOICES
from carousearch.filters.payload import format_price
from carousearch.filters.schema import Condition, DealMethod, SearchRequest, SortOrder

# Every value of each name, in order, as Starlette's QueryParams.getlist returns them.
QueryParams = Mapping[str, Sequence[str]]

# Carousell's own default page; at the 1,200 px content width the grid has 5 columns, so 20 fills 4 rows.
WEB_PAGE_SIZE = 20

QUERY = "q"
PRICE_MIN = "price_min"
PRICE_MAX = "price_max"
CONDITION = "condition"
DELIVERY = "delivery"
SORT = "sort"
CATEGORY = "category"
CURSOR = "cursor"
HIDE_MULTI_ITEM = "hide_multi_item"

TRUE_WORDS = frozenset({"1", "on", "true", "yes"})
# After "S$" and commas are stripped: "1200", "99.50"; no signs, exponents, non-ASCII digits or 3+ decimals.
PRICE_PATTERN = re.compile(r"[0-9]+(\.[0-9]{1,2})?")
WHOLE_NUMBER_PATTERN = re.compile(r"[0-9]+")


@dataclass(frozen=True)
class ResultsTally:
    """Counts over every page shown so far, for the results line. Field names double as the URL names."""

    shown: int = 0
    multi_item: int = 0
    hidden: int = 0
    screened: int = 0  # descriptions checked

    def plus(self, other: "ResultsTally") -> "ResultsTally":
        return ResultsTally(*(getattr(self, field.name) + getattr(other, field.name) for field in fields(self)))


def search_request_from_params(params: QueryParams) -> SearchRequest | None:
    """The search the URL describes, or None when there is nothing to search for (no keywords, no category)."""
    query = first_value(params, QUERY).strip() or None
    category = parse_category(first_value(params, CATEGORY))
    if query is None and category is None:
        return None
    price_min = parse_price(first_value(params, PRICE_MIN))
    price_max = parse_price(first_value(params, PRICE_MAX))
    price_min, price_max = ordered(price_min, price_max)
    return SearchRequest(
        query=query,
        category=category,
        price_min=price_min,
        price_max=price_max,
        conditions=parse_choices(params.get(CONDITION, ()), Condition),
        deal_methods=parse_choices(params.get(DELIVERY, ()), DealMethod),
        sort=parse_sort(first_value(params, SORT)),
        page_size=WEB_PAGE_SIZE,
        page_cursor=first_value(params, CURSOR).strip() or None,
    )


def params_from_search_request(request: SearchRequest) -> dict[str, list[str]]:
    """The inverse of search_request_from_params: only the fields the page sets, defaults left out."""
    params: dict[str, list[str]] = {}
    if request.query:
        params[QUERY] = [request.query]
    if request.price_min is not None:
        params[PRICE_MIN] = [format_price(request.price_min)]
    if request.price_max is not None:
        params[PRICE_MAX] = [format_price(request.price_max)]
    if request.conditions:
        params[CONDITION] = [condition.value for condition in request.conditions]
    if request.deal_methods:
        params[DELIVERY] = [method.value for method in request.deal_methods]
    if request.sort != SortOrder.BEST_MATCH:
        params[SORT] = [request.sort.value]
    if request.category:
        params[CATEGORY] = [request.category]
    if request.page_cursor:
        params[CURSOR] = [request.page_cursor]
    return params


def hide_multi_item_from_params(params: QueryParams) -> bool:
    return first_value(params, HIDE_MULTI_ITEM).strip().lower() in TRUE_WORDS


def tally_from_params(params: QueryParams) -> ResultsTally:
    """The counts of the pages already shown; a missing or malformed count is 0."""
    return ResultsTally(*(parse_count(first_value(params, field.name)) for field in fields(ResultsTally)))


def params_from_tally(tally: ResultsTally) -> dict[str, list[str]]:
    return {field.name: [str(getattr(tally, field.name))] for field in fields(tally)}


def page_url(request: SearchRequest | None, hide_multi_item: bool) -> str:
    """The bookmarkable address of a search's first page, e.g. "/?q=iphone+15&sort=recent"."""
    params = {} if request is None else params_from_search_request(request.model_copy(update={"page_cursor": None}))
    if hide_multi_item:
        params[HIDE_MULTI_ITEM] = ["1"]
    return "/?" + urlencode(params, doseq=True) if params else "/"


def load_more_url(request: SearchRequest, next_page: str, hide_multi_item: bool, tally: ResultsTally) -> str:
    """The /results address of the page after this one, carrying the counts so far."""
    params = params_from_search_request(request.model_copy(update={"page_cursor": next_page}))
    if hide_multi_item:
        params[HIDE_MULTI_ITEM] = ["1"]
    params.update(params_from_tally(tally))
    return "/results?" + urlencode(params, doseq=True)


# --- single values ---------------------------------------------------------------------------------------------


def first_value(params: QueryParams, name: str) -> str:
    values = params.get(name, ())
    return values[0] if values else ""


def parse_price(text: str) -> float | None:
    cleaned = text.strip().upper().removeprefix("S").removeprefix("$").replace(",", "").strip()
    return float(cleaned) if PRICE_PATTERN.fullmatch(cleaned) else None


def ordered(price_min: float | None, price_max: float | None) -> tuple[float | None, float | None]:
    """Swap a reversed range ("1200" to "800") instead of dropping it."""
    if price_min is not None and price_max is not None and price_min > price_max:
        return price_max, price_min
    return price_min, price_max


def parse_choices[Choice: StrEnum](values: Sequence[str], choice_type: type[Choice]) -> list[Choice]:
    """Known values in the enum's own order, each once; unknown ones are dropped."""
    wanted = {value.strip().lower() for value in values}
    return [choice for choice in choice_type if choice.value in wanted]


def parse_sort(text: str) -> SortOrder:
    """One of the page's sort choices; anything else (including "nearby", which needs a location) is best match."""
    by_value = {sort.value: sort for sort in SORT_CHOICES}
    return by_value.get(text.strip().lower(), SortOrder.BEST_MATCH)


def parse_category(text: str) -> str | None:
    cleaned = text.strip()
    return cleaned if WHOLE_NUMBER_PATTERN.fullmatch(cleaned) else None


def parse_count(text: str) -> int:
    cleaned = text.strip()
    return int(cleaned) if WHOLE_NUMBER_PATTERN.fullmatch(cleaned) else 0
