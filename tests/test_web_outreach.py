"""Outreach in the web app through FastAPI's TestClient: the panel under an answer, drafting (a FunctionModel),
sending (the dry-run transport), polling a batch and the /outreach page. No network, no browser."""

import json
import random
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic_ai import models
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from carousearch.agent.listings_db import RunListingsStore
from carousearch.agent.result import AgentAnswer, KeptListing
from carousearch.config import ScreeningSettings
from carousearch.outreach.browser import DryRunChatTransport
from carousearch.outreach.drafting import MessageDrafter
from carousearch.outreach.models import OutreachPolicy, SessionStatus
from carousearch.outreach.service import OutreachService
from carousearch.outreach.store import SqliteOutreachStore
from carousearch.screening.pipeline import ScreeningServices
from carousearch.web.app import WebSettings, create_app
from carousearch.web.services import WebServices

models.ALLOW_MODEL_REQUESTS = False

NOW = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)
RUN_FOLDER = "20261030-090000-webrun1"
HTMX = {"HX-Request": "true"}


def drafting_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    prompt = next(
        part.content for message in messages if isinstance(message, ModelRequest)
        for part in message.parts if isinstance(part, UserPromptPart)
    )
    title = prompt.split("Listing title: ")[1].split("\n")[0]
    offer = prompt.split("Offer price: ")[1].split("\n")[0]
    return ModelResponse(parts=[ToolCallPart("message", {"body": f"Hi, saw your {title}. Would {offer} work?"})])


def make_run_folder(data_dir: Path) -> Path:
    directory = data_dir / "runs" / RUN_FOLDER
    store = RunListingsStore(directory)
    store.record_search_page(
        [{"id": str(index), "title": f"Lamp {index}", "price": 100.0 * index, "seller_id": f"s{index}",
          "seller_username": f"user{index}", "listing_url": f"https://www.carousell.sg/p/{index}/"}
         for index in (1, 2, 3)],
        None,
    )
    store.close()
    kept = [{"listing_id": str(index), "reason": "fits"} for index in (1, 2, 3)]
    (directory / "answer.json").write_text(json.dumps({"kept": kept}), encoding="utf-8")
    return directory


class Clock:
    def __call__(self) -> datetime:
        return NOW


class NoCarousell:
    def search(self, request):
        raise AssertionError("outreach never searches")

    def get_listing(self, listing_id):
        raise AssertionError("outreach never fetches details")


def kept(listing_id: str) -> KeptListing:
    return KeptListing(listing_id=listing_id, title=f"Lamp {listing_id}", price=100.0, url="https://x", seller="u",
                       reason="fits")


def build_services(tmp_path: Path, session: SessionStatus | None = None, missing_key: str | None = None) -> WebServices:
    session = session or SessionStatus(logged_in=True, username="me")
    store = SqliteOutreachStore.open(tmp_path / "outreach.db")
    outreach = OutreachService(
        store=store,
        policy=OutreachPolicy(daily_cap=2, min_gap_seconds=0, max_gap_seconds=0),
        runs_parent=tmp_path,
        drafter_factory=lambda: MessageDrafter(FunctionModel(drafting_model)),
        transport_factory=lambda: DryRunChatTransport(session=session),
        clock=Clock(),
        sleep=lambda seconds: None,
        rng=random.Random(4),
    )
    screening = ScreeningServices(client=NoCarousell(), store=None, resolver=None, clock=Clock())
    return WebServices(
        carousell=NoCarousell(), screening=screening, price_lookup_enabled=False, runs_root=tmp_path,
        outreach=outreach, outreach_missing_key=missing_key,
    )


def open_client(services: WebServices) -> TestClient:
    settings = WebSettings(data_dir=Path("unused"), screening=ScreeningSettings())
    return TestClient(create_app(settings, services_factory=lambda: services))


def finished_run(services: WebServices, run_directory: Path, kept_ids: list[str]) -> str:
    run = services.prompt_runs.start("a lamp", NOW, run_id="ask1")
    run.run_directory = run_directory
    run.finish(AgentAnswer(kept=[kept(listing_id) for listing_id in kept_ids], dropped=[], notes="", tool_trace=[]), NOW)
    return run.run_id


def draft(client: TestClient, percent: str = "25") -> str:
    response = client.post(
        f"/outreach/runs/{RUN_FOLDER}/drafts", data={"percent_below": percent, "round_to": "50", "note": ""},
        headers=HTMX,
    )
    assert response.status_code == 200, response.text
    return response.text


def batch_id_in(page: str) -> str:
    return page.split("/outreach/batches/")[1].split('"')[0]


def wait_for_batch(client: TestClient, batch_id: str) -> str:
    for _ in range(100):
        page = client.get(f"/outreach/batches/{batch_id}").text
        if "hx-trigger" not in page:
            return page
        time.sleep(0.02)
    raise AssertionError("the batch never finished")


# --- the panel ----------------------------------------------------------------------------------------------------


def test_a_finished_answer_shows_the_message_sellers_panel(tmp_path: Path) -> None:
    services = build_services(tmp_path)
    run_id = finished_run(services, make_run_folder(tmp_path), ["1", "2"])
    with open_client(services) as client:
        page = client.get(f"/ask/{run_id}").text

    assert "Message sellers" in page and f'hx-post="/outreach/runs/{RUN_FOLDER}/drafts"' in page
    assert 'name="percent_below"' in page and 'value="25"' in page and "kept listing (2)" in page


def test_no_panel_without_kept_listings_or_outside_the_data_folder(tmp_path: Path) -> None:
    services = build_services(tmp_path)
    empty = finished_run(services, make_run_folder(tmp_path), [])
    with open_client(services) as client:
        assert "Message sellers" not in client.get(f"/ask/{empty}").text
        services.prompt_runs.get(empty).answer.kept.append(kept("1"))
        services.prompt_runs.get(empty).run_directory = tmp_path / "elsewhere" / RUN_FOLDER
        assert "Message sellers" not in client.get(f"/ask/{empty}").text


def test_the_panel_says_which_key_drafting_needs(tmp_path: Path) -> None:
    services = build_services(tmp_path, missing_key="OPENAI_API_KEY")
    run_id = finished_run(services, make_run_folder(tmp_path), ["1"])
    with open_client(services) as client:
        page = client.get(f"/ask/{run_id}").text
        refused = client.post(f"/outreach/runs/{RUN_FOLDER}/drafts", data={"percent_below": "25"}, headers=HTMX)

    assert "Drafting needs OPENAI_API_KEY" in page and 'name="percent_below"' not in page
    assert refused.status_code == 409


# --- drafting -----------------------------------------------------------------------------------------------------


def test_drafting_shows_each_draft_to_review_with_one_send_button(tmp_path: Path) -> None:
    services = build_services(tmp_path)
    make_run_folder(tmp_path)
    with open_client(services) as client:
        page = draft(client)

    assert page.count('name="message_id"') == 3
    assert "Hi, saw your Lamp 1. Would $70 work?" in page and "S$70" in page and "S$100" in page
    assert 'href="https://www.carousell.sg/p/2/"' in page
    assert page.count(" checked") == 2  # the third is beyond the daily cap of 2
    assert "Would be skipped: the daily cap on new conversations is reached" in page
    assert "Send 2 messages" in page and 'name="dry_run"' in page
    assert "2 new conversations allowed in the next 24 hours." in page


def test_drafting_needs_the_pages_own_request_and_a_sensible_rule(tmp_path: Path) -> None:
    services = build_services(tmp_path)
    make_run_folder(tmp_path)
    with open_client(services) as client:
        assert client.post(f"/outreach/runs/{RUN_FOLDER}/drafts", data={"percent_below": "25"}).status_code == 403
        bad = client.post(f"/outreach/runs/{RUN_FOLDER}/drafts", data={"percent_below": "95"}, headers=HTMX)
        unknown = client.post("/outreach/runs/no-such-run/drafts", data={"percent_below": "25"}, headers=HTMX)

    assert bad.status_code == 422 and "percentage from 1 to 90" in bad.text
    assert unknown.status_code == 404


# --- sending ------------------------------------------------------------------------------------------------------


def message_ids(services: WebServices) -> list[int]:
    return [message.id for message in services.outreach.store.list_for_run(RUN_FOLDER)]


def test_a_dry_run_send_reports_progress_and_records_nothing(tmp_path: Path) -> None:
    services = build_services(tmp_path)
    make_run_folder(tmp_path)
    with open_client(services) as client:
        draft(client)
        first, second, _ = message_ids(services)
        response = client.post(
            f"/outreach/runs/{RUN_FOLDER}/send",
            data={"message_id": [str(first), str(second)], f"body_{first}": "Hi, would $70 do?", "dry_run": "1"},
            headers=HTMX,
        )
        assert response.status_code == 200 and 'id="outreach-batch"' in response.text
        final = wait_for_batch(client, batch_id_in(response.text))

    assert "Dry run done (nothing sent or recorded): 2 would be sent, 0 skipped." in final
    assert "Would send to user1: Lamp 1" in final and "Dry run, not sent: user2" in final
    statuses = [message.status for message in services.outreach.store.list_for_run(RUN_FOLDER)]
    assert statuses == ["drafted"] * 3
    assert services.outreach.store.get_many([first])[0].body == "Hi, would $70 do?"  # the edit is kept


def test_a_send_sends_the_ticked_drafts_and_the_outreach_page_lists_them(tmp_path: Path) -> None:
    services = build_services(tmp_path)
    make_run_folder(tmp_path)
    with open_client(services) as client:
        draft(client)
        first, _, third = message_ids(services)
        response = client.post(
            f"/outreach/runs/{RUN_FOLDER}/send", data={"message_id": [str(first), str(third)]}, headers=HTMX
        )
        final = wait_for_batch(client, batch_id_in(response.text))
        page = client.get("/outreach").text
        sent_only = client.get("/outreach?status=sent").text

    assert "Done: 2 sent, 0 skipped." in final and "All messages" in final
    statuses = [message.status for message in services.outreach.store.list_for_run(RUN_FOLDER)]
    assert statuses == ["sent", "drafted", "sent"]
    assert page.count("outreach-status-sent") == 2 and page.count("outreach-status-drafted") == 1
    assert "Hi, saw your Lamp 3. Would $220 work?" in page and RUN_FOLDER in page
    assert sent_only.count("outreach-status-") == 2


def test_a_send_is_aborted_when_the_profile_is_not_logged_in(tmp_path: Path) -> None:
    services = build_services(tmp_path, session=SessionStatus(logged_in=False, detail="Not logged in. Run login."))
    make_run_folder(tmp_path)
    with open_client(services) as client:
        draft(client)
        response = client.post(
            f"/outreach/runs/{RUN_FOLDER}/send", data={"message_id": [str(message_ids(services)[0])]}, headers=HTMX
        )
        final = wait_for_batch(client, batch_id_in(response.text))

    assert "Nothing sent. Not logged in. Run login." in final
    assert services.outreach.store.list_for_run(RUN_FOLDER)[0].status == "approved"


def test_a_send_needs_a_ticked_draft_and_the_pages_own_request(tmp_path: Path) -> None:
    services = build_services(tmp_path)
    make_run_folder(tmp_path)
    with open_client(services) as client:
        draft(client)
        nothing = client.post(f"/outreach/runs/{RUN_FOLDER}/send", data={}, headers=HTMX)
        foreign = client.post(f"/outreach/runs/{RUN_FOLDER}/send", data={"message_id": ["1"]})

    assert nothing.status_code == 422 and "Tick at least one" in nothing.text
    assert foreign.status_code == 403


def test_unknown_batches_are_404(tmp_path: Path) -> None:
    with open_client(build_services(tmp_path)) as client:
        assert client.get("/outreach/batches/nope").status_code == 404
        assert client.post("/outreach/batches/nope/stop", headers=HTMX).status_code == 404


def test_the_outreach_page_without_messages_says_how_to_start(tmp_path: Path) -> None:
    with open_client(build_services(tmp_path)) as client:
        page = client.get("/outreach")

    assert page.status_code == 200 and "No messages yet." in page.text


@pytest.mark.parametrize("status", ["sent", "bogus"])
def test_the_status_filter_ignores_unknown_values(tmp_path: Path, status: str) -> None:
    with open_client(build_services(tmp_path)) as client:
        assert client.get(f"/outreach?status={status}").status_code == 200
