"""ToolBudget (pure): what it admits, cuts and refuses, and the notes it leaves."""

import pytest

from carousearch.agent.budget import BudgetExhausted, ToolBudget, count_of
from carousearch.agent.settings import AgentBudget

SEARCH = {"request": {"query": "nintendo switch"}, "limit": 48}
PHOTO = {"url": "https://media.karousell.com/media/photos/products/a.jpg"}


def test_calls_within_the_budget_pass_through_unchanged_and_are_counted() -> None:
    budget = ToolBudget(AgentBudget(max_tool_calls=3, max_photos=2, max_listings=100))

    assert budget.admit("search_listings", SEARCH) == SEARCH
    assert budget.admit("listing_photo", PHOTO) == PHOTO
    assert budget.admit("listing_details", {"listing_id": "1"}) == {"listing_id": "1"}

    assert (budget.tool_calls, budget.photos, budget.listings) == (3, 1, 48)
    assert budget.notes() == []


def test_the_call_after_the_tool_call_cap_is_refused_and_not_counted() -> None:
    budget = ToolBudget(AgentBudget(max_tool_calls=1))
    budget.admit("usage_notes", {})

    with pytest.raises(BudgetExhausted, match="tool-call budget of 1 calls is used up"):
        budget.admit("listing_details", {"listing_id": "1"})

    assert budget.tool_calls == 1
    assert budget.notes() == ["Tool-call budget reached: 1 call used, 1 more refused."]


def test_photos_beyond_the_photo_cap_are_refused_while_other_tools_still_work() -> None:
    budget = ToolBudget(AgentBudget(max_tool_calls=10, max_photos=1))
    budget.admit("listing_photo", PHOTO)

    with pytest.raises(BudgetExhausted, match="photo budget of 1 photos"):
        budget.admit("listing_photo", PHOTO)
    budget.admit("listing_details", {"listing_id": "1"})

    assert (budget.photos, budget.tool_calls) == (1, 2)
    assert budget.notes() == ["Photo budget reached: 1 photo judged, 1 more refused."]


def test_a_zero_photo_budget_refuses_every_photo() -> None:
    budget = ToolBudget(AgentBudget(max_photos=0))

    with pytest.raises(BudgetExhausted):
        budget.admit("listing_photo", PHOTO)


def test_a_search_is_cut_to_the_listings_left_and_the_next_one_is_refused() -> None:
    budget = ToolBudget(AgentBudget(max_listings=60))
    budget.admit("search_listings", SEARCH)

    second = budget.admit("search_listings", {**SEARCH, "page_token": "p2"})
    with pytest.raises(BudgetExhausted, match="listing budget of 60 listings"):
        budget.admit("search_listings", {**SEARCH, "page_token": "p3"})

    assert second == {**SEARCH, "page_token": "p2", "limit": 12}
    assert budget.listings == 60
    assert budget.notes() == [
        "Listing budget reached: 60 listings pulled from search pages (1 page cut short, 1 search refused)."
    ]


def test_a_search_without_a_limit_counts_the_tools_default_page_size() -> None:
    budget = ToolBudget(AgentBudget())

    admitted = budget.admit("search_listings", {"request": {"query": "bike"}})

    assert admitted["limit"] == 20
    assert budget.listings == 20


def test_a_limit_that_is_not_a_whole_number_is_replaced_by_the_default() -> None:
    budget = ToolBudget(AgentBudget())

    assert budget.admit("search_listings", {"request": {"query": "bike"}, "limit": "lots"})["limit"] == 20


def test_count_of_uses_singular_and_plural_forms() -> None:
    assert count_of(1, "call") == "1 call"
    assert count_of(0, "page") == "0 pages"
    assert count_of(2, "search", "searches") == "2 searches"


# --- what local tools fetch inside one call ---------------------------------------------------------------------


def test_detail_fetches_inside_a_tool_are_granted_up_to_the_tool_call_cap() -> None:
    budget = ToolBudget(AgentBudget(max_tool_calls=5))
    budget.admit("judge_listings", {})

    assert budget.grant_detail_fetches(3) == 3
    assert budget.grant_detail_fetches(3) == 1
    assert budget.tool_calls == 5
    assert budget.notes() == ["Tool-call budget reached inside a tool: 2 listings left without details."]


def test_photos_inside_a_tool_count_as_photos_and_tool_calls() -> None:
    budget = ToolBudget(AgentBudget(max_tool_calls=40, max_photos=2))

    assert budget.grant_photos(3) == 2
    assert (budget.photos, budget.tool_calls) == (2, 2)
    assert budget.notes() == ["Photo budget reached: 2 photos judged, 1 more refused."]


def test_nothing_is_granted_once_a_cap_is_reached() -> None:
    budget = ToolBudget(AgentBudget(max_tool_calls=1, max_photos=5))
    budget.admit("judge_photos", {})

    assert (budget.grant_photos(2), budget.grant_detail_fetches(2)) == (0, 0)
    assert (budget.photos, budget.tool_calls) == (0, 1)


def test_a_sample_counts_its_page_against_the_listing_cap_and_its_detail_fetches_as_tool_calls() -> None:
    budget = ToolBudget(AgentBudget(max_tool_calls=4, max_listings=30))

    admitted = budget.admit("sample_descriptions", {"request": {"query": "a"}, "n": 48, "max_detail_fetches": 5})

    assert admitted["n"] == 30 and admitted["max_detail_fetches"] == 3  # 4 calls: the sample itself and 3 details
    assert (budget.tool_calls, budget.listings) == (4, 30)
    assert "2 listings left without details" in " ".join(budget.notes())


def test_a_sample_without_detail_fetches_costs_one_call_and_its_default_page() -> None:
    budget = ToolBudget(AgentBudget())

    admitted = budget.admit("sample_descriptions", {"request": {"query": "a"}})

    assert (admitted["n"], admitted["max_detail_fetches"]) == (20, 0)
    assert (budget.tool_calls, budget.listings) == (1, 20)
