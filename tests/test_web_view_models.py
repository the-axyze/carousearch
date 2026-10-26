"""What the web templates show: price display, badges, meta line, results line, form and detail views. Pure."""

from datetime import UTC, datetime

import pytest

from carousearch.carousell.errors import CarousellBlockedError, CarousellHTTPError, CarousellSchemaError
from carousearch.carousell.parse import parse_listing_detail_response, parse_search_response
from carousearch.filters.schema import Condition, DealMethod, Listing, SearchRequest, SortOrder
from carousearch.screening.models import PriceSource, ResolvedPrice, ScreenedListing, TagCode, build_tag
from carousearch.web.query_params import ResultsTally
from carousearch.web.view_models import (
    GENERIC_ERROR_MESSAGE,
    RATE_LIMITED_MESSAGE,
    SITE_CHANGED_MESSAGE,
    BadgeTone,
    PriceKind,
    ScreenedPage,
    badges_for,
    detail_view,
    error_message_for,
    format_day,
    format_month,
    hidden_rows,
    meta_line,
    price_display,
    results_summary,
    results_view,
    search_form_view,
    tally_page,
)

from fixture_files import load_json_fixture

TEMPLATE_LISTING = parse_search_response(load_json_fixture("search_price_condition.json")).listings[0]
TEMPLATE_DETAIL = parse_listing_detail_response(load_json_fixture("listing_detail.json"))
REQUEST = SearchRequest(query="iphone 15 pro 256gb", page_size=20)
TITLE_TAG = build_tag(TagCode.MULTI_ITEM_TITLE, "title names 3 variants: 15, 15 Pro, 15 Pro Max")
PRICE_LIST_TAG = build_tag(TagCode.PRICE_LIST_IN_DESCRIPTION, "description lists 4 prices")
BELOW_MARKET_TAG = build_tag(TagCode.PRICE_BELOW_MARKET, "listed S$100 is 13% of the median S$800")


def make_listing(listing_id: str = "100", price: float | None = 579, **fields) -> Listing:
    display = f"S${price:,.0f}" if price is not None else "Contact seller"
    update = {"id": listing_id, "price": price, "price_display": display, "raw": {}, **fields}
    return TEMPLATE_LISTING.model_copy(update=update)


def screened(
    listing: Listing | None = None, tags=(), source=PriceSource.LISTED, amount: float | None = 579, checked=False
) -> ScreenedListing:
    listing = listing or make_listing()
    return ScreenedListing(
        listing=listing,
        detail=TEMPLATE_DETAIL.model_copy(update={"id": listing.id, "raw": {}}) if checked else None,
        tags=list(tags),
        resolved_price=ResolvedPrice(amount=amount, source=source, note="iPhone 15 Pro 256GB - $850"),
    )


# --- the price row ------------------------------------------------------------------------------------------------


def test_listed_price_is_shown_plainly() -> None:
    display = price_display(screened())
    assert (display.kind, display.price, display.struck_price) == (PriceKind.LISTED, "S$579", None)
    assert display.caption is None


def test_a_different_description_price_is_shown_with_the_listed_price_struck() -> None:
    display = price_display(screened(tags=[TITLE_TAG], source=PriceSource.DESCRIPTION, amount=850))
    assert display.kind == PriceKind.RESOLVED
    assert (display.price, display.struck_price, display.caption) == ("S$850", "S$579", "from description")


def test_a_description_price_equal_to_the_listed_price_is_just_the_listed_price() -> None:
    assert price_display(screened(source=PriceSource.DESCRIPTION, amount=579)).kind == PriceKind.LISTED


def test_a_description_source_without_an_amount_falls_back_to_the_listed_price() -> None:
    assert price_display(screened(source=PriceSource.DESCRIPTION, amount=None)).kind == PriceKind.LISTED


def test_not_stated_keeps_the_listed_price_with_a_caption() -> None:
    display = price_display(screened(tags=[TITLE_TAG], source=PriceSource.NOT_STATED, amount=None))
    assert display.kind == PriceKind.NOT_STATED
    assert (display.price, display.struck_price, display.caption) == ("S$579", None, "price for your item not stated")


# --- badges and the meta line -------------------------------------------------------------------------------------


def test_no_tags_no_badges() -> None:
    assert badges_for(screened()) == []


def test_multi_item_badge_lists_every_warning_reason() -> None:
    [badge] = badges_for(screened(tags=[TITLE_TAG, PRICE_LIST_TAG]))
    assert (badge.label, badge.tone) == ("Multi-item", BadgeTone.AMBER)
    assert badge.reasons == (
        "Multi-item title: title names 3 variants: 15, 15 Pro, 15 Pro Max\nPrice list: description lists 4 prices"
    )


def test_below_market_alone_is_a_grey_low_price_badge() -> None:
    [badge] = badges_for(screened(tags=[BELOW_MARKET_TAG]))
    assert (badge.label, badge.tone, badge.reasons) == (
        "Low price",
        BadgeTone.GREY,
        "Below market: listed S$100 is 13% of the median S$800",
    )


def test_multi_item_comes_before_low_price() -> None:
    badges = badges_for(screened(tags=[BELOW_MARKET_TAG, TITLE_TAG]))
    assert [badge.label for badge in badges] == ["Multi-item", "Low price"]


def test_meta_line_has_condition_likes_and_seller() -> None:
    listing = make_listing(condition_label="Lightly used", like_count=3, seller_username="grandstone")
    assert meta_line(listing) == "Lightly used · 3 likes · grandstone"
    assert meta_line(listing.model_copy(update={"like_count": 1})) == "Lightly used · 1 like · grandstone"


def test_meta_line_leaves_out_what_is_missing() -> None:
    listing = make_listing(condition_label=None, like_count=0, seller_username="grandstone")
    assert meta_line(listing) == "grandstone"


# --- the results region -------------------------------------------------------------------------------------------


def mixed_page(next_page: str | None = "cursor-2") -> ScreenedPage:
    return ScreenedPage(
        listings=[
            screened(make_listing("1"), tags=[TITLE_TAG], checked=True),
            screened(make_listing("2"), tags=[BELOW_MARKET_TAG], checked=True),
            screened(make_listing("3")),
            screened(make_listing("4"), tags=[TITLE_TAG]),
        ],
        total_display="1,000+",
        next_page=next_page,
    )


def test_tally_counts_multi_item_hidden_and_screened() -> None:
    page = mixed_page().listings
    assert tally_page(page, hide_multi_item=False) == ResultsTally(shown=4, multi_item=2, hidden=0, screened=2)
    assert tally_page(page, hide_multi_item=True) == ResultsTally(shown=2, multi_item=2, hidden=2, screened=2)


def test_results_view_hides_multi_item_cards_and_says_so() -> None:
    view = results_view(mixed_page(), REQUEST, True, ResultsTally(), price_lookup_enabled=True)
    assert [card.listing_id for card in view.cards] == ["2", "3"]
    assert view.summary == "2 of 1,000+ results · 2 multi-item hidden · screened 2 descriptions"
    assert "cursor=cursor-2" in view.load_more_url
    assert "hidden=2" in view.load_more_url
    assert not view.nothing_found


def test_hidden_rows_list_the_hidden_posts_with_their_warning_reasons() -> None:
    page = mixed_page().listings + [screened(make_listing("5"), tags=[TITLE_TAG, PRICE_LIST_TAG, BELOW_MARKET_TAG])]
    rows = hidden_rows(page, hide_multi_item=True)
    assert [row.listing_id for row in rows] == ["1", "4", "5"]
    assert (rows[0].title, rows[0].url, rows[0].price) == (
        TEMPLATE_LISTING.title,
        TEMPLATE_LISTING.listing_url,
        "S$579",
    )
    assert rows[2].reasons == [
        "Multi-item title: title names 3 variants: 15, 15 Pro, 15 Pro Max",
        "Price list: description lists 4 prices",
    ]


def test_hidden_rows_are_empty_when_the_switch_is_off() -> None:
    assert hidden_rows(mixed_page().listings, hide_multi_item=False) == []


def test_hidden_row_reasons_are_the_multi_item_badge_tooltip_lines() -> None:
    item = screened(tags=[BELOW_MARKET_TAG, TITLE_TAG, PRICE_LIST_TAG])
    [row] = hidden_rows([item], hide_multi_item=True)
    [multi_item_badge, _low_price] = badges_for(item)
    assert row.reasons == multi_item_badge.reasons.split("\n")


def test_results_view_lists_this_pages_hidden_posts_and_counts_every_page() -> None:
    earlier = ResultsTally(shown=6, multi_item=2, hidden=2, screened=3)
    view = results_view(mixed_page(), REQUEST, True, earlier, price_lookup_enabled=True)
    assert view.hide_multi_item
    assert [row.listing_id for row in view.hidden] == ["1", "4"]
    assert view.hidden == hidden_rows(mixed_page().listings, hide_multi_item=True)
    assert view.hidden_summary == "Hidden (4)"


def test_results_view_without_the_switch_has_no_hidden_section() -> None:
    view = results_view(mixed_page(), REQUEST, False, ResultsTally(), price_lookup_enabled=True)
    assert (view.hide_multi_item, view.hidden, view.hidden_summary) == (False, [], "Hidden (0)")


def test_results_view_adds_the_earlier_pages_and_drops_load_more_on_the_last_page() -> None:
    earlier = ResultsTally(shown=20, multi_item=1, hidden=0, screened=3)
    view = results_view(mixed_page(next_page=None), REQUEST, False, earlier, price_lookup_enabled=True)
    assert len(view.cards) == 4
    assert view.summary == "24 results · 3 multi-item · screened 5 descriptions"
    assert view.load_more_url is None


def test_results_view_of_an_empty_search_is_nothing_found() -> None:
    view = results_view(ScreenedPage([], "0", None), REQUEST, False, ResultsTally(), price_lookup_enabled=True)
    assert view.nothing_found
    assert view.cards == []


def test_summary_mentions_price_lookup_off_only_when_it_matters() -> None:
    assert results_summary(ResultsTally(5, 1, 0, 1), None, False, False) == (
        "5 results · 1 multi-item · screened 1 description · price lookup off"
    )
    assert results_summary(ResultsTally(1, 0, 0, 0), None, False, False) == "1 result"


# --- the form -----------------------------------------------------------------------------------------------------


def test_form_without_a_search_is_blank() -> None:
    form = search_form_view(None, hide_multi_item=False)
    assert (form.query, form.price_min, form.price_max, form.hide_multi_item) == ("", "", "", False)
    assert not any(choice.selected for choice in form.conditions + form.deliveries)
    assert [choice.label for choice in form.sorts] == ["Best match", "Recent", "Price ↑", "Price ↓"]
    assert [choice.value for choice in form.sorts if choice.selected] == ["best_match"]
    assert form.categories[0].label == "Any category" and form.categories[0].selected


def test_form_shows_the_search() -> None:
    request = SearchRequest(
        query="chair",
        price_min=100,
        price_max=250.5,
        conditions=[Condition.LIKE_NEW],
        deal_methods=[DealMethod.MAILING],
        sort=SortOrder.RECENT,
        category="13",
    )
    form = search_form_view(request, hide_multi_item=True)
    assert (form.query, form.price_min, form.price_max, form.hide_multi_item) == ("chair", "100", "250.5", True)
    assert [choice.value for choice in form.conditions if choice.selected] == ["like_new"]
    assert [choice.label for choice in form.deliveries if choice.selected] == ["Mailing"]
    assert [choice.label for choice in form.sorts if choice.selected] == ["Recent"]
    assert [choice.label for choice in form.categories if choice.selected] == ["Furniture"]


def test_an_uncommon_category_from_the_url_is_offered_and_selected() -> None:
    form = search_form_view(SearchRequest(category="9999"), hide_multi_item=False)
    assert form.categories[-1].label == "Collection 9999" and form.categories[-1].selected


# --- the detail panel ---------------------------------------------------------------------------------------------


def test_detail_view_lists_facts_and_every_tag() -> None:
    item = screened(tags=[TITLE_TAG, BELOW_MARKET_TAG], source=PriceSource.DESCRIPTION, amount=850)
    view = detail_view(TEMPLATE_DETAIL, item)
    facts = {fact.label: fact.lines for fact in view.facts}
    assert facts["Seller"] == ["urameshi1984"]
    assert facts["Rating"] == ["4.95 ★ · 88 reviews"]
    assert facts["Joined"] == ["May 2019"]
    assert facts["Response rate"] == ["High"]
    assert facts["Meet-up"][0] == "Punggol MRT/LRT Interchange (NE17/PTC)" and len(facts["Meet-up"]) == 3
    assert facts["Posted"] == ["24 Oct 2026"]  # 17:03 UTC on the 23rd is the 24th in Singapore
    assert facts["Price check"] == ["iPhone 15 Pro 256GB - $850"]
    tags = [(tag.label, tag.tone) for tag in view.tags]
    assert tags == [("Multi-item title", BadgeTone.AMBER), ("Below market", BadgeTone.GREY)]
    assert len(view.photo_urls) == 7
    assert view.screened and not view.description_is_long


def test_detail_view_without_a_screening_has_no_tags_and_skips_missing_facts() -> None:
    detail = TEMPLATE_DETAIL.model_copy(
        update={"seller_rating": None, "seller_response_rate": None, "meetup_locations": [], "description": None}
    )
    view = detail_view(detail, None)
    labels = [fact.label for fact in view.facts]
    assert "Rating" not in labels and "Response rate" not in labels and "Meet-up" not in labels
    assert "Price check" not in labels
    assert (view.tags, view.screened, view.description) == ([], False, "")


@pytest.mark.parametrize(("description", "is_long"), [("short", False), ("line\n" * 13, True), ("x" * 701, True)])
def test_long_descriptions_start_clamped(description: str, is_long: bool) -> None:
    detail = TEMPLATE_DETAIL.model_copy(update={"description": description})
    assert detail_view(detail, None).description_is_long is is_long


def test_dates_read_in_singapore_time() -> None:
    late_evening_utc = datetime(2019, 5, 10, 17, 0, tzinfo=UTC)
    assert format_day(late_evening_utc) == "11 May 2019"
    assert format_month(late_evening_utc) == "May 2019"
    assert format_day(None) is None


# --- errors -------------------------------------------------------------------------------------------------------


def test_error_messages_are_calm_and_specific() -> None:
    assert error_message_for(CarousellBlockedError("challenge")) == RATE_LIMITED_MESSAGE
    assert error_message_for(CarousellSchemaError("missing key 'data'")) == SITE_CHANGED_MESSAGE
    assert SITE_CHANGED_MESSAGE.command == "carousearch canary"
    assert error_message_for(CarousellHTTPError("HTTP 500", 500)) == GENERIC_ERROR_MESSAGE
    assert error_message_for(RuntimeError("bug")) == GENERIC_ERROR_MESSAGE
