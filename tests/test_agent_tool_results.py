"""What the planner is sent of a tool result (pure)."""

from carousearch.agent.tool_results import (
    DESCRIPTION_CUT_NOTE,
    DESCRIPTION_PREVIEW_CHARACTERS,
    arguments_for_server,
    result_for_model,
)

SEARCH = {
    "listings": [{"id": "1", "title": "R15", "price": 5000.0, "seller_username": "a", "thumbnail_url": "u"}],
    "total": 1,
    "next_page": None,
}


def test_a_searchs_fields_are_applied_by_the_gate_not_the_server() -> None:
    arguments = {"request": {"query": "r15"}, "limit": 48, "fields": ["title", "price"]}

    assert arguments_for_server("search_listings", arguments) == {"request": {"query": "r15"}, "limit": 48}
    assert result_for_model("search_listings", arguments, SEARCH) == {
        "listings": [{"id": "1", "title": "R15", "price": 5000.0}], "total": 1, "next_page": None
    }


def test_a_search_without_fields_goes_through_whole() -> None:
    assert result_for_model("search_listings", {"request": {"query": "r15"}}, SEARCH) == SEARCH


def test_a_long_description_is_cut_with_a_pointer_to_listings_db() -> None:
    detail = {"id": "1", "description": "d" * 1000, "image_urls": ["x"]}

    shown = result_for_model("listing_details", {"listing_id": "1"}, detail)

    assert shown["description"] == "d" * DESCRIPTION_PREVIEW_CHARACTERS + DESCRIPTION_CUT_NOTE
    assert shown["image_urls"] == ["x"]
    assert detail["description"] == "d" * 1000  # the full result is untouched


def test_a_short_description_and_other_tools_are_unchanged() -> None:
    detail = {"id": "1", "description": "short"}

    assert result_for_model("listing_details", {}, detail) == detail
    assert result_for_model("run_python", {"code": "print(1)"}, "1") == "1"
    assert arguments_for_server("listing_details", {"listing_id": "1"}) == {"listing_id": "1"}
