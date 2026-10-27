"""What Ask mode's templates show: form state, progress, kept cards, dropped rows, trace rows, notes. Pure."""

from datetime import UTC, datetime

import pytest
from pydantic_ai import models

from carousearch.agent.cost import ModelUsage
from carousearch.agent.planning import PlanningOutcome
from carousearch.agent.progress import BudgetUsed, ToolCallEvent
from carousearch.agent.result import AgentAnswer, DroppedListing, KeptListing, ToolCall
from carousearch.agent.settings import AgentBudget, AgentSettings
from carousearch.agent.templates import METHOD_TEMPLATES
from carousearch.language_models import ProviderKeys
from carousearch.web.prompt_runs import PromptRun, TraceLine
from carousearch.web.prompt_view_models import (
    EMPTY_CHANGE_MESSAGE,
    EMPTY_PROMPT_MESSAGE,
    MAX_PROMPT_LENGTH,
    UNSEEN_LISTING_WARNING,
    SuggestedNoteView,
    answer_trace_rows,
    answer_view,
    ask_form_view,
    budget_used_line,
    cancelled_view,
    change_problem,
    dropped_row,
    failed_view,
    format_duration,
    kept_card,
    notes_lines,
    plan_view,
    progress_trace_rows,
    progress_view,
    prompt_problem,
    template_choices,
    trace_row,
    waiting_view,
)
from carousearch.web.view_models import ErrorMessage, meta_line

from fake_mcp_services import SEARCH_RESULT
from plan_examples import EXAMPLE_PLAN, example_plan

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


def test_the_form_names_the_planner_and_the_worker() -> None:
    form = ask_form_view(AgentSettings(keys=ProviderKeys(openai="sk-ant-secret")))
    assert form.model_line == "Models: openai:gpt-5.4-mini plans, openai:gpt-5.4-nano judges"
    assert form.budget_line == "Up to 40 tool calls, 15 photos and 200 listings per run"
    assert (form.missing_key, form.run_disabled, form.max_prompt_length) == (None, False, MAX_PROMPT_LENGTH)
    assert "sk-ant-secret" not in repr(form)


@pytest.mark.parametrize(
    ("model", "variable"), [("anthropic:claude-haiku-5-5", "ANTHROPIC_API_KEY"), ("openai:gpt-5", "OPENAI_API_KEY")]
)
def test_without_the_provider_key_the_form_names_the_variable_and_disables_run(model: str, variable: str) -> None:
    form = ask_form_view(AgentSettings(planner_model=model, worker_model=model))
    assert form.run_disabled
    assert form.missing_key == ErrorMessage(
        f"{variable} is not set; the model {model} needs it. Add it to .env and restart", "carousearch web"
    )


def test_a_provider_without_a_known_key_variable_is_not_disabled() -> None:
    settings = AgentSettings(planner_model="google-gla:gemini-3-flash", worker_model="google-gla:gemini-3-flash")
    assert not ask_form_view(settings).run_disabled


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
        not_shortlisted=17,
        tool_trace=[
            ToolCall(tool_name="search_listings", arguments='query="iphone 15"', duration_seconds=1.24, status="ok"),
            ToolCall(tool_name="listing_photo", arguments="url=a.jpg", duration_seconds=0.0, status="refused"),
        ],
        usage=[ModelUsage(role="planner", model="openai:gpt-5.4-mini", requests=2, input_tokens=5000,
                          output_tokens=300, estimated_cost_usd=0.0051)],
    )

    view = answer_view(run, answer, BUDGET, elapsed_seconds=14.2)

    assert view.summary == "2 kept · 1 dropped · 17 not shortlisted · 2 tool calls · 14 s"
    assert view.budget_line == "calls 1 of 40 · photos 0 of 15 · listings 20 of 200"
    assert view.notes[:2] == ["Checked one page.", "Photo budget reached: 0 photos judged, 1 more refused."]
    assert view.notes[2].startswith("Cost (estimate, list prices of 2026-10-27): about US$0.0051. planner")
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


# --- the plan step ------------------------------------------------------------------------------------------------


def planned_run(revisions: int = 0) -> PromptRun:
    run = PromptRun(run_id="abc123", prompt="Find a table", started_at=START, status="planned", revisions=revisions)
    trace = [ToolCall(tool_name="sample_descriptions", arguments="query=table", duration_seconds=1.2, status="ok")]
    usage = [ModelUsage(role="planner", model="openai:gpt-5.4-mini", requests=2, estimated_cost_usd=0.0041)]
    run.planning = PlanningOutcome(plan=EXAMPLE_PLAN, tool_trace=trace, usage=usage, knowledge_files=["tables.md"])
    return run


def test_the_plan_view_has_every_section_and_the_buttons() -> None:
    view = plan_view(planned_run(), BUDGET, planning_seconds=12.4)

    assert (view.goal, view.item_class) == (EXAMPLE_PLAN.goal, EXAMPLE_PLAN.item_class)
    assert view.value_drivers == ["size", "condition", "age"]
    assert [(row.name, row.source, row.source_class, row.unit) for row in view.attributes] == [
        ("price", "card field", "card-field", None),
        ("size_cm", "text extraction", "text-extraction", "cm"),
        ("days_listed", "derived", "derived", "days"),
    ]
    assert view.benchmark == [
        ("Population", "single-item sale listings with a plain price"), ("Grouped by", "size_cm"),
        ("Statistic", "median"), ("Normalisation", "none"),
    ]
    assert view.exclusions[0].reason == "their price is not the price of one item"
    assert view.template_line == "under_benchmark: Listings priced well below what comparable listings ask."
    assert view.estimate_line == (
        "2 search pages, 10 details, 0 photos, 1 worker call (run budget: 40 tool calls, 15 photos)"
    )
    assert view.planning_line == "Planned in 12 s · 1 tool call · about US$0.0041 so far"
    assert view.knowledge_files == ["tables.md"] and view.planning_trace[0].tool_name == "sample_descriptions"
    assert (view.execute_url, view.revise_url, view.cancel_url) == (
        "/ask/abc123/execute", "/ask/abc123/revise", "/ask/abc123/cancel"
    )
    assert (view.can_revise, view.revised) == (True, False)


def test_a_revised_plan_cannot_be_revised_again_and_a_plan_without_benchmark_says_so() -> None:
    run = planned_run(revisions=1)
    run.planning = PlanningOutcome(plan=example_plan(benchmark=None, template=None))

    view = plan_view(run, BUDGET, planning_seconds=3)

    assert (view.can_revise, view.revised, view.benchmark, view.template_line) == (False, True, [], "none")


def test_progress_while_planning_counts_against_the_planning_budget() -> None:
    run = PromptRun(run_id="abc123", prompt="Find a table", started_at=START, status="planning")
    run.record(ToolCallEvent("started", "sample_descriptions", "query=table", None, 0.0, budget_used(1, 0, 20)))

    view = progress_view(run, BUDGET, 4.0, planning_budget=AgentBudget(max_tool_calls=6, max_photos=0))

    assert view.status_line == "Planning · 4.0 s · 1 tool call"
    assert view.trace[0].tool_name == "sample_descriptions"


def test_the_answer_shows_the_plan_line_the_review_and_the_suggested_note() -> None:
    run = planned_run()
    answer = AgentAnswer(
        kept=[], dropped=[], notes="", tool_trace=[], plan=EXAMPLE_PLAN, suggested_knowledge="Sizes are in titles.",
        plan_review=[{"item": "exclusion:1", "status": "not_done", "note": "no multi-item posts seen"}],
    )
    run.saved_note = "tables.md"

    view = answer_view(run, answer, BUDGET, 30.0)

    assert view.plan_line == "Plan: a second-hand household item sold in several sizes · under_benchmark"
    (row,) = view.review
    assert (row.label, row.status, row.status_class, row.note) == (
        "Exclusion 1: posts offering several items at one price", "not done", "not-done", "no multi-item posts seen"
    )
    expected_note = SuggestedNoteView("Sizes are in titles.", "/ask/abc123/knowledge", "knowledge/tables.md")
    assert view.suggested_note == expected_note


def test_the_form_offers_one_choice_per_method_template() -> None:
    choices = template_choices()

    assert [choice.name for choice in choices] == [template.name for template in METHOD_TEMPLATES]
    assert choices[0].label == "under benchmark" and "<item>" in choices[0].example_request
    assert ask_form_view(AgentSettings()).templates == choices


def test_a_plan_change_must_be_said_in_words_and_kept_short() -> None:
    assert change_problem("   ") == EMPTY_CHANGE_MESSAGE
    assert "1,000 characters" in change_problem("x" * 1001).text
    assert change_problem("compare within each size") is None


def test_a_cancelled_run_says_nothing_else_was_searched() -> None:
    assert cancelled_view(planned_run()).text == "Plan cancelled. Nothing was searched beyond planning (1 tool call)."
