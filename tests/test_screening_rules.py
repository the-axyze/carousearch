import pytest

from carousearch.carousell.parse import parse_listing_detail_response, parse_search_response
from carousearch.screening.models import PriceSource, ResolvedPrice, Severity, TagCode
from carousearch.screening.rules import (
    BELOW_MARKET_FRACTION,
    MIN_PEERS_FOR_MARKET,
    description_has_price_list,
    description_tags,
    extract_prices,
    listed_price_is_floor,
    offered_prices,
    price_below_market,
    price_not_for_your_item,
    title_lists_multiple_variants,
)

from fixture_files import SEARCH_FIXTURE_NAMES, load_json_fixture

# Real titles from searches made with the bot on 2026-10-25 (seen_listings), besides the fixture titles.
SWITCH_CATALOGUE_TITLE = "💎(Accept CDC) Nintendo Switch OLED / Switch 2 / Switch Console Local Set + Warranty"
MOTORBIKE_KEYWORD_TITLE = (
    "2031 Aerox V2 (Key) Yamaha | ADV150 |AEROX | NMAX | SNIPER | R15 | ADV150 | MT15 | CB150R | XSR155 | XMAX "
    "| CB400 | ADV160 | YAMAHA | HONDA |"
)
MOTORBIKE_CATALOGUE_TITLE = (
    "NEW R15M & XSR155 & MT15 & T155 ( YAMAHA R15M CARBON / R15V4 / R15 & XSR 155 & MT-15 & SNIPER & EXCITER & "
    "NMAX & AEROX & MX KING / MXKING & HONDA ADV160 & PCX & CB150X / CB150 & CB200X & NX200 )"
)
REAL_SINGLE_ITEM_TITLES = [
    "Chanel French Riviera - Caviar 29CM Quilted Seasonal Flap Red Silver Hardware Series 15",
    "(COE 2030) Pre-owned Yamaha R15 V3‼️",
    "2019 Yamaha YZF R15",
    "R15 V2 2017",
    "LF for r15 v3",
    "Nintendo Switch Gen 1 Soft-modded",
    "iPhone 15 Plus Black 256GB",
]


def fixture_titles() -> list[str]:
    titles = []
    for name in SEARCH_FIXTURE_NAMES:
        titles += [listing.title for listing in parse_search_response(load_json_fixture(f"{name}.json")).listings]
    return titles


# --- extract_prices ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Selling at $850", [850]),
        ("S$1,299 nett", [1299]),
        ("SGD 650 only", [650]),
        ("650 SGD", [650]),
        ("letting go 650sgd", [650]),
        ("1.2k", [1200]),
        ("$1.5k firm", [1500]),
        ("$12.50 each", [12.5]),
        ("price is $1,299.00", [1299]),
        ("Price: 850", [850]),
        ("Selling for 900 nego", [900]),
        ("850 nett, meet at Tampines", [850]),
        ("selling at 850 in good condition", [850]),
        ("15 Pro 256GB - 850\n15 Pro Max 256GB - 1050", [850, 1050]),
        ("256GB - $850, 512GB - $1,050", [850, 1050]),
        ("$500 - $600", [500, 600]),
        # storage sizes, percentages, durations and counts are not prices
        ("iPhone 15 Pro 256GB", []),
        ("Storage: 256GB", []),
        ("50% off", []),
        ("Price 50% off", []),
        ("Battery health 89%", []),
        ("Battery health: 89\nCycle count: 120", []),
        ("used for 8 months, only 2 months old", []),
        ("Warranty: 6 months", []),
        ("Selling 2 sets", []),
        ("4K video", []),
        # years
        ("Bought in 2023, still under warranty", []),
        ("Year: 2023", []),
        # Singapore phone numbers
        ("Contact 9123 4567", []),
        ("WhatsApp +65 81234567 for details", []),
        ("Call 91234567 or $850", [850]),
        # the fixture's real description
        ("Selling a perfect condition iphone 15. No box and cable. Low ballers will be ignore.", []),
        ("", []),
    ],
)
def test_extract_prices(text: str, expected: list[float]) -> None:
    assert extract_prices(text) == expected


def test_offered_prices_leave_out_reference_prices() -> None:
    assert offered_prices("Bought at $1,299, selling at $850. Retail $1,599") == [850]
    assert offered_prices("Was $900, now $850") == [850]
    assert offered_prices("$579, $579 and $650") == [579, 650]


# --- title rule ------------------------------------------------------------------------------------------


def test_title_naming_three_models_is_multi_item() -> None:
    tag = title_lists_multiple_variants("iPhone 15 / 15 Pro / 15 Pro Max all storage", "iphone 15 pro 256gb")

    assert tag.code == TagCode.MULTI_ITEM_TITLE
    assert tag.severity == Severity.WARN
    assert tag.reason == "title names 3 variants: iPhone 15, 15 Pro, 15 Pro Max; says 'all storage'"


@pytest.mark.parametrize(
    ("title", "query", "evidence"),
    [
        (SWITCH_CATALOGUE_TITLE, "nintendo switch", "names 3 variants: Nintendo Switch OLED, Switch 2, Switch Console"),
        (MOTORBIKE_KEYWORD_TITLE, "r15", "8 variants: 2031 Aerox V2 Yamaha, ADV150, R15, MT15, CB150R and 3 more"),
        (MOTORBIKE_CATALOGUE_TITLE, "r15", "names 13 variants"),
        ("iPhone 15 Pro 128GB / 256GB / 512GB", "iphone 15 pro", "names 3 storage sizes: 128GB, 256GB, 512GB"),
        ("Brand New Sealed iPhone 15 Pro Max 256GB/512GB/1TB", "iphone 15 pro max", "256GB, 512GB, 1TB"),
        ("iPhone 15 or 15 Pro", "iphone 15", "names 2 variants: iPhone 15, 15 Pro"),
        ("Samsung S24 / S24+ / S24 Ultra", "s24", "names 3 variants"),
        ("iPhone 13/14/15 Case", "iphone case", "names 3 variants"),
        ("iPhone 15 from $579", "iphone 15", "says 'from $579'"),
        ("iPhone 15 Pro many sets available", "iphone 15 pro", "says 'many sets'"),
    ],
)
def test_multi_item_titles(title: str, query: str, evidence: str) -> None:
    tag = title_lists_multiple_variants(title, query)

    assert tag is not None
    assert evidence in tag.reason


def test_a_repeated_query_word_makes_a_bare_model_a_variant() -> None:
    assert title_lists_multiple_variants("Nintendo Switch / Switch Lite", "nintendo switch") is not None
    assert title_lists_multiple_variants("Nintendo Switch / Switch Lite", None) is None


@pytest.mark.parametrize("title", fixture_titles() + REAL_SINGLE_ITEM_TITLES)
def test_real_single_item_titles_are_not_tagged(title: str) -> None:
    assert title_lists_multiple_variants(title, "iphone 15 pro") is None


@pytest.mark.parametrize(
    ("title", "query"),
    [
        ("iPhone 15 Pro Max 256GB", "iphone 15 pro"),
        ("iPhone 15 Pro, 256GB, Natural Titanium, 100% battery", "iphone 15 pro"),
        ("iPhone 15 Pro Max 256GB, 6 months warranty", "iphone 15 pro"),
        ("iPhone 15 Pro, 2 units left", "iphone 15 pro"),
        ("iPhone 15 Pro w/ box & charger", "iphone 15 pro"),
        ("iPhone 15 Pro $750, like new", "iphone 15 pro"),
        ("Apple iPhone | 15 Pro | 256GB | Like New", "iphone 15 pro"),
        ("iPhone 15 Pro Max Natural / Blue Titanium", "iphone 15 pro max"),
        ("iPhone 15 Pro Max Blue / iPhone 15 Pro Max Black", "iphone 15 pro max"),
        ("MacBook Pro M3 16GB/512GB", "macbook pro"),
        ("Samsung S24 Ultra 12/256GB", "s24 ultra"),
        ("Mac Studio 128GB RAM 1TB", "mac studio"),
        ("Console Repair Service for Nintendo Switch, 3DS, and more", "nintendo switch"),
    ],
)
def test_one_item_with_attributes_is_not_tagged(title: str, query: str) -> None:
    assert title_lists_multiple_variants(title, query) is None


# --- description rules -------------------------------------------------------------------------------------

PRICE_LIST_DESCRIPTION = (
    "iPhone 15 128GB - $579\n"
    "iPhone 15 Pro 256GB - $850\n"
    "iPhone 15 Pro Max 256GB - $1,050\n"
    "All local sets, 1 year warranty. Call 9123 4567"
)


def test_description_with_three_priced_variants_is_a_price_list() -> None:
    tag = description_has_price_list(PRICE_LIST_DESCRIPTION)

    assert tag.code == TagCode.PRICE_LIST_IN_DESCRIPTION
    assert tag.reason == (
        "description lists 3 prices: S$579, S$850, S$1,050; has 3 priced lines, e.g. 'iPhone 15 128GB - $579'"
    )


def test_two_priced_variant_lines_are_a_price_list() -> None:
    tag = description_has_price_list("15 Pro: 850\n15 Pro Max: 1050")

    assert tag.reason == "description has 2 priced lines, e.g. '15 Pro: 850'"


def test_price_list_needs_three_distinct_offered_prices() -> None:
    assert description_has_price_list("$579, $650") is None
    assert description_has_price_list("$579, $650, $579") is None
    assert description_has_price_list("$579, $650, $700") is not None


@pytest.mark.parametrize(
    "description",
    [
        parse_listing_detail_response(load_json_fixture("listing_detail.json")).description,
        "Selling at $850, bought at $1,299. Retail $1,599",
        "Battery health: 89\nCycle count: 120\nPrice: $850",
        "Price: $850\nRetail: $1,599",
        "Bought in 2023. Contact 9123 4567. $850 firm.",
    ],
)
def test_single_item_descriptions_are_not_price_lists(description: str) -> None:
    assert description_has_price_list(description) is None


def test_listed_price_equal_to_the_lowest_description_price_is_a_floor() -> None:
    tag = listed_price_is_floor(579, PRICE_LIST_DESCRIPTION)

    assert tag.code == TagCode.LISTED_PRICE_IS_FLOOR
    assert tag.reason == "listed S$579 is the lowest of 3 prices in the description (up to S$1,050)"


def test_a_from_price_is_a_floor() -> None:
    assert listed_price_is_floor(579, "Prices from $579 depending on storage").reason == (
        "description says 'from $579'"
    )


def test_floor_tolerance_boundary() -> None:
    assert listed_price_is_floor(579.5, "$579 or $650") is not None
    assert listed_price_is_floor(580, "$579 or $650") is None


@pytest.mark.parametrize(
    ("listed_price", "description"),
    [
        (850, "$850 firm"),
        (850, "$750 or $850 with case"),
        (850, "Selling at $850, bought at $1,299"),
        (None, "$579, $650"),
    ],
)
def test_prices_that_are_not_floors(listed_price: float | None, description: str) -> None:
    assert listed_price_is_floor(listed_price, description) is None


def test_description_tags_runs_every_description_rule() -> None:
    codes = [tag.code for tag in description_tags(579, PRICE_LIST_DESCRIPTION)]

    assert codes == [TagCode.PRICE_LIST_IN_DESCRIPTION, TagCode.LISTED_PRICE_IS_FLOOR]
    assert description_tags(850, "Selling at $850, no box") == []


# --- price rules ------------------------------------------------------------------------------------------

PEERS = [700, 750, 800, 850, 900]  # median 800, so the threshold is 480


def test_price_below_market_threshold_boundary() -> None:
    threshold = BELOW_MARKET_FRACTION * 800

    tag = price_below_market(threshold - 1, PEERS)

    assert tag.code == TagCode.PRICE_BELOW_MARKET
    assert tag.severity == Severity.INFO
    assert tag.reason == "listed S$479 is 59% of the median S$800 of 5 other results"
    assert price_below_market(threshold, PEERS) is None


def test_price_below_market_needs_enough_peers() -> None:
    assert len(PEERS) == MIN_PEERS_FOR_MARKET
    assert price_below_market(9, PEERS[:-1]) is None
    assert price_below_market(9, [*PEERS[:-1], None]) is None
    assert price_below_market(9, PEERS) is not None
    assert price_below_market(None, PEERS) is None


def test_price_not_for_your_item_only_when_not_stated() -> None:
    not_stated = ResolvedPrice(amount=None, source=PriceSource.NOT_STATED, note="only 128GB is priced")

    tag = price_not_for_your_item(not_stated, "iphone 15 pro 256gb")

    assert tag.code == TagCode.PRICE_NOT_FOR_YOUR_ITEM
    assert tag.reason == "the description states no price for 'iphone 15 pro 256gb' (only 128GB is priced)"
    from_description = ResolvedPrice(amount=850, source=PriceSource.DESCRIPTION)
    assert price_not_for_your_item(from_description, "iphone 15 pro 256gb") is None
