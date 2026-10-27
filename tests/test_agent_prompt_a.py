"""End to end, offline: the owner's prompt (A) driven by a scripted planner over the real MCP server, the real
local tools (the sandbox runs for real), a fake Carousell and a scripted worker model.

(A) "Find the average market price of the r15 motorcycle (well used and second hand), then take 80% of that as
the market benchmark, then filter on top of that for blurry or low-effort images and COE of about 2 years, to
find a bike priced 50% below the benchmark."
"""

import io
import json
import sqlite3
from pathlib import Path

from PIL import Image, ImageFilter
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from carousearch.agent.judge_cache import MemoryJudgementCache
from carousearch.agent.local_tools import LocalToolServices
from carousearch.agent.run_prompt import OUTPUT_TOOL_NAME, run_prompt_sync
from carousearch.agent.settings import AgentSettings
from carousearch.filters.schema import SearchResult

from fake_mcp_services import DETAIL, SEARCH_RESULT, FakeCarousellSource, fake_services
from scripted_model import ScriptedModel, calls
from scripted_worker import ScriptedWorker

PROMPT_A = (
    "Find the average market price of the r15 motorcycle (well used and second hand), then take 80% of that as the "
    "market benchmark, then filter on top of that for blurry or low-effort images and COE of about 2 years, to find "
    "a bike priced 50% below the benchmark."
)
# id, title, price: five ordinary used R15s, one cheap one with a short COE, one dealer post priced as an instalment.
BIKES = [
    ("101", "Yamaha R15 V3 COE 2030", 6500.0),
    ("102", "Yamaha R15 V3 lowered", 6200.0),
    ("103", "R15 V3 well kept", 6800.0),
    ("104", "Yamaha R15 v3", 7000.0),
    ("105", "R15 V3 cheap sale", 2200.0),
    ("106", "R15 V4 | MT15 | XSR155 | NMAX 3.88% instalment", 268.0),
]
COE_CRITERION = "COE has about 2 years left (between 1.5 and 2.5 years)"
PHOTO_CRITERION = "blurry, dark or low-effort photo"
BENCHMARK_CODE = """\
import json, sqlite3, statistics
rows = sqlite3.connect('listings.db').execute("SELECT id, price FROM listings WHERE price >= 1000").fetchall()
average = statistics.mean(price for _, price in rows)
benchmark = round(0.8 * average, 2)
candidates = [listing_id for listing_id, price in rows if price <= 0.5 * benchmark]
print(json.dumps({"counted": len(rows), "average": average, "benchmark": benchmark, "candidates": candidates}))
"""


def bike_search_result() -> SearchResult:
    template = SEARCH_RESULT.listings[0]
    listings = [
        template.model_copy(update={
            "id": listing_id, "title": title, "price": price, "price_display": f"S${price:,.0f}",
            "listing_url": f"https://www.carousell.sg/p/{listing_id}/",
            "thumbnail_url": f"https://media.karousell.com/media/photos/products/{listing_id}_thumbnail",
        })
        for listing_id, title, price in BIKES
    ]
    return SearchResult(listings=listings, total=len(listings), total_display=str(len(listings)), next_page=None)


def blurry_photo() -> bytes:
    image = Image.effect_noise((810, 1080), 64).convert("RGB").filter(ImageFilter.GaussianBlur(10))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()


def latest_return(messages: list[ModelMessage], tool_name: str) -> object:
    for message in reversed(messages):
        if isinstance(message, ModelRequest):
            for part in message.parts:
                if isinstance(part, ToolReturnPart) and part.tool_name == tool_name:
                    return part.content
    raise AssertionError(f"no {tool_name} result yet")


def judge_candidates(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    candidates = json.loads(latest_return(messages, "run_python"))["candidates"]
    return calls(("judge_listings", {"listing_ids": candidates, "criterion": COE_CRITERION}))


def judge_their_photos(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    candidates = json.loads(latest_return(messages, "run_python"))["candidates"]
    return calls(("judge_photos", {"listing_ids": candidates, "criterion": PHOTO_CRITERION}))


def answer_with_the_numbers(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    numbers = json.loads(latest_return(messages, "run_python"))
    coe = {item["id"]: item for item in latest_return(messages, "judge_listings")}
    photos = {item["id"]: item for item in latest_return(messages, "judge_photos")}
    benchmark = numbers["benchmark"]
    kept = []
    for listing_id in numbers["candidates"]:
        title, price = next((title, price) for bike_id, title, price in BIKES if bike_id == listing_id)
        percent_below = round(100 * (1 - price / benchmark))
        kept.append({
            "listing_id": listing_id, "title": title, "price": price,
            "url": f"https://www.carousell.sg/p/{listing_id}/", "seller": "rider",
            "reason": (
                f"S${price:,.0f}, {percent_below}% below the S${benchmark:,.0f} benchmark; "
                f"{coe[listing_id]['reason']}; photo {photos[listing_id]['score_1_to_5']}/5 "
                f"(sharpness {photos[listing_id]['sharpness']})"
            ),
        })
    notes = (
        f"Benchmark: 80% of the S${numbers['average']:,.0f} average of {numbers['counted']} used R15s; "
        "left out 1 instalment-priced dealer post."
    )
    arguments = {"kept": kept, "dropped": [], "not_shortlisted": numbers["counted"] - len(kept), "notes": notes}
    return ModelResponse(parts=[ToolCallPart(OUTPUT_TOOL_NAME, arguments)])


def run_prompt_a(tmp_path: Path) -> tuple:
    planner = ScriptedModel(
        calls(("category_fields", {"category": "7100"})),
        calls(("search_listings", {
            "request": {"query": "yamaha r15", "category": "1595"}, "limit": 48, "fields": ["id", "title", "price"]
        })),
        calls(("run_python", {"code": BENCHMARK_CODE})),
        judge_candidates,
        judge_their_photos,
        answer_with_the_numbers,
    )
    worker = ScriptedWorker(
        listing_answer=lambda listing_id, block: ("yes", "COE till Nov 2028, about 2.1 years"),
        photo_answer=lambda listing_id: (4, "blurry and dim"),
    )
    carousell = FakeCarousellSource(
        search_result=bike_search_result(),
        details={"105": DETAIL.model_copy(update={
            "id": "105", "title": "R15 V3 cheap sale", "price": 2200.0, "description": "COE till Nov 2028. Must go.",
            "image_urls": ["https://media.karousell.com/media/photos/products/105_full"],
        })},
        photo=blurry_photo(),
    )
    local = LocalToolServices(
        run_directory=tmp_path / "run", judgement_cache=MemoryJudgementCache(), worker_model=FunctionModel(worker),
        network_prefix=(),
    )
    answer = run_prompt_sync(PROMPT_A, fake_services(carousell), AgentSettings(), FunctionModel(planner), local=local)
    return answer, planner, worker, carousell


def test_prompt_a_computes_the_benchmark_in_the_sandbox_and_reports_it_in_the_reasons(tmp_path: Path) -> None:
    answer, _, _, _ = run_prompt_a(tmp_path)

    # Five used R15s count (the S$268 instalment post is under S$1,000): average S$5,740, benchmark S$4,592, and
    # only the S$2,200 bike is at least 50% below it.
    (kept,) = answer.kept
    assert kept.listing_id == "105"
    assert kept.reason.startswith("S$2,200, 52% below the S$4,592 benchmark; COE till Nov 2028, about 2.1 years")
    assert "photo 4/5" in kept.reason
    assert answer.notes.startswith("Benchmark: 80% of the S$5,740 average of 5 used R15s")
    assert answer.not_shortlisted == 4


def test_prompt_a_uses_each_tool_once_in_order_and_reports_both_models(tmp_path: Path) -> None:
    answer, _, worker, carousell = run_prompt_a(tmp_path)

    assert [(call.tool_name, call.status) for call in answer.tool_trace] == [
        ("category_fields", "ok"), ("search_listings", "ok"), ("run_python", "ok"), ("judge_listings", "ok"),
        ("judge_photos", "ok"),
    ]
    assert (worker.listing_batches, worker.photo_batches) == ([["105"]], [["105"]])
    assert carousell.detail_requests == ["105"]
    assert carousell.photo_requests == ["https://media.karousell.com/media/photos/products/105_full"]
    assert [usage.role for usage in answer.usage] == ["planner", "worker"]
    assert answer.usage[1].requests == 2


def test_the_benchmark_maths_ran_in_the_sandbox(tmp_path: Path) -> None:
    run_prompt_a(tmp_path)

    output = (tmp_path / "run" / "python" / "001.stdout").read_text()
    assert json.loads(output) == {"counted": 5, "average": 5740, "benchmark": 4592.0, "candidates": ["105"]}


def test_the_search_result_the_planner_reads_has_only_the_fields_it_asked_for(tmp_path: Path) -> None:
    _, planner, _, _ = run_prompt_a(tmp_path)

    search_return = next(
        part.content
        for messages, _ in planner.requests
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart) and part.tool_name == "search_listings"
    )
    assert {tuple(sorted(listing)) for listing in search_return["listings"]} == {("id", "price", "title")}
    database = sqlite3.connect(tmp_path / "run" / "listings.db")
    assert database.execute("SELECT count(*), count(thumbnail_url) FROM listings").fetchone() == (6, 6)
