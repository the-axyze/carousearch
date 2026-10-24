"""Translate a SearchRequest into the JSON body of Carousell's search endpoint.

This is the only place that knows Carousell's filter codes and payload shape
(CAROUSELL_ACCESS_NOTES.md sections 3.2, 4.2 and 4.4). Pure functions, no I/O.
"""

from carousearch.filters.schema import Condition, DealMethod, SearchRequest, SortOrder

SINGAPORE_COUNTRY_CODE = "SG"
SINGAPORE_COUNTRY_ID = "1880251"

CONDITION_CODES: dict[Condition, str] = {
    Condition.BRAND_NEW: "3",
    Condition.LIKE_NEW: "4",
    Condition.LIGHTLY_USED: "7",
    Condition.WELL_USED: "5",
    Condition.HEAVILY_USED: "6",
}

SORT_CODES: dict[SortOrder, str] = {
    SortOrder.BEST_MATCH: "1",
    SortOrder.RECENT: "3",
    SortOrder.PRICE_LOW_TO_HIGH: "4",
    SortOrder.PRICE_HIGH_TO_LOW: "5",
    SortOrder.NEARBY: "6",
}

DEAL_METHOD_CODES: dict[DealMethod, str] = {
    DealMethod.MEETUP: "MEETUP",
    DealMethod.MAILING: "MAILING",
}

_CONDITIONS_BY_CODE: dict[str, Condition] = {code: condition for condition, code in CONDITION_CODES.items()}


def condition_from_code(code: str | int | None) -> Condition | None:
    """Map Carousell's layered condition code (e.g. 4) to a Condition; None if unknown."""
    if code is None:
        return None
    return _CONDITIONS_BY_CODE.get(str(code))


def build_search_payload(request: SearchRequest) -> dict:
    """Return the exact JSON body for POST /ds/search/cf/4.0/search/."""
    payload = {
        "bestMatchEnabled": True,
        # False stops Carousell from silently rewriting the query (access notes 7.5).
        "canChangeKeyword": False,
        "count": request.page_size,
        "countryCode": SINGAPORE_COUNTRY_CODE,
        "countryId": SINGAPORE_COUNTRY_ID,
        "filters": build_filters(request),
        "includeSuggestions": False,
        "locale": "en",
        "prefill": {},
        "query": request.query or "",
        "sortParam": {"fieldName": SORT_CODES[request.sort]},
    }
    if request.page_cursor:
        payload["session"] = request.page_cursor
    return payload


def build_filters(request: SearchRequest) -> list[dict]:
    filters = []
    if request.category:
        filters.append(ids_filter("collections", [request.category]))
    for field_name, value in request.category_fields.items():
        filters.append(ids_filter(field_name, value if isinstance(value, list) else [value]))
    if request.price_min is not None or request.price_max is not None:
        filters.append(price_filter(request.price_min, request.price_max))
    if request.conditions:
        filters.append(ids_filter("layered_condition", [CONDITION_CODES[c] for c in request.conditions]))
    if request.deal_methods:
        filters.append(ids_filter("_deal_method", [DEAL_METHOD_CODES[m] for m in request.deal_methods]))
    if request.buyer_protection_only:
        filters.append(boolean_filter("is_bp_enabled"))
    if request.free_delivery_only:
        filters.append(boolean_filter("shipping_offer_free_shipping"))
    return filters


def ids_filter(field_name: str, values: list[str]) -> dict:
    return {"fieldName": field_name, "idsOrKeywords": {"value": list(values)}, "enforce": True}


def price_filter(price_min: float | None, price_max: float | None) -> dict:
    price_range = {}
    if price_min is not None:
        price_range["start"] = {"value": format_price(price_min)}
    if price_max is not None:
        price_range["end"] = {"value": format_price(price_max)}
    return {"fieldName": "price", "rangedFloat": price_range, "enforce": True}


def boolean_filter(field_name: str) -> dict:
    return {"fieldName": field_name, "boolean": {"value": True}, "enforce": True}


def format_price(price: float) -> str:
    """Carousell expects prices as strings: 900 -> "900", 648.5 -> "648.5"."""
    if float(price).is_integer():
        return str(int(price))
    return str(price)
