"""What Ask mode's templates show: form state, progress, kept cards, dropped rows, trace rows, notes. Pure."""

from datetime import UTC, datetime

import pytest
from pydantic_ai import models

from carousearch.agent.progress import BudgetUsed, ToolCallEvent
from carousearch.agent.result import AgentAnswer, DroppedListing, KeptListing, ToolCall
from carousearch.agent.settings import AgentBudget, AgentSettings
from carousearch.web.prompt_runs import PromptRun, TraceLine
from carousearch.web.prompt_view_models import (
    EMPTY_PROMPT_MESSAGE,
    MAX_PROMPT_LENGTH,
    UNSEEN_LISTING_WARNING,
    answer_trace_rows,
    answer_view,
    ask_form_view,
    budget_used_line,
    dropped_row,
    failed_view,
    format_duration,
    kept_card,
    notes_lines,
    progress_trace_rows,
    progress_view,
    prompt_problem,
    trace_row,
    waiting_view,
)
from carousearch.web.view_models import ErrorMessage, meta_line

from fake_mcp_services import SEARCH_RESULT

models.ALLOW_MODEL_REQUESTS = False

START = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
FIRST, SECOND = SEARCH_RESULT.listings[0], SEARCH_RESULT.listings[1]
BUDGET = AgentBudget()


def budget_used(calls: int, photos: int, listings: int) -> BudgetUsed:
    return BudgetUsed(calls, 40, photos, 15, listings, 200)


def kept(listing_id: str = FIRST.id, **changes) -> KeptListing:
    fields = {
        "listing_id": listing_id, "title": "the model's title", "price": 579.0,
        "url": f"https://www.carousell.sg/p/{listing_id}/", "seller": "someone", "reason": "one phone, in budget",
    }
    return KeptListing(**(fields | changes))


def run_with_search() -> PromptRun:
    run = PromptRun(run_id="abc123", prompt="Find a used iPhone 15", started_at=START)
    run.record(ToolCallEvent("started", "search_listings", 'query="iphone 15"', None, 0.0, budget_used(1, 0, 20)))
    run.record(
        ToolCallEvent(
            "finished", "search_listings", 'query="iphone 15"', "ok", 1.24, budget_used(1, 0, 20),
            tuple(SEARCH_RESULT.listings),
        )
    )
    return run


# --- the form -----------------------------------------------------------------------------------------------------


def test_the_form_names_the_model_and_where_it_comes_from() -> None:
    form = ask_form_view(AgentSettings(anthropic_api_key="sk-ant-secret"))
    assert form.model_line == "Model: anthropic:claude-haiku-5-5, from AGENT_MODEL"
    assert form.budget_line == "Up to 40 tool calls, 15 photos and 200 listings per run"
    assert (form.missing_key, form.run_disabled, form.max_prompt_length) == (None, False, MAX_PROMPT_LENGTH)
    assert "sk-ant-secret" not in repr(form)


@pytest.mark.parametrize(
    ("model", "variable"), [("anthropic:claude-haiku-5-5", "ANTHROPIC_API_KEY"), ("openai:gpt-5", "OPENAI_API_KEY")]
)
def test_without_the_provider_key_the_form_names_the_variable_and_disables_run(model: str, variable: str) -> None:
    form = ask_form_view(AgentSettings(model=model))
    assert form.run_disabled
    assert form.missing_key == ErrorMessage(
        f"{variable} is not set; the model {model} needs it. Add it to .env and restart", "carousearch web"
    )


def test_a_provider_without_a_known_key_variable_is_not_disabled() -> None:
    assert not ask_form_view(AgentSettings(model="google-gla:gemini-3-flash")).run_disabled


def test_prompt_problems() -> None:
    assert prompt_problem("Find a used iPhone 15") is None
    assert prompt_problem("   \n ") == EMPTY_PROMPT_MESSAGE
    assert prompt_problem("x" * MAX_PROMPT_LENGTH) is None
    too_long = prompt_problem("x" * (MAX_PROMPT_LENGTH + 1))
    assert too_long is not None and "2,000 characters; this one has 2,001" in too_long.text


# --- progress -----------------------------------------------------------------------------------------------------


def test_the_budget_line_reads_the_latest_counters_or_zero_before_any_call() -> None:
    assert budget_used_line(budget_used(3, 1, 48), BUDGET) == "calls 3 of 40 · photos 1 of 15 · listings 48 of 200"
    assert budget_used_line(None, AgentBudget(max_photos=0)) == "calls 0 of 40 · photos 0 of 0 · listings 0 of 200"


def test_progress_shows_the_status_the_budget_and_the_calls_so_far() -> None:
    run = run_with_search()
    run.record(ToolCallEvent("started", "listing_details", "listing_id=1", None, 0.0, budget_used(2, 0, 20)))

    view = progress_view(run, BUDGET, elapsed_seconds=12.4)

    assert (view.run_id, view.poll_url, view.prompt) == ("abc123", "/ask/abc123", "Find a used iPhone 15")
    assert view.status_line == "Running · 12 s · 2 tool calls"
    assert view.budget_line == "calls 2 of 40 · photos 0 of 15 · listings 20 of 200"
    assert [(row.tool_name, row.status, row.duration) for row in view.trace] == [
        ("search_listings", "ok", "1.2 s"),
        ("listing_details", "running", ""),
    ]


def test_progress_before_the_first_call_says_starting() -> None:
    run = PromptRun(run_id="abc", prompt="p", started_at=START)
    view = progress_view(run, BUDGET, elapsed_seconds=0.3)
    assert view.status_line == "Running · 0.3 s · starting"
    assert view.trace == []


# --- trace rows ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "duration_seconds", "status_class", "duration"),
    [
        ("ok", 1.234, "ok", "1.2 s"),
        ("failed", 0.04, "failed", "0.0 s"),
        ("refused", 0.0, "refused", ""),
        ("not run", 0.0, "not-run", ""),
        ("running", 0.0, "running", ""),
    ],
)
def test_trace_rows_carry_the_status_and_a_duration_only_for_calls_that_ran(
    status: str, duration_seconds: float, status_class: str, duration: str
) -> None:
    row = trace_row("listing_photo", "url=a.jpg", status, duration_seconds)
    assert (row.status, row.status_class, row.duration) == (status, status_class, duration)


def test_trace_rows_from_the_answer_and_from_progress_lines() -> None:
    answer_rows = answer_trace_rows([ToolCall(tool_name="usage_notes", arguments="", duration_seconds=0.01, status="ok")])
    progress_rows = progress_trace_rows([TraceLine("search_listings", "q", "running", 0.0)])
    assert [(row.tool_name, row.duration) for row in answer_rows] == [("usage_notes", "0.0 s")]
    assert [(row.tool_name, row.status) for row in progress_rows] == [("search_listings", "running")]


@pytest.mark.parametrize(
    ("seconds", "expected"), [(0.0, "0.0 s"), (1.25, "1.2 s"), (9.94, "9.9 s"), (14.4, "14 s"), (125, "2 min 5 s")]
)
def test_durations(seconds: float, expected: str) -> None:
    assert format_duration(seconds) == expected


# --- the answer ---------------------------------------------------------------------------------------------------


def test_a_kept_card_of_a_listing_a_search_returned_shows_carousells_data_and_the_reason() -> None:
    card = kept_card(kept(), FIRST)
    assert card.title == FIRST.title  # Carousell's title, not the model's
    assert (card.url, card.thumbnail_url, card.price) == (FIRST.listing_url, FIRST.thumbnail_url, FIRST.price_display)
    assert card.meta == meta_line(FIRST)
    assert card.meta.startswith("Like new") and card.meta.endswith("urameshi1984")
    assert card.reason == "one phone, in budget"
    assert card.warning is None and card.details_available


def test_a_kept_card_of_an_unseen_listing_uses_the_models_data_with_a_warning() -> None:
    card = kept_card(kept("999", price=1299.5, seller="bob"), None)
    assert (card.title, card.url, card.price, card.meta) == (
        "the model's title", "https://www.carousell.sg/p/999/", "S$1,299.50", "bob"
    )
    assert card.thumbnail_url is None
    assert card.warning == UNSEEN_LISTING_WARNING
    assert card.details_available


def test_an_unseen_card_drops_a_link_that_is_not_http_and_an_id_that_is_not_numeric() -> None:
    card = kept_card(kept("abc", url="javascript:alert(1)", price=None, seller=None), None)
    assert (card.url, card.price, card.meta, card.details_available) == (None, None, "", False)


def test_dropped_rows_link_to_the_listing_with_carousells_price_when_known() -> None:
    seen = dropped_row(DroppedListing(listing_id=SECOND.id, title="Pro", reason="a Pro"), SECOND)
    unseen = dropped_row(DroppedListing(listing_id="123", title="Other", reason="over budget"), None)
    odd = dropped_row(DroppedListing(listing_id="n/a", title="?", reason="?"), None)

    assert (seen.url, seen.price, seen.title, seen.reason) == (SECOND.listing_url, SECOND.price_display, "Pro", "a Pro")
    assert (unseen.url, unseen.price) == ("https://www.carousell.sg/p/123/", None)
    assert odd.url is None


def test_notes_become_lines_without_blanks() -> None:
    assert notes_lines("One page only.\n\n  Photo budget reached: 15 photos judged.  \n") == [
        "One page only.",
        "Photo budget reached: 15 photos judged.",
    ]
    assert notes_lines("") == []


def test_the_answer_view_puts_it_all_together() -> None:
    run = run_with_search()
    answer = AgentAnswer(
        kept=[kept(), kept("999")],
        dropped=[DroppedListing(listing_id=SECOND.id, title=SECOND.title, reason="a Pro")],
        notes="Checked one page.\nPhoto budget reached: 0 photos judged, 1 more refused.",
        tool_trace=[
            ToolCall(tool_name="search_listings", arguments='query="iphone 15"', duration_seconds=1.24, status="ok"),
            ToolCall(tool_name="listing_photo", arguments="url=a.jpg", duration_seconds=0.0, status="refused"),
        ],
    )

    view = answer_view(run, answer, BUDGET, elapsed_seconds=14.2)

    assert view.summary == "2 kept · 1 dropped · 2 tool calls · 14 s"
    assert view.budget_line == "calls 1 of 40 · photos 0 of 15 · listings 20 of 200"
    assert view.notes == ["Checked one page.", "Photo budget reached: 0 photos judged, 1 more refused."]
    assert [(card.listing_id, card.warning is None) for card in view.kept] == [(FIRST.id, True), ("999", False)]
    assert [(row.listing_id, row.price) for row in view.dropped] == [(SECOND.id, SECOND.price_display)]
    assert [(row.tool_name, row.status, row.duration) for row in view.trace] == [
        ("search_listings", "ok", "1.2 s"),
        ("listing_photo", "refused", ""),
    ]


def test_a_failed_view_keeps_the_calls_made_before_the_failure() -> None:
    error = ErrorMessage("The agent run failed.")
    view = failed_view(run_with_search(), error, BUDGET)
    assert (view.prompt, view.error) == ("Find a used iPhone 15", error)
    assert [row.status for row in view.trace] == ["ok"]
    assert view.budget_line.startswith("calls 1 of 40")


def test_the_waiting_view_asks_for_the_same_url_and_says_what_it_waits_for() -> None:
    search = waiting_view("/results?q=bike")
    detail = waiting_view("/listing/123", for_detail=True)
    assert (search.url, search.text) == ("/results?q=bike", "An Ask run is in progress; your search will run when it finishes.")
    assert detail.url == "/listing/123" and "details will load" in detail.text
    assert (search.element_id, detail.element_id) == ("ask-waiting-results", None)
