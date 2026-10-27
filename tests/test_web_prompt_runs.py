"""Ask mode's run bookkeeping: events, seen listings, status changes, the trace so far and the newest-10 registry.
Pure: no model, no Carousell, no event loop."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic_ai import models

from carousearch.agent.progress import BudgetUsed, ToolCallEvent
from carousearch.agent.result import AgentAnswer
from carousearch.web.prompt_runs import MAX_KEPT_RUNS, PromptRun, PromptRunRegistry, TraceLine, trace_lines
from carousearch.web.view_models import ErrorMessage

from fake_mcp_services import SEARCH_RESULT

models.ALLOW_MODEL_REQUESTS = False

START = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
BUDGET = BudgetUsed(
    tool_calls_used=1, max_tool_calls=40, photos_used=0, max_photos=15, listings_used=5, max_listings=200
)
SEARCH_ARGUMENTS = 'query="iphone 15" limit=5'


def event(
    phase: str, tool_name: str = "search_listings", arguments: str = SEARCH_ARGUMENTS, status: str | None = None,
    duration_seconds: float = 0.0, listings: tuple = (),
) -> ToolCallEvent:
    return ToolCallEvent(phase, tool_name, arguments, status, duration_seconds, BUDGET, listings)


def new_run() -> PromptRun:
    return PromptRun(run_id="abc", prompt="Find a used iPhone 15", started_at=START)


def test_a_new_run_is_running_with_nothing_recorded() -> None:
    run = new_run()
    assert run.running and run.status == "running"
    assert (run.events, run.seen_listings, run.answer, run.error, run.finished_at) == ([], {}, None, None, None)


def test_record_appends_events_and_keeps_listings_from_finished_searches_by_id() -> None:
    run = new_run()
    first, second, *rest = SEARCH_RESULT.listings
    run.record(event("started"))
    run.record(event("finished", status="ok", listings=(first, second)))
    updated_first = first.model_copy(update={"price_display": "S$500"})
    run.record(event("finished", status="ok", listings=(updated_first, *rest)))

    assert [item.phase for item in run.events] == ["started", "finished", "finished"]
    assert list(run.seen_listings) == [listing.id for listing in SEARCH_RESULT.listings]
    assert run.seen_listings[first.id].price_display == "S$500"  # a later search's copy wins


def test_finish_and_fail_end_the_run() -> None:
    answer = AgentAnswer(kept=[], dropped=[], notes="", tool_trace=[])
    done = new_run()
    done.finish(answer, START + timedelta(seconds=9))
    assert (done.status, done.answer, done.finished_at, done.running) == ("done", answer, START + timedelta(seconds=9), False)

    failed = new_run()
    failed.fail(ErrorMessage("The agent run failed."), START)
    assert (failed.status, failed.error, failed.running) == ("failed", ErrorMessage("The agent run failed."), False)


def test_trace_lines_show_running_calls_until_they_finish() -> None:
    events = [
        event("started"),
        event("finished", status="ok", duration_seconds=1.25),
        event("started", "listing_details", "listing_id=1"),
    ]
    assert trace_lines(events) == [
        TraceLine("search_listings", SEARCH_ARGUMENTS, "ok", 1.25),
        TraceLine("listing_details", "listing_id=1", "running", 0.0),
    ]


def test_trace_lines_match_parallel_calls_and_add_refused_ones_where_they_were_refused() -> None:
    events = [
        event("started", "listing_photo", "url=a.jpg"),
        event("finished", "listing_photo", "url=b.jpg", status="refused"),
        event("started", "listing_details", "listing_id=1"),
        event("finished", "listing_details", "listing_id=1", status="failed", duration_seconds=0.5),
        event("finished", "listing_photo", "url=a.jpg", status="ok", duration_seconds=2.0),
    ]
    assert trace_lines(events) == [
        TraceLine("listing_photo", "url=a.jpg", "ok", 2.0),
        TraceLine("listing_photo", "url=b.jpg", "refused", 0.0),
        TraceLine("listing_details", "listing_id=1", "failed", 0.5),
    ]


def test_the_registry_finds_runs_by_id_and_knows_the_active_one() -> None:
    registry = PromptRunRegistry()
    assert registry.active() is None

    run = registry.start("Find a bike", START)
    assert registry.get(run.run_id) is run
    assert registry.active() is run
    assert registry.get("unknown") is None

    run.finish(AgentAnswer(kept=[], dropped=[], notes="", tool_trace=[]), START)
    assert registry.active() is None


def test_the_registry_refuses_a_second_run_while_one_is_running() -> None:
    registry = PromptRunRegistry()
    registry.start("first", START)
    with pytest.raises(RuntimeError, match="already in progress"):
        registry.start("second", START)


def test_the_registry_keeps_only_the_newest_runs() -> None:
    registry = PromptRunRegistry()
    runs = []
    for index in range(MAX_KEPT_RUNS + 2):
        run = registry.start(f"prompt {index}", START, run_id=f"run-{index}")
        run.fail(ErrorMessage("failed"), START)
        runs.append(run)

    assert registry.get("run-0") is None and registry.get("run-1") is None
    assert all(registry.get(run.run_id) is run for run in runs[2:])


def test_run_ids_are_short_random_tokens() -> None:
    first, second = PromptRunRegistry().start("a", START), PromptRunRegistry().start("b", START)
    assert first.run_id != second.run_id
    assert 8 <= len(first.run_id) <= 16 and first.run_id.replace("-", "").replace("_", "").isalnum()
