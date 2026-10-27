"""The local tools against a fake Carousell, an in-memory judgement cache and a scripted worker model: batching,
caching, budget accounting and the photo tools. Async tool methods are driven with asyncio.run."""

import asyncio
import io
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFilter
from pydantic_ai.exceptions import ModelHTTPError, ToolFailed
from pydantic_ai.models.function import FunctionModel

from carousearch.agent.budget import ToolBudget
from carousearch.agent.cost import UsageLedger
from carousearch.agent.extraction import FieldSpec
from carousearch.agent.judge_cache import MemoryJudgementCache
from carousearch.agent.listings_db import RunListingsStore
from carousearch.agent.local_tools import (
    LocalToolServices,
    RunTools,
    build_local_toolset,
    run_python_description,
)
from carousearch.agent.run_photos import RunPhotoStore, main_photo_url
from carousearch.agent.settings import AgentBudget
from carousearch.agent.worker import WorkerJudge
from carousearch.carousell.errors import CarousellBlockedError
from carousearch.filters.schema import ListingDetail
from carousearch.mcp_server import McpServices, detail_response

from fake_mcp_services import DETAIL, SEARCH_RESULT, FakeCarousellSource, fake_services
from scripted_worker import ScriptedWorker

WORKER_NAME = "openai:gpt-5.4-nano"
COE_CRITERION = "COE has about 2 years left"


def card(listing_id: str, title: str = "Yamaha R15 V3", price: float = 3000.0) -> dict:
    listing = SEARCH_RESULT.listings[0].model_copy(
        update={
            "id": listing_id,
            "title": title,
            "price": price,
            "thumbnail_url": f"https://media.karousell.com/media/photos/products/{listing_id}_thumbnail",
        }
    )
    return listing.model_dump(mode="json", exclude={"raw"})


def detail(
    listing_id: str, description: str = "COE till Nov 2028. Well kept.", title: str = "Yamaha R15 V3", price: float = 3000.0
) -> ListingDetail:
    return DETAIL.model_copy(
        update={
            "id": listing_id,
            "title": title,
            "price": price,
            "description": description,
            "image_urls": [f"https://media.karousell.com/media/photos/products/{listing_id}_full"],
        }
    )


def detail_dict(listing_id: str) -> dict:
    return detail_response(detail(listing_id))


def photo(blurred: bool = False) -> bytes:
    image = Image.new("RGB", (810, 1080), "white")
    draw = ImageDraw.Draw(image)
    for x in range(0, 810, 18):
        draw.line([(x, 0), (x, 1080)], fill="black", width=3)
    if blurred:
        image = image.filter(ImageFilter.GaussianBlur(8))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()


@dataclass
class Harness:
    tools: RunTools
    carousell: FakeCarousellSource
    worker: ScriptedWorker
    services: McpServices
    ledger: UsageLedger
    budget: ToolBudget


def harness(
    tmp_path: Path,
    cards: list[dict],
    carousell: FakeCarousellSource | None = None,
    budget: AgentBudget | None = None,
    worker: ScriptedWorker | None = None,
    cache: MemoryJudgementCache | None = None,
    run_name: str = "run",
) -> Harness:
    carousell = carousell or FakeCarousellSource()
    services = fake_services(carousell)
    worker = worker or ScriptedWorker()
    ledger = UsageLedger()
    run_directory = tmp_path / run_name
    listings = RunListingsStore(run_directory)
    listings.record_search_page(cards, "1595")
    tool_budget = ToolBudget(budget or AgentBudget())
    tools = RunTools(
        services=services,
        local=LocalToolServices(run_directory=run_directory, judgement_cache=cache or MemoryJudgementCache(),
                                network_prefix=()),
        budget=tool_budget,
        listings=listings,
        worker=WorkerJudge(FunctionModel(worker), WORKER_NAME, ledger),
        photos=RunPhotoStore(run_directory),
    )
    return Harness(tools, carousell, worker, services, ledger, tool_budget)


def ids(count: int, start: int = 100) -> list[str]:
    return [str(number) for number in range(start, start + count)]


# --- judge_listings -----------------------------------------------------------------------------------------------


def test_judge_listings_fetches_missing_details_and_batches_the_model_calls(tmp_path: Path) -> None:
    listing_ids = ids(15)
    carousell = FakeCarousellSource(details={listing_id: detail(listing_id) for listing_id in listing_ids})
    run = harness(tmp_path, [card(listing_id) for listing_id in listing_ids], carousell)

    answers = asyncio.run(run.tools.judge_listings(listing_ids, COE_CRITERION))

    assert [answer["id"] for answer in answers] == listing_ids
    assert {answer["verdict"] for answer in answers} == {"yes"}
    assert [len(batch) for batch in run.worker.listing_batches] == [12, 3]
    assert carousell.detail_requests == listing_ids
    assert run.budget.tool_calls == 15  # one per detail request; the gate counts the call itself
    (usage,) = run.ledger.report()
    assert (usage.role, usage.model, usage.requests) == ("worker", WORKER_NAME, 2)
    assert usage.input_tokens > 0


def test_the_model_sees_title_price_and_description(tmp_path: Path) -> None:
    seen_blocks: list[str] = []

    def remember(listing_id: str, block: str) -> tuple[str, str]:
        seen_blocks.append(block)
        return "yes", "COE till Nov 2028"

    carousell = FakeCarousellSource(details={"100": detail("100", "COE till Nov 2028, lowered", "R15 V3 lowered", 2800)})
    run = harness(tmp_path, [card("100", "R15 V3 lowered", 2800)], carousell, worker=ScriptedWorker(remember))

    asyncio.run(run.tools.judge_listings(["100"], COE_CRITERION))

    (block,) = seen_blocks
    assert "Title: R15 V3 lowered" in block and "Price: S$2,800" in block and "COE till Nov 2028, lowered" in block


def test_asking_again_makes_no_model_call_and_no_request(tmp_path: Path) -> None:
    carousell = FakeCarousellSource(details={"100": detail("100"), "101": detail("101")})
    run = harness(tmp_path, [card("100"), card("101")], carousell)
    first = asyncio.run(run.tools.judge_listings(["100", "101"], COE_CRITERION))

    again = asyncio.run(run.tools.judge_listings(["101", "100"], "  coe HAS about 2 years   left "))

    assert len(run.worker.listing_batches) == 1
    assert {answer["id"]: answer for answer in again} == {answer["id"]: answer for answer in first}
    assert carousell.detail_requests == ["100", "101"]


def test_the_cache_is_shared_across_runs_for_the_same_worker_model(tmp_path: Path) -> None:
    cache = MemoryJudgementCache()
    details = {"100": detail("100")}
    first_run = harness(tmp_path, [card("100")], FakeCarousellSource(details=details), cache=cache, run_name="a")
    asyncio.run(first_run.tools.judge_listings(["100"], COE_CRITERION))

    second_run = harness(tmp_path, [card("100")], FakeCarousellSource(details=details), cache=cache, run_name="b")
    asyncio.run(second_run.tools.judge_listings(["100"], COE_CRITERION))

    assert second_run.worker.listing_batches == []
    assert second_run.carousell.detail_requests == []


def test_details_from_the_detail_cache_cost_no_request(tmp_path: Path) -> None:
    run = harness(tmp_path, [card("100")])
    run.services.detail_cache.save_detail(detail("100"), run.services.clock())

    asyncio.run(run.tools.judge_listings(["100"], COE_CRITERION))

    assert run.carousell.detail_requests == []
    assert run.budget.tool_calls == 0


def test_past_the_tool_call_budget_listings_are_judged_on_title_alone_and_not_cached(tmp_path: Path) -> None:
    listing_ids = ids(3)
    carousell = FakeCarousellSource(details={listing_id: detail(listing_id) for listing_id in listing_ids})
    run = harness(tmp_path, [card(listing_id) for listing_id in listing_ids], carousell,
                  budget=AgentBudget(max_tool_calls=1))

    first = asyncio.run(run.tools.judge_listings(listing_ids, COE_CRITERION))
    asyncio.run(run.tools.judge_listings(listing_ids, COE_CRITERION))

    assert first[1]["reason"] == "listing 101 fits (no description: the tool-call budget is used up)"
    assert carousell.detail_requests == ["100"]
    assert run.worker.listing_batches == [listing_ids, ["101", "102"]]  # only the one with a description was cached
    assert "Tool-call budget reached inside a tool: 4 listings left without details." in run.budget.notes()


def test_a_listing_the_model_leaves_out_is_unsure(tmp_path: Path) -> None:
    carousell = FakeCarousellSource(details={"100": detail("100"), "101": detail("101")})
    run = harness(tmp_path, [card("100"), card("101")], carousell, worker=ScriptedWorker(skip=frozenset({"101"})))

    answers = asyncio.run(run.tools.judge_listings(["100", "101"], COE_CRITERION))

    assert answers[1] == {"id": "101", "verdict": "unsure", "reason": "the model gave no verdict for this listing"}


def test_carousell_blocking_a_detail_request_fails_the_tool_call(tmp_path: Path) -> None:
    class BlockedDetails(FakeCarousellSource):
        def get_listing(self, listing_id: str) -> ListingDetail:
            raise CarousellBlockedError("HTTP 429")

    run = harness(tmp_path, [card("100")], BlockedDetails())

    with pytest.raises(ToolFailed, match="blocking or rate-limiting"):
        asyncio.run(run.tools.judge_listings(["100"], COE_CRITERION))
    assert run.worker.listing_batches == []


def test_at_most_thirty_listings_are_judged_per_call(tmp_path: Path) -> None:
    listing_ids = ids(35)
    carousell = FakeCarousellSource(details={listing_id: detail(listing_id) for listing_id in listing_ids})
    run = harness(tmp_path, [card(listing_id) for listing_id in listing_ids], carousell)

    answers = asyncio.run(run.tools.judge_listings(listing_ids, COE_CRITERION))

    assert len(answers) == 30


# --- judge_photos and photo_metrics --------------------------------------------------------------------------------


def test_judge_photos_scores_each_main_photo_with_its_metrics(tmp_path: Path) -> None:
    carousell = FakeCarousellSource(photos={
        "https://media.karousell.com/media/photos/products/100_thumbnail": photo(),
        "https://media.karousell.com/media/photos/products/101_thumbnail": photo(blurred=True),
    })
    worker = ScriptedWorker(photo_answer=lambda listing_id: (5, "very blurry") if listing_id == "101" else (1, "sharp"))
    run = harness(tmp_path, [card("100"), card("101")], carousell, worker=worker)

    answers = asyncio.run(run.tools.judge_photos(["100", "101"], "blurry or low-effort photo"))

    assert [(answer["id"], answer["score_1_to_5"], answer["photo"]) for answer in answers] == [
        ("100", 1, "thumbnail"), ("101", 5, "thumbnail")
    ]
    assert answers[0]["sharpness"] > answers[1]["sharpness"]
    assert (answers[0]["width"], answers[0]["height"]) == (810, 1080)
    assert run.worker.photo_batches == [["100", "101"]]
    assert (run.budget.photos, run.budget.tool_calls) == (2, 2)


def test_judge_photos_uses_the_full_photo_once_details_are_known(tmp_path: Path) -> None:
    run = harness(tmp_path, [card("100")], FakeCarousellSource(photo=photo()))
    run.tools.listings.record_detail(detail_dict("100"))

    answers = asyncio.run(run.tools.judge_photos(["100"], "blurry photo"))

    assert answers[0]["photo"] == "full"
    assert run.carousell.photo_requests == ["https://media.karousell.com/media/photos/products/100_full"]


def test_photos_are_downloaded_once_per_run_and_scores_are_cached(tmp_path: Path) -> None:
    run = harness(tmp_path, [card("100")], FakeCarousellSource(photo=photo()))

    asyncio.run(run.tools.judge_photos(["100"], "blurry photo"))
    asyncio.run(run.tools.judge_photos(["100"], "Blurry  photo"))
    metrics = asyncio.run(run.tools.photo_metrics(["100"]))

    assert len(run.carousell.photo_requests) == 1
    assert len(run.worker.photo_batches) == 1
    assert run.budget.photos == 1
    assert metrics[0]["photo"] == "thumbnail" and metrics[0]["sharpness"] > 100


def test_photo_metrics_needs_no_model(tmp_path: Path) -> None:
    run = harness(tmp_path, [card("100"), card("101")], FakeCarousellSource(photo=photo(blurred=True)))

    metrics = asyncio.run(run.tools.photo_metrics(["100", "101"]))

    assert [entry["id"] for entry in metrics] == ["100", "101"]
    assert all(entry["sharpness"] < 100 for entry in metrics)
    assert run.worker.photo_batches == [] and run.ledger.report() == []


def test_past_the_photo_budget_photos_are_not_downloaded(tmp_path: Path) -> None:
    run = harness(tmp_path, [card(listing_id) for listing_id in ids(3)], FakeCarousellSource(photo=photo()),
                  budget=AgentBudget(max_photos=2))

    answers = asyncio.run(run.tools.judge_photos(ids(3), "blurry photo"))

    assert len(run.carousell.photo_requests) == 2
    assert answers[2] == {"id": "102", "score_1_to_5": None, "reason": "not checked: the photo budget is used up"}
    assert run.worker.photo_batches == [["100", "101"]]
    assert "Photo budget reached: 2 photos judged, 1 more refused." in run.budget.notes()


def test_a_photo_that_is_not_an_image_is_reported_and_not_sent_to_the_model(tmp_path: Path) -> None:
    run = harness(tmp_path, [card("100"), card("101")], FakeCarousellSource(
        photos={"https://media.karousell.com/media/photos/products/100_thumbnail": b"<html>"}, photo=photo()
    ))

    answers = asyncio.run(run.tools.judge_photos(["100", "101"], "blurry photo"))

    assert answers[0]["score_1_to_5"] is None and answers[0]["reason"].startswith("unreadable photo")
    assert answers[1]["score_1_to_5"] == 3
    assert run.worker.photo_batches == [["101"]]


def test_a_listing_this_run_never_saw_has_no_photo(tmp_path: Path) -> None:
    run = harness(tmp_path, [])

    (answer,) = asyncio.run(run.tools.photo_metrics(["555"]))

    assert answer == {"id": "555", "problem": "no photo URL: search for the listing first"}
    assert run.carousell.photo_requests == []


def test_the_main_photo_is_the_first_full_photo_or_else_the_thumbnail() -> None:
    assert main_photo_url({"image_urls": ["a", "b"], "thumbnail_url": "t"}) == ("a", "full")
    assert main_photo_url({"image_urls": [], "thumbnail_url": "t"}) == ("t", "thumbnail")
    assert main_photo_url({"image_urls": None, "thumbnail_url": None}) is None


# --- run_python and the toolset -----------------------------------------------------------------------------------


def test_run_python_reads_the_runs_listings(tmp_path: Path) -> None:
    run = harness(tmp_path, [card("100", price=2000), card("101", price=4000)])

    output = asyncio.run(run.tools.run_python(
        "import sqlite3\nprint(sqlite3.connect('listings.db').execute('SELECT avg(price) FROM listings').fetchone()[0])"
    ))

    assert output == "3000.0"


def test_the_toolset_offers_the_five_local_tools_with_model_facing_descriptions(tmp_path: Path) -> None:
    toolset = build_local_toolset(harness(tmp_path, []).tools)

    assert set(toolset.tools) == {"run_python", "extract_fields", "judge_listings", "judge_photos", "photo_metrics"}
    assert "x_<name>" in toolset.tools["extract_fields"].description
    assert "listings.db" in toolset.tools["run_python"].description
    assert "score_1_to_5" in toolset.tools["judge_photos"].description


def test_the_run_python_description_names_the_optional_libraries() -> None:
    assert "(pandas)" in run_python_description(["pandas"])
    assert "no pandas or numpy" in run_python_description([])


# --- extract_fields -----------------------------------------------------------------------------------------------


SIZE = FieldSpec(name="size_cm", type="int", unit="cm", hint="the width in centimetres", pattern=r"(\d+)\s*cm")
COLOUR = FieldSpec(name="colour", type="enum", hint="the frame colour", enum_values=["Black", "Oak"])


def snippet_card(listing_id: str, title: str, snippet: str | None) -> dict:
    return {**card(listing_id, title), "description_snippet": snippet}


def read_rows(tmp_path: Path) -> dict[str, dict]:
    connection = sqlite3.connect(tmp_path / "run" / "listings.db")
    connection.row_factory = sqlite3.Row
    return {row["id"]: dict(row) for row in connection.execute("SELECT * FROM listings")}


def colour_from_text(listing_id: str, block: str, field_name: str) -> object:
    if field_name == "colour":
        return "Oak" if "oak" in block.lower() else None
    return 120 if "120" in block else None


def test_extract_fields_tries_the_pattern_first_and_asks_the_model_only_for_the_rest(tmp_path: Path) -> None:
    worker = ScriptedWorker(field_answer=colour_from_text)
    cards = [snippet_card("100", "Desk 140cm", "Oak top, lightly used"), snippet_card("101", "Desk", "width 120")]
    run = harness(tmp_path, cards, worker=worker)

    result = asyncio.run(run.tools.extract_fields(["100", "101"], [SIZE, COLOUR], fetch_details=False))

    assert result["rows"] == [
        {"id": "100", "size_cm": 140, "colour": "Oak"}, {"id": "101", "size_cm": 120, "colour": None}
    ]
    assert worker.extraction_batches == [(["100", "101"], ["size_cm", "colour"])]  # 100 only needed colour
    assert result["counts"] == {"size_cm": {"pattern": 1, "model": 1, "unknown": 0},
                                "colour": {"model": 2, "unknown": 1}}
    assert result["columns"] == {"size_cm": "x_size_cm INTEGER", "colour": "x_colour TEXT"}
    assert result["text_read"] == {"snippet": 2} and result["problems"] == {}
    assert run.carousell.detail_requests == []  # fetch_details false: the card snippets were enough


def test_extracted_values_land_in_typed_columns_and_the_extracted_json(tmp_path: Path) -> None:
    worker = ScriptedWorker(field_answer=colour_from_text)
    run = harness(tmp_path, [snippet_card("100", "Desk 140cm", "Oak top")], worker=worker)

    asyncio.run(run.tools.extract_fields(["100"], [SIZE, COLOUR], fetch_details=False))

    row = read_rows(tmp_path)["100"]
    assert (row["x_size_cm"], row["x_colour"]) == (140, "Oak")
    assert json.loads(row["extracted"]) == {"colour": "Oak", "size_cm": 140}
    listings_json = json.loads((tmp_path / "run" / "listings.json").read_text())
    assert listings_json[0]["extracted"] == {"colour": "Oak", "size_cm": 140}


def test_unknown_values_stay_null_and_are_counted(tmp_path: Path) -> None:
    run = harness(tmp_path, [snippet_card("100", "Desk", "no measurements given")])  # the worker never finds any

    result = asyncio.run(run.tools.extract_fields(["100"], [SIZE], fetch_details=False))

    assert result["rows"] == [{"id": "100", "size_cm": None}]
    assert result["counts"]["size_cm"] == {"model": 1, "unknown": 1}
    assert read_rows(tmp_path)["100"]["x_size_cm"] is None


def test_model_answers_from_real_text_are_cached_by_spec(tmp_path: Path) -> None:
    worker = ScriptedWorker(field_answer=colour_from_text)
    cache = MemoryJudgementCache()
    cards = [snippet_card("100", "Desk", "Oak top"), snippet_card("101", "Table", None)]  # 101: title only
    run = harness(tmp_path, cards, worker=worker, cache=cache)

    asyncio.run(run.tools.extract_fields(["100", "101"], [COLOUR], fetch_details=False))
    again = asyncio.run(run.tools.extract_fields(["100", "101"], [COLOUR], fetch_details=False))
    changed = COLOUR.model_copy(update={"hint": "the colour of the top"})
    asyncio.run(run.tools.extract_fields(["100"], [changed], fetch_details=False))

    assert [batch for batch, _ in worker.extraction_batches] == [["100", "101"], ["101"], ["100"]]
    assert again["counts"]["colour"] == {"cached": 1, "model": 1, "unknown": 1}
    assert again["rows"][0] == {"id": "100", "colour": "Oak"}


def test_fetching_details_counts_against_the_budget_and_reports_what_was_left(tmp_path: Path) -> None:
    carousell = FakeCarousellSource(details={"100": detail("100", "Oak frame, 120 wide")})
    run = harness(tmp_path, [card("100"), card("101")], carousell, budget=AgentBudget(max_tool_calls=1),
                  worker=ScriptedWorker(field_answer=colour_from_text))

    result = asyncio.run(run.tools.extract_fields(["100", "101"], [COLOUR], fetch_details=True))

    assert carousell.detail_requests == ["100"]
    assert run.budget.tool_calls == 1
    assert result["problems"] == {"101": "no description: the tool-call budget is used up"}
    assert result["text_read"] == {"description": 1, "snippet": 1}
    assert result["rows"][0] == {"id": "100", "colour": "Oak"}


def test_unknown_listing_ids_are_reported_not_invented(tmp_path: Path) -> None:
    run = harness(tmp_path, [snippet_card("100", "Desk 90cm", None)])

    result = asyncio.run(run.tools.extract_fields(["100", "999"], [SIZE], fetch_details=False))

    assert result["rows"] == [{"id": "100", "size_cm": 90}]
    assert result["problems"] == {"999": "unknown listing id: search for it first"}


def test_a_failing_worker_leaves_its_fields_unknown_and_says_so(tmp_path: Path) -> None:
    def broken(messages, info):
        raise ModelHTTPError(status_code=500, model_name="nano", body="down")

    run = harness(tmp_path, [snippet_card("100", "Desk", "Oak")])
    run.tools.worker = WorkerJudge(FunctionModel(broken), WORKER_NAME, run.ledger)

    result = asyncio.run(run.tools.extract_fields(["100"], [COLOUR], fetch_details=False))

    assert result["rows"] == [{"id": "100", "colour": None}]
    assert result["problems"] == {"100": "the worker model failed; its fields are unknown"}
