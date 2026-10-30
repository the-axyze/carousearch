"""`carousearch account` and `carousearch outreach` with a faked service and browser: a run folder in tmp_path, a
FunctionModel drafter, the dry-run transport. No browser, no network, no model provider."""

import io
import json
import random
from pathlib import Path

import pytest
from pydantic_ai import models
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from carousearch import cli
from carousearch.agent.listings_db import RunListingsStore
from carousearch.config import OutreachSettings
from carousearch.outreach.browser import BrowserError, DryRunChatTransport
from carousearch.outreach.drafting import MessageDrafter
from carousearch.outreach.models import OutreachPolicy, SessionStatus
from carousearch.outreach.service import OutreachService
from carousearch.outreach.store import SqliteOutreachStore

from outreach_examples import START

models.ALLOW_MODEL_REQUESTS = False

RUN_ID = "20261030-090000-abc123"


def make_run(data_dir: Path) -> None:
    directory = data_dir / "runs" / RUN_ID
    store = RunListingsStore(directory)
    store.record_search_page(
        [{"id": str(index), "title": f"Listing {index}", "price": 100.0 * index, "seller_id": f"s{index}",
          "seller_username": f"user{index}", "listing_url": f"https://www.carousell.sg/p/{index}/"}
         for index in (1, 2, 3)],
        None,
    )
    store.close()
    kept = [{"listing_id": "1", "reason": "a"}, {"listing_id": "2", "reason": "b"}, {"listing_id": "3", "reason": "c"}]
    (directory / "answer.json").write_text(json.dumps({"kept": kept}), encoding="utf-8")


def drafting_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    prompt = next(
        part.content for message in messages if isinstance(message, ModelRequest)
        for part in message.parts if isinstance(part, UserPromptPart)
    )
    offer = prompt.split("Offer price: ")[1].split("\n")[0]
    return ModelResponse(parts=[ToolCallPart("message", {"body": f"Hello, would you take {offer}?"})])


class FakeServiceFactory:
    """Builds an OutreachService over tmp_path with fakes; remembers the transports a real send opened."""

    def __init__(self, session: SessionStatus | None = None) -> None:
        self.session = session or SessionStatus(logged_in=True, username="me")
        self.transports: list[DryRunChatTransport] = []

    def __call__(self, settings: OutreachSettings, data_dir: Path) -> OutreachService:
        def transport() -> DryRunChatTransport:
            self.transports.append(DryRunChatTransport(session=self.session))
            return self.transports[-1]

        return OutreachService(
            store=SqliteOutreachStore.open(data_dir / "outreach.db"),
            policy=OutreachPolicy(daily_cap=2, min_gap_seconds=30, max_gap_seconds=60),
            runs_parent=data_dir,
            drafter_factory=lambda: MessageDrafter(FunctionModel(drafting_model)),
            transport_factory=transport,
            clock=lambda: START,
            sleep=lambda seconds: None,
            rng=random.Random(2),
        )


def run(tmp_path: Path, *arguments: str, factory: FakeServiceFactory | None = None) -> int:
    return cli.main(["--data-dir", str(tmp_path), "outreach", *arguments], outreach_service_factory=factory or FakeServiceFactory())


def test_draft_prints_each_draft_with_its_offer_and_sends_nothing(tmp_path: Path, capsys) -> None:
    make_run(tmp_path)

    exit_code = run(tmp_path, "draft", RUN_ID, "--below", "25", "--limit", "2")

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "S$100" in output and "S$70" in output and "Hello, would you take $70?" in output  # 75 rounded to 10s
    assert "Hello, would you take $150?" in output and "$225" not in output  # --limit 2
    assert "2 new conversations allowed in the next 24 hours." in output
    assert "Nothing was sent." in output


def test_draft_of_an_unknown_run_is_a_usage_error(tmp_path: Path, capsys) -> None:
    assert run(tmp_path, "draft", "no-such-run", "--below", "25") == 2
    assert "no run folder" in capsys.readouterr().err


def test_draft_without_the_model_key_says_which_variable(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(cli, "load_outreach_settings", lambda: OutreachSettings(draft_model="openai:gpt-5.4-nano"))

    exit_code = cli.main(["--data-dir", str(tmp_path), "outreach", "draft", RUN_ID, "--below", "25"])

    assert exit_code == 2 and "OPENAI_API_KEY not set" in capsys.readouterr().err


def test_a_dry_run_send_shows_the_policy_and_records_nothing(tmp_path: Path, capsys) -> None:
    make_run(tmp_path)
    factory = FakeServiceFactory()
    run(tmp_path, "draft", RUN_ID, "--below", "25", factory=factory)
    capsys.readouterr()

    exit_code = run(tmp_path, "send", RUN_ID, "--all", "--dry-run", factory=factory)

    output = capsys.readouterr().out
    assert exit_code == 0 and factory.transports == []
    assert "Skipped Listing 3: the daily cap on new conversations is reached" in output
    assert "Would send to user1: Listing 1" in output and "Dry run, not sent: user1" in output
    assert "Would wait" in output
    assert "Dry run: nothing was sent and nothing was recorded." in output
    assert "2 would be sent, 1 skipped." in output
    run(tmp_path, "list", "--status", "sent")
    assert "(no messages)" in capsys.readouterr().out


def test_send_with_ids_sends_only_those_and_lists_them(tmp_path: Path, capsys) -> None:
    make_run(tmp_path)
    factory = FakeServiceFactory()
    run(tmp_path, "draft", RUN_ID, "--below", "25", factory=factory)
    capsys.readouterr()

    exit_code = run(tmp_path, "send", RUN_ID, "--ids", "1", "3", factory=factory)

    output = capsys.readouterr().out
    assert exit_code == 0 and [listing for listing, _ in factory.transports[0].sends] == ["1", "3"]
    assert "2 sent, 0 skipped." in output and "Daily cap: 0 new conversations left" in output
    run(tmp_path, "list", "--run", RUN_ID)
    listed = capsys.readouterr().out
    assert listed.count(" sent ") == 2 and listed.count(" drafted ") == 1
    assert "dry-run://chat/1" in listed


def test_send_aborts_when_the_profile_is_not_logged_in(tmp_path: Path, capsys) -> None:
    make_run(tmp_path)
    factory = FakeServiceFactory(session=SessionStatus(logged_in=False, detail="Not logged in."))
    run(tmp_path, "draft", RUN_ID, "--below", "25", factory=factory)

    exit_code = run(tmp_path, "send", RUN_ID, "--all", factory=factory)

    assert exit_code == 1 and "Nothing sent: Not logged in." in capsys.readouterr().out


def test_send_refuses_ids_that_are_not_unsent_drafts_of_the_run(tmp_path: Path, capsys) -> None:
    make_run(tmp_path)
    factory = FakeServiceFactory()
    run(tmp_path, "draft", RUN_ID, "--below", "25", factory=factory)

    assert run(tmp_path, "send", RUN_ID, "--ids", "99", factory=factory) == 2
    assert "has the id 99" in capsys.readouterr().err


def test_send_needs_all_or_ids(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        run(tmp_path, "send", RUN_ID)


def test_send_without_drafts_says_how_to_draft(tmp_path: Path, capsys) -> None:
    assert run(tmp_path, "send", RUN_ID, "--all") == 0
    assert "has no unsent drafts" in capsys.readouterr().out


class FakeLoginBrowser(DryRunChatTransport):
    def __init__(self, statuses: list[SessionStatus], fail: bool = False) -> None:
        super().__init__(session=statuses[-1])
        self.statuses = statuses
        self.fail = fail
        self.closed = False

    def open_login_page(self) -> None:
        if self.fail:
            raise BrowserError("Chromium could not start")

    def current_status(self) -> SessionStatus:
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]

    def close(self) -> None:
        self.closed = True


def test_account_status_reports_the_session_and_closes_the_browser(tmp_path: Path, capsys) -> None:
    browser = FakeLoginBrowser([SessionStatus(logged_in=False, detail="Not logged in. Run login.")])

    exit_code = cli.main(["--data-dir", str(tmp_path), "account", "status"], login_browser_factory=lambda *_: browser)

    assert exit_code == 1 and browser.closed
    assert "Not logged in. Run login." in capsys.readouterr().out


def test_account_login_waits_for_the_owner_then_reports(tmp_path: Path, capsys) -> None:
    browser = FakeLoginBrowser([SessionStatus(False), SessionStatus(True, "me", detail="Logged in as me.")])
    output = io.StringIO()

    exit_code = cli.run_account_login_command(browser, output, io.StringIO(), monotonic=lambda: 0.0, sleep=lambda s: None)

    assert exit_code == 0 and browser.closed and output.getvalue().endswith("Logged in as me.\n")


def test_account_login_reports_a_browser_that_cannot_start(tmp_path: Path) -> None:
    errors = io.StringIO()

    exit_code = cli.run_account_login_command(FakeLoginBrowser([SessionStatus(False)], fail=True), io.StringIO(), errors)

    assert exit_code == 1 and "Chromium could not start" in errors.getvalue()
