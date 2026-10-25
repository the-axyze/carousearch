from carousearch.carousell.parse import parse_search_response
from carousearch.screening.models import (
    PriceSource,
    ResolvedPrice,
    ScreenedListing,
    TagCode,
    build_tag,
    format_sgd,
)

from fixture_files import load_json_fixture

LISTING = parse_search_response(load_json_fixture("search_price_condition.json")).listings[0]  # S$579
MULTI_ITEM_TAG = build_tag(TagCode.MULTI_ITEM_TITLE, "title names 3 variants: 15, 15 Pro, 15 Pro Max")
BELOW_MARKET_TAG = build_tag(TagCode.PRICE_BELOW_MARKET, "listed S$579 is 55% of the median S$1,050 of 9 other results")


def screened(tags: list, resolved: ResolvedPrice) -> ScreenedListing:
    return ScreenedListing(listing=LISTING, tags=tags, resolved_price=resolved)


def test_summary_line_for_each_kind_of_resolved_price() -> None:
    listed = ResolvedPrice(amount=579, source=PriceSource.LISTED)
    from_description = ResolvedPrice(amount=850, source=PriceSource.DESCRIPTION, note="15 Pro 256GB - $850")
    not_stated = ResolvedPrice(amount=None, source=PriceSource.NOT_STATED)

    assert screened([MULTI_ITEM_TAG], from_description).summary_line() == (
        "Multi-item · listed S$579 · your variant S$850 (description)"
    )
    assert screened([MULTI_ITEM_TAG], not_stated).summary_line() == (
        "Multi-item · listed S$579 · no price stated for your variant"
    )
    assert screened([MULTI_ITEM_TAG], listed).summary_line() == (
        "Multi-item · listed S$579 · may not be your variant's price"
    )
    assert screened([BELOW_MARKET_TAG], listed).summary_line() == "Below market · listed S$579"
    assert screened([], listed).summary_line() == "listed S$579"


def test_only_warn_tags_make_a_listing_multi_item() -> None:
    listed = ResolvedPrice(amount=579, source=PriceSource.LISTED)

    assert screened([MULTI_ITEM_TAG], listed).is_multi_item is True
    assert screened([BELOW_MARKET_TAG], listed).is_multi_item is False
    assert screened([], listed).is_multi_item is False


def test_json_carries_is_multi_item_and_reads_back() -> None:
    item = screened([MULTI_ITEM_TAG], ResolvedPrice(amount=850, source=PriceSource.DESCRIPTION))

    dumped = item.model_dump(mode="json", exclude={"listing": {"raw"}})

    assert dumped["is_multi_item"] is True
    assert dumped["tags"][0] == {
        "code": "MULTI_ITEM_TITLE",
        "label": "Multi-item title",
        "reason": "title names 3 variants: 15, 15 Pro, 15 Pro Max",
        "severity": "warn",
    }
    assert dumped["resolved_price"] == {"amount": 850.0, "source": "description", "note": None}
    assert ScreenedListing.model_validate(dumped).tags == item.tags


def test_format_sgd() -> None:
    assert format_sgd(1299) == "S$1,299"
    assert format_sgd(12.5) == "S$12.50"
