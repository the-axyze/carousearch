import pytest
from pydantic import ValidationError

from carousearch.filters.schema import Condition, SearchRequest


def test_query_is_stripped_and_blank_query_becomes_none() -> None:
    assert SearchRequest(query="  iphone  ").query == "iphone"
    assert SearchRequest(query="   ", category="5707").query is None


def test_search_needs_query_or_category() -> None:
    with pytest.raises(ValidationError, match="query, a category, or both"):
        SearchRequest()


def test_category_must_be_numeric_collection_id() -> None:
    with pytest.raises(ValidationError, match="numeric Carousell collection id"):
        SearchRequest(category="mobile-phones")


def test_price_range_must_be_ordered_and_not_negative() -> None:
    with pytest.raises(ValidationError, match="price_min must not be greater"):
        SearchRequest(query="sofa", price_min=500, price_max=100)
    with pytest.raises(ValidationError):
        SearchRequest(query="sofa", price_max=-1)


def test_page_size_is_limited() -> None:
    with pytest.raises(ValidationError):
        SearchRequest(query="sofa", page_size=0)
    with pytest.raises(ValidationError):
        SearchRequest(query="sofa", page_size=49)


def test_category_fields_cannot_override_core_filters() -> None:
    with pytest.raises(ValidationError, match="set through its own SearchRequest field"):
        SearchRequest(query="sofa", category_fields={"layered_condition": "3"})
    with pytest.raises(ValidationError, match="empty value list"):
        SearchRequest(query="sofa", category_fields={"chairs_type": []})


def test_unknown_fields_are_rejected() -> None:
    """Strictness matters once an LLM fills this model: a typo must not be silently ignored."""
    with pytest.raises(ValidationError):
        SearchRequest(query="sofa", posted_within_days=3)


def test_json_round_trip() -> None:
    request = SearchRequest(query="sofa", conditions=[Condition.LIKE_NEW], category_fields={"furniture_size": "A"})

    assert SearchRequest.model_validate_json(request.model_dump_json()) == request
