import pytest

from carousearch.filters.payload import build_search_payload, condition_from_code, format_price
from carousearch.filters.schema import Condition, DealMethod, SearchRequest, SortOrder

from fixture_files import SEARCH_FIXTURE_NAMES, load_json_fixture


def filters_by_name(payload: dict) -> dict[str, dict]:
    return {search_filter["fieldName"]: search_filter for search_filter in payload["filters"]}


def test_minimal_request_has_carousell_defaults() -> None:
    payload = build_search_payload(SearchRequest(query="aeron chair"))

    assert payload == {
        "bestMatchEnabled": True,
        "canChangeKeyword": False,
        "count": 20,
        "countryCode": "SG",
        "countryId": "1880251",
        "filters": [],
        "includeSuggestions": False,
        "locale": "en",
        "prefill": {},
        "query": "aeron chair",
        "sortParam": {"fieldName": "1"},
    }


def test_price_and_condition_filters_use_carousell_codes() -> None:
    request = SearchRequest(
        query="herman miller aeron",
        price_min=300,
        price_max=900,
        conditions=[Condition.BRAND_NEW, Condition.LIKE_NEW, Condition.LIGHTLY_USED],
        sort=SortOrder.RECENT,
    )

    payload = build_search_payload(request)

    filters = filters_by_name(payload)
    assert filters["price"] == {
        "fieldName": "price",
        "rangedFloat": {"start": {"value": "300"}, "end": {"value": "900"}},
        "enforce": True,
    }
    assert filters["layered_condition"]["idsOrKeywords"] == {"value": ["3", "4", "7"]}
    assert payload["sortParam"] == {"fieldName": "3"}


def test_price_max_only_sends_open_start() -> None:
    payload = build_search_payload(SearchRequest(query="iphone 15", price_max=900))

    assert filters_by_name(payload)["price"]["rangedFloat"] == {"end": {"value": "900"}}


def test_category_fields_deal_method_and_flags() -> None:
    request = SearchRequest(
        query="iphone 15 pro",
        category="5707",
        category_fields={"mobile_storage": "MOBILE_STORAGE_256_GB", "mobile_color": ["MOBILE_COLOR_BLACK", "MOBILE_COLOR_BLUE"]},
        deal_methods=[DealMethod.MEETUP, DealMethod.MAILING],
        buyer_protection_only=True,
        free_delivery_only=True,
    )

    filters = filters_by_name(build_search_payload(request))

    assert filters["collections"]["idsOrKeywords"] == {"value": ["5707"]}
    assert filters["mobile_storage"]["idsOrKeywords"] == {"value": ["MOBILE_STORAGE_256_GB"]}
    assert filters["mobile_color"]["idsOrKeywords"] == {"value": ["MOBILE_COLOR_BLACK", "MOBILE_COLOR_BLUE"]}
    assert filters["_deal_method"]["idsOrKeywords"] == {"value": ["MEETUP", "MAILING"]}
    assert filters["is_bp_enabled"] == {"fieldName": "is_bp_enabled", "boolean": {"value": True}, "enforce": True}
    assert filters["shipping_offer_free_shipping"]["boolean"] == {"value": True}


def test_page_cursor_is_sent_as_session() -> None:
    payload = build_search_payload(SearchRequest(query="sofa", page_size=40, page_cursor="eyJhZ2dy"))

    assert payload["session"] == "eyJhZ2dy"
    assert payload["count"] == 40


def test_category_only_search_sends_empty_query() -> None:
    assert build_search_payload(SearchRequest(category="5707"))["query"] == ""


@pytest.mark.parametrize(("price", "expected"), [(900, "900"), (900.0, "900"), (648.5, "648.5"), (0, "0")])
def test_format_price(price: float, expected: str) -> None:
    assert format_price(price) == expected


@pytest.mark.parametrize(("code", "expected"), [(4, Condition.LIKE_NEW), ("7", Condition.LIGHTLY_USED), (99, None), (None, None)])
def test_condition_from_code(code: object, expected: Condition | None) -> None:
    assert condition_from_code(code) == expected


@pytest.mark.parametrize("fixture_name", SEARCH_FIXTURE_NAMES)
def test_fixture_request_still_translates_to_the_payload_that_was_sent(fixture_name: str) -> None:
    saved = load_json_fixture(f"{fixture_name}.request.json")

    request = SearchRequest.model_validate(saved["search_request"])

    assert build_search_payload(request) == saved["payload"]


@pytest.mark.parametrize("fixture_name", SEARCH_FIXTURE_NAMES)
def test_carousell_applied_every_filter_we_sent(fixture_name: str) -> None:
    """Carousell echoes the filters it applied in data.appliedContext; values come back as numbers or strings."""
    sent_filters = load_json_fixture(f"{fixture_name}.request.json")["payload"]["filters"]
    applied_filters = filters_by_name(load_json_fixture(f"{fixture_name}.json")["data"]["appliedContext"])

    for sent in sent_filters:
        applied = applied_filters[sent["fieldName"]]
        assert normalise_filter_values(applied) == normalise_filter_values(sent)


def normalise_filter_values(search_filter: dict) -> object:
    if "idsOrKeywords" in search_filter:
        return sorted(str(value) for value in search_filter["idsOrKeywords"]["value"])
    if "rangedFloat" in search_filter:
        return {end: float(bound["value"]) for end, bound in search_filter["rangedFloat"].items()}
    return search_filter.get("boolean")
