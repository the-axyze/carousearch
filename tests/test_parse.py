import copy
from datetime import UTC, datetime

import pytest

from carousearch.carousell.errors import CarousellBlockedError, CarousellSchemaError
from carousearch.carousell.parse import (
    extract_csrf_token,
    long_to_int,
    parse_card_timestamp,
    parse_category_filters_response,
    parse_display_price,
    parse_listing_card,
    parse_listing_detail_response,
    parse_search_response,
    raise_if_blocked,
)
from carousearch.filters.payload import condition_from_code
from carousearch.filters.schema import CategoryFilter, CategoryFilterOption, Condition, PostedOrBumpedKind

from fixture_files import listing_cards, load_json_fixture, load_text_fixture

# --- search -----------------------------------------------------------------


def test_search_with_price_and_condition_filters() -> None:
    response = load_json_fixture("search_price_condition.json")
    sent_request = load_json_fixture("search_price_condition.request.json")["search_request"]

    result = parse_search_response(response)

    assert len(result.listings) == len(listing_cards(response)) > 0
    for listing in result.listings:
        assert sent_request["price_min"] <= listing.price <= sent_request["price_max"]
        assert listing.condition in {Condition(name) for name in sent_request["conditions"]}
        assert listing.currency == "SGD"
        assert listing.listing_url == f"https://www.carousell.sg/p/{listing.id}/"
    assert result.next_page == response["data"]["session"]
    assert result.total == response["data"]["total"]["value"]["low"]
    assert result.total_display == response["data"]["formattedTotal"]


def test_search_with_category_fields() -> None:
    response = load_json_fixture("search_category_fields.json")

    result = parse_search_response(response)

    assert [listing.id for listing in result.listings] == [str(card["id"]) for card in listing_cards(response)]
    assert all(listing.seller_username for listing in result.listings)


def test_listing_card_fields_match_the_raw_card() -> None:
    card = listing_cards(load_json_fixture("search_price_condition.json"))[0]

    listing = parse_listing_card(card)

    assert listing.id == str(card["id"])
    assert listing.title == card["title"]
    assert listing.price_display == card["price"]
    assert listing.price == float(card["price"].removeprefix("S$").replace(",", ""))
    assert listing.condition == condition_from_code(card["condition"])
    assert listing.condition_label in {"Brand new", "Like new", "Lightly used", "Well used", "Heavily used"}
    assert listing.like_count == card.get("likesCount", 0)
    assert listing.seller_username == card["seller"]["username"]
    assert listing.seller_id == str(card["seller"]["id"])
    assert listing.thumbnail_url == card["photoUrls"][0]
    seconds = card["aboveFold"][0]["timestampContent"]["seconds"]["low"]
    assert listing.posted_or_bumped_at == datetime.fromtimestamp(seconds, UTC)
    assert listing.posted_or_bumped_kind == PostedOrBumpedKind(card["aboveFold"][0]["component"])
    assert listing.raw == card
    assert "raw" not in repr(listing)


def test_missing_like_count_means_zero() -> None:
    card = copy.deepcopy(listing_cards(load_json_fixture("search_price_condition.json"))[0])
    card.pop("likesCount", None)

    assert parse_listing_card(card).like_count == 0


def test_legacy_used_condition_has_a_label_but_no_condition() -> None:
    """Old listings carry card condition 2 ("Used"), which is not one of the five filterable conditions."""
    card = copy.deepcopy(listing_cards(load_json_fixture("search_price_condition.json"))[0])
    card["condition"] = 2

    listing = parse_listing_card(card)

    assert listing.condition is None
    assert listing.condition_label == "Used"


def test_banners_and_other_result_types_are_skipped() -> None:
    response = load_json_fixture("search_price_condition.json")
    card_count = len(listing_cards(response))
    response["data"]["results"].insert(1, {"banner": {"bannerType": "bulky_delivery"}})

    assert len(parse_search_response(response).listings) == card_count


def test_last_page_has_no_next_page() -> None:
    response = load_json_fixture("search_price_condition.json")
    response["data"]["hasMore"] = False

    assert parse_search_response(response).next_page is None


def test_search_without_matches_has_no_listings_and_no_next_page() -> None:
    response = load_json_fixture("search_no_results.json")
    assert "results" not in response["data"]  # Carousell omits the key when nothing matches

    result = parse_search_response(response)

    assert result.listings == []
    assert result.total == 0
    assert result.total_display == response["data"]["formattedTotal"] == "0"
    assert result.next_page is None


@pytest.mark.parametrize("total", [{"value": {"low": 36, "high": 0}}, None], ids=["nonzero total", "no total"])
def test_missing_results_without_a_zero_total_raises_schema_error_naming_the_key(total: dict | None) -> None:
    response = load_json_fixture("search_price_condition.json")
    del response["data"]["results"]
    response["data"].pop("total")
    if total is not None:
        response["data"]["total"] = total

    with pytest.raises(CarousellSchemaError, match=r"'data\.results'"):
        parse_search_response(response)


@pytest.mark.parametrize("response", [{}, {"data": None}, {"data": []}, {"errors": [{"message": "x"}]}, []])
def test_missing_or_malformed_data_raises_schema_error(response: object) -> None:
    with pytest.raises(CarousellSchemaError, match=r"'data'"):
        parse_search_response(response)


def test_missing_card_title_raises_schema_error_naming_the_key() -> None:
    response = load_json_fixture("search_price_condition.json")
    first_card = listing_cards(response)[0]
    del first_card["title"]

    with pytest.raises(CarousellSchemaError, match=rf"'listingCard\[{first_card['id']}\]\.title'"):
        parse_search_response(response)


def test_missing_session_while_has_more_raises_schema_error() -> None:
    response = load_json_fixture("search_price_condition.json")
    response["data"]["hasMore"] = True
    del response["data"]["session"]

    with pytest.raises(CarousellSchemaError, match=r"'data\.session'"):
        parse_search_response(response)


def test_wrong_value_type_raises_schema_error() -> None:
    response = load_json_fixture("search_price_condition.json")
    listing_cards(response)[0]["likesCount"] = "lots"

    with pytest.raises(CarousellSchemaError, match="like_count"):
        parse_search_response(response)


# --- listing detail -----------------------------------------------------------


def test_listing_detail() -> None:
    response = load_json_fixture("listing_detail.json")
    raw_listing = response["data"]["screens"][0]["meta"]["default_value"]
    raw_seller = raw_listing["seller"]

    detail = parse_listing_detail_response(response)

    assert detail.id == str(raw_listing["id"])
    assert detail.title == raw_listing["title"]
    assert detail.price == float(raw_listing["price"])
    assert detail.currency == raw_listing["currency_code"]
    assert detail.condition == condition_from_code(raw_listing["layered_condition"])
    assert detail.condition_label == raw_listing["listing_attributes"]["condition"]
    assert detail.description == raw_listing["description"]
    assert detail.image_urls == [photo["image_url"] for photo in raw_listing["photos"]]
    assert detail.created_at == datetime.fromisoformat(raw_listing["time_created"])
    assert detail.seller_username == raw_seller["username"]
    assert detail.seller_rating == raw_seller["feedback_score"]
    assert detail.seller_review_count == raw_seller["feedback_count"]
    assert detail.seller_joined_at == datetime.fromisoformat(raw_seller["date_joined"])
    assert detail.seller_response_rate == raw_seller["response_rate"]
    assert detail.seller_verification.email_verified == raw_seller["profile"]["is_email_verified"]
    assert detail.seller_verification.id_verified == raw_seller["profile"]["is_id_verified"]
    assert detail.meetup_available == raw_listing["meetup"]
    assert [meetup.name for meetup in detail.meetup_locations] == [m["name"] for m in raw_listing["meetups"]]


def test_listing_detail_names_its_category() -> None:
    detail = parse_listing_detail_response(load_json_fixture("listing_detail.json"))

    assert (detail.category_id, detail.category_name) == ("6359", "iPhone 15 Series")


def test_listing_detail_leaves_unprovided_fields_empty() -> None:
    response = load_json_fixture("listing_detail.json")
    raw_listing = response["data"]["screens"][0]["meta"]["default_value"]
    del raw_listing["seller"]["profile"]
    del raw_listing["description"]

    detail = parse_listing_detail_response(response)

    assert detail.seller_verification is None
    assert detail.description is None
    assert detail.thumbnail_url is None  # the detail endpoint has no thumbnail
    assert detail.posted_or_bumped_at is None  # nor bump times


def test_listing_detail_without_listing_object_raises_schema_error() -> None:
    response = load_json_fixture("listing_detail.json")
    for screen in response["data"]["screens"]:
        screen["meta"]["default_value"] = None

    with pytest.raises(CarousellSchemaError, match="default_value"):
        parse_listing_detail_response(response)


def test_listing_detail_without_seller_raises_schema_error() -> None:
    response = load_json_fixture("listing_detail.json")
    raw_listing = response["data"]["screens"][0]["meta"]["default_value"]
    del raw_listing["seller"]

    with pytest.raises(CarousellSchemaError, match=rf"'listing\[{raw_listing['id']}\]\.seller'"):
        parse_listing_detail_response(response)


# --- bootstrap page and block detection -----------------------------------------


def test_csrf_token_is_read_from_bootstrap_page() -> None:
    token = extract_csrf_token(load_text_fixture("bootstrap_page.html"))

    assert len(token) > 20


def test_page_without_initial_state_raises_schema_error() -> None:
    with pytest.raises(CarousellSchemaError, match="csrfToken"):
        extract_csrf_token("<html><body>Maintenance</body></html>")


def test_cloudflare_challenge_raises_blocked_error() -> None:
    challenge = load_json_fixture("cloudflare_challenge.json")

    with pytest.raises(CarousellBlockedError, match="Cloudflare challenge"):
        raise_if_blocked(challenge["status_code"], challenge["headers"], challenge["body"])


def test_challenge_page_without_header_is_recognised_by_title() -> None:
    challenge = load_json_fixture("cloudflare_challenge.json")

    with pytest.raises(CarousellBlockedError, match="Just a moment"):
        raise_if_blocked(403, {"Content-Type": "text/html"}, challenge["body"])


def test_rate_limit_response_raises_blocked_error() -> None:
    with pytest.raises(CarousellBlockedError, match="429"):
        raise_if_blocked(429, {"content-type": "application/json"}, "{}")


def test_normal_responses_are_not_blocked() -> None:
    raise_if_blocked(200, {"content-type": "text/html"}, load_text_fixture("bootstrap_page.html"))
    raise_if_blocked(200, {"content-type": "application/json"}, '{"data": {"title": "captcha"}}')


# --- small helpers ------------------------------------------------------------


@pytest.mark.parametrize(
    ("display", "expected"),
    [("S$680", (680.0, "SGD")), ("S$1,400", (1400.0, "SGD")), ("S$648.88", (648.88, "SGD")), ("S$0", (0.0, "SGD")),
     ("120", (120.0, None)), ("Free", (None, None)), ("S$1,000 - S$2,000", (None, None))],
)
def test_parse_display_price(display: str, expected: tuple) -> None:
    assert parse_display_price(display) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(5, 5), ("36", 36), ({"low": 36, "high": 0, "unsigned": False}, 36), ({"low": -1, "high": 0, "unsigned": True}, 2**32 - 1),
     ({"low": 0, "high": 1}, 2**32), (None, None), (True, None), ("n/a", None)],
)
def test_long_to_int(value: object, expected: int | None) -> None:
    assert long_to_int(value) == expected


def test_card_timestamp_ignores_unknown_components() -> None:
    above_fold = [
        {"component": "something_new", "timestampContent": {"seconds": {"low": 1}}},
        {"component": "active_bump", "timestampContent": {"seconds": {"low": 1792734669, "high": 0}}},
    ]

    assert parse_card_timestamp(above_fold) == (PostedOrBumpedKind.BUMPED, datetime.fromtimestamp(1792734669, UTC))
    assert parse_card_timestamp(None) == (None, None)


# --- category filters --------------------------------------------------------


def test_category_filters_are_read_in_the_sites_order_with_their_values() -> None:
    filters = parse_category_filters_response(load_json_fixture("category_filters_class_2b.json"))

    assert [(item.field_name, item.filter_type, len(item.options)) for item in filters] == [
        ("sort_by", None, 5),
        ("vehicle_motorbike_type", "id_or_keyword", 12),
        ("make", "id_or_keyword", 48),
        ("price_start", "float_range_start", 0),
        ("price_end", "float_range_end", 0),
        ("deal_options", "id_or_keyword", 2),
    ]
    assert filters[1].label == "Body"
    assert filters[5].options == [
        CategoryFilterOption(value="MEETUP", label="Meet-up"), CategoryFilterOption(value="MAILING", label="Mailing & Delivery")
    ]


def test_category_fields_without_a_field_name_or_a_value_are_skipped() -> None:
    response = {"data": {"filters": {"screens": [{"groups": [{"fields": [
        {"meta": {}, "ui_rules": {}},
        {"meta": {"field_name": "make", "filter_type": "id_or_keyword"},
         "ui_rules": {"label": "Make", "options": [{"display_name": "Honda"}, {"display_name": "BMW", "value": "BMW"}]}},
    ]}]}]}}}

    assert parse_category_filters_response(response) == [
        CategoryFilter(field_name="make", label="Make", filter_type="id_or_keyword",
                       options=[CategoryFilterOption(value="BMW", label="BMW")])
    ]


def test_a_response_without_the_fieldset_raises_schema_error() -> None:
    with pytest.raises(CarousellSchemaError, match="data.filters"):
        parse_category_filters_response(load_json_fixture("search_price_condition.json"))
