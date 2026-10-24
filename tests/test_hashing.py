from carousearch.carousell.parse import parse_search_response
from carousearch.filters.schema import Condition, SearchRequest, SortOrder
from carousearch.market.hashing import listing_content_hash, search_request_hash

from fixture_files import load_json_fixture


def sample_listing():
    return parse_search_response(load_json_fixture("search_price_condition.json")).listings[0]


def test_content_hash_is_sixteen_hex_characters() -> None:
    content_hash = listing_content_hash(sample_listing())

    assert len(content_hash) == 16
    assert int(content_hash, 16) >= 0


def test_content_hash_ignores_likes_thumbnail_and_timestamps() -> None:
    listing = sample_listing()
    varied = listing.model_copy(
        update={
            "like_count": listing.like_count + 40,
            "thumbnail_url": "https://media.karousell.com/media/photos/products/other.jpg",
            "posted_or_bumped_at": None,
            "seller_username": "someone_else",
        }
    )

    assert listing_content_hash(varied) == listing_content_hash(listing)


def test_content_hash_changes_on_price_title_and_condition() -> None:
    listing = sample_listing()
    original = listing_content_hash(listing)

    assert listing_content_hash(listing.model_copy(update={"price": 1.0})) != original
    assert listing_content_hash(listing.model_copy(update={"price": None})) != original
    assert listing_content_hash(listing.model_copy(update={"title": listing.title + " (faulty)"})) != original
    other_condition = Condition.HEAVILY_USED if listing.condition != Condition.HEAVILY_USED else Condition.BRAND_NEW
    assert listing_content_hash(listing.model_copy(update={"condition": other_condition})) != original
    assert listing_content_hash(listing.model_copy(update={"condition": None})) != original


def test_content_hash_treats_integer_and_float_prices_alike() -> None:
    listing = sample_listing()

    as_int = listing.model_copy(update={"price": 750})
    as_float = listing.model_copy(update={"price": 750.0})

    assert listing_content_hash(as_int) == listing_content_hash(as_float)


def test_request_hash_ignores_page_cursor_and_page_size() -> None:
    request = SearchRequest(query="iphone 15 pro", price_max=1500, conditions=[Condition.LIKE_NEW])

    assert search_request_hash(request.model_copy(update={"page_cursor": "abc"})) == search_request_hash(request)
    assert search_request_hash(request.model_copy(update={"page_size": 48})) == search_request_hash(request)


def test_request_hash_changes_with_filters() -> None:
    request = SearchRequest(query="iphone 15 pro", price_max=1500)
    original = search_request_hash(request)

    assert len(original) == 16
    assert search_request_hash(request.model_copy(update={"query": "iphone 15"})) != original
    assert search_request_hash(request.model_copy(update={"price_max": 1400.0})) != original
    assert search_request_hash(request.model_copy(update={"sort": SortOrder.RECENT})) != original
    assert search_request_hash(request.model_copy(update={"conditions": [Condition.LIKE_NEW]})) != original
