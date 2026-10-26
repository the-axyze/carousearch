"""URL <-> SearchRequest mapping of the web page. Pure; no app, no network."""

from urllib.parse import parse_qs, urlsplit

import pytest

from carousearch.filters.schema import Condition, DealMethod, SearchRequest, SortOrder
from carousearch.web.query_params import (
    WEB_PAGE_SIZE,
    PageMode,
    ResultsTally,
    hide_multi_item_from_params,
    load_more_url,
    mode_from_params,
    page_url,
    params_from_search_request,
    params_from_tally,
    search_request_from_params,
    tally_from_params,
)


def web_request(**fields) -> SearchRequest:
    return SearchRequest(page_size=WEB_PAGE_SIZE, **fields)


EVERY_CONTROL = web_request(
    query="iphone 15 pro",
    price_min=150.5,
    price_max=1200,
    conditions=[Condition.LIKE_NEW, Condition.LIGHTLY_USED],
    deal_methods=[DealMethod.MEETUP, DealMethod.MAILING],
    sort=SortOrder.PRICE_LOW_TO_HIGH,
    category="5707",
    page_cursor="eyJwYWdlIjoyfQ==",
)


def query_of(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


def test_every_control_round_trips() -> None:
    params = params_from_search_request(EVERY_CONTROL)
    assert params == {
        "q": ["iphone 15 pro"],
        "price_min": ["150.5"],
        "price_max": ["1200"],
        "condition": ["like_new", "lightly_used"],
        "delivery": ["meetup", "mailing"],
        "sort": ["price_low_to_high"],
        "category": ["5707"],
        "cursor": ["eyJwYWdlIjoyfQ=="],
    }
    assert search_request_from_params(params) == EVERY_CONTROL


@pytest.mark.parametrize(
    "sort", [SortOrder.BEST_MATCH, SortOrder.RECENT, SortOrder.PRICE_LOW_TO_HIGH, SortOrder.PRICE_HIGH_TO_LOW]
)
def test_each_sort_choice_round_trips(sort: SortOrder) -> None:
    request = web_request(query="sofa", sort=sort)
    assert search_request_from_params(params_from_search_request(request)) == request


def test_defaults_are_left_out_of_the_params() -> None:
    assert params_from_search_request(web_request(query="sofa")) == {"q": ["sofa"]}


def test_nothing_to_search_for_gives_none() -> None:
    assert search_request_from_params({}) is None
    assert search_request_from_params({"q": ["   "], "price_max": ["100"], "condition": ["like_new"]}) is None


def test_a_category_alone_is_a_search() -> None:
    assert search_request_from_params({"category": ["13"]}) == web_request(category="13")


def test_every_search_uses_the_web_page_size() -> None:
    assert search_request_from_params({"q": ["chair"]}).page_size == WEB_PAGE_SIZE


@pytest.mark.parametrize("text", ["abc", "-5", "1e3", "12.345", "nan", "inf", "١٢", "", "  "])
def test_a_malformed_price_is_ignored(text: str) -> None:
    request = search_request_from_params({"q": ["chair"], "price_min": [text], "price_max": [text]})
    assert (request.price_min, request.price_max) == (None, None)


@pytest.mark.parametrize(("text", "expected"), [("1200", 1200), ("1,200", 1200), ("S$1,200", 1200), ("$99.50", 99.5)])
def test_prices_may_be_written_like_prices(text: str, expected: float) -> None:
    assert search_request_from_params({"q": ["chair"], "price_max": [text]}).price_max == expected


def test_a_reversed_price_range_is_swapped() -> None:
    request = search_request_from_params({"q": ["chair"], "price_min": ["900"], "price_max": ["300"]})
    assert (request.price_min, request.price_max) == (300, 900)


@pytest.mark.parametrize("text", ["nearby", "cheapest", "", "price"])
def test_an_unknown_sort_falls_back_to_best_match(text: str) -> None:
    assert search_request_from_params({"q": ["chair"], "sort": [text]}).sort == SortOrder.BEST_MATCH


def test_sort_is_case_insensitive() -> None:
    assert search_request_from_params({"q": ["chair"], "sort": ["RECENT"]}).sort == SortOrder.RECENT


def test_unknown_and_repeated_choices_are_dropped_and_ordered() -> None:
    request = search_request_from_params(
        {
            "q": ["chair"],
            "condition": ["well_used", "mint", "brand_new", "well_used"],
            "delivery": ["courier", "meetup"],
        }
    )
    assert request.conditions == [Condition.BRAND_NEW, Condition.WELL_USED]
    assert request.deal_methods == [DealMethod.MEETUP]


@pytest.mark.parametrize("text", ["mobile-phones", "57o7", "²", "-13"])
def test_a_non_numeric_category_is_ignored(text: str) -> None:
    assert search_request_from_params({"q": ["chair"], "category": [text]}).category is None


@pytest.mark.parametrize(
    ("values", "expected"), [(["1"], True), (["on"], True), (["TRUE"], True), (["0"], False), ([], False)]
)
def test_hide_multi_item_flag(values: list[str], expected: bool) -> None:
    assert hide_multi_item_from_params({"hide_multi_item": values}) is expected


@pytest.mark.parametrize(
    ("values", "expected"),
    [(["ask"], PageMode.ASK), ([" ASK "], PageMode.ASK), (["filters"], PageMode.FILTERS), (["chat"], PageMode.FILTERS),
     ([], PageMode.FILTERS)],
)
def test_mode_is_ask_only_when_the_url_says_so(values: list[str], expected: PageMode) -> None:
    assert mode_from_params({"mode": values}) is expected


def test_tally_round_trips_and_malformed_counts_are_zero() -> None:
    tally = ResultsTally(shown=17, multi_item=3, hidden=3, screened=6)
    assert tally_from_params(params_from_tally(tally)) == tally
    assert tally_from_params({"shown": ["x"], "hidden": ["-2"], "screened": ["²"]}) == ResultsTally()


def test_tallies_add_up() -> None:
    assert ResultsTally(1, 2, 3, 4).plus(ResultsTally(10, 20, 30, 40)) == ResultsTally(11, 22, 33, 44)


def test_page_url_is_the_first_page_and_keeps_the_hide_flag() -> None:
    assert page_url(None, hide_multi_item=False) == "/"
    assert page_url(None, hide_multi_item=True) == "/?hide_multi_item=1"
    url = page_url(EVERY_CONTROL, hide_multi_item=True)
    assert url.startswith("/?")
    query = query_of(url)
    assert "cursor" not in query
    assert query["hide_multi_item"] == ["1"]
    assert query["condition"] == ["like_new", "lightly_used"]


def test_load_more_url_carries_the_cursor_the_counts_and_the_filters() -> None:
    url = load_more_url(web_request(query="chair", price_max=300), "next-cursor", True, ResultsTally(17, 3, 3, 6))
    assert url.startswith("/results?")
    query = query_of(url)
    assert query["cursor"] == ["next-cursor"]
    assert query["price_max"] == ["300"]
    assert query["hide_multi_item"] == ["1"]
    assert (query["shown"], query["multi_item"], query["hidden"], query["screened"]) == (["17"], ["3"], ["3"], ["6"])
    assert search_request_from_params(query).page_cursor == "next-cursor"
