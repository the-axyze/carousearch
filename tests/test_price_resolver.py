import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from carousearch.config import DEFAULT_PRICE_MODEL, ScreeningSettings, load_screening_settings
from carousearch.screening.models import PriceSource, ResolvedPrice
from carousearch.screening.price_resolver import (
    MAX_DESCRIPTION_CHARACTERS,
    MAX_RESPONSE_TOKENS,
    PRICE_ANSWER_SCHEMA,
    ClaudePriceResolver,
    NoopPriceResolver,
    PriceResolutionError,
    build_price_resolver,
    estimate_cost_usd,
    parse_price_answer,
)

QUERY = "iphone 15 pro 256gb"
TITLE = "iPhone 15 / 15 Pro / 15 Pro Max all storage"
DESCRIPTION = "iPhone 15 128GB - $579\niPhone 15 Pro 256GB - $850\niPhone 15 Pro Max 256GB - $1,050"
UNREADABLE = "the model's answer could not be read"


def text_response(text: str, stop_reason: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=420, output_tokens=40),
    )


def answer(amount: float | None, source: str, note: str = "") -> SimpleNamespace:
    return text_response(json.dumps({"amount": amount, "source": source, "note": note}))


class FakeMessages:
    def __init__(self, response: object = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict] = []

    def create(self, **request: object) -> object:
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        return self.response


class FakeAnthropicClient:
    """Stands in for anthropic.Anthropic: only `messages.create` is used."""

    def __init__(self, response: object = None, error: Exception | None = None) -> None:
        self.messages = FakeMessages(response, error)


def resolve_with(response: object) -> tuple[ResolvedPrice, FakeAnthropicClient]:
    client = FakeAnthropicClient(response)
    resolved = ClaudePriceResolver(client, DEFAULT_PRICE_MODEL).resolve(QUERY, TITLE, DESCRIPTION, 579)
    return resolved, client


def test_request_carries_the_search_title_and_description() -> None:
    _, client = resolve_with(answer(850, "description", "iPhone 15 Pro 256GB - $850"))

    [request] = client.messages.calls
    prompt = request["messages"][0]["content"]
    assert request["model"] == "claude-haiku-5-5"
    assert request["max_tokens"] == MAX_RESPONSE_TOKENS
    assert MAX_RESPONSE_TOKENS <= 512
    assert f"Search: {QUERY}" in prompt
    assert f"Title: {TITLE}" in prompt
    assert DESCRIPTION in prompt
    assert "Listed price: S$579" in prompt
    assert "not_stated" in request["system"] and "Never guess" in request["system"]
    assert request["output_config"]["format"] == {"type": "json_schema", "schema": PRICE_ANSWER_SCHEMA}
    assert request["thinking"] == {"type": "disabled"}


def test_description_price_is_returned_with_its_note() -> None:
    resolved, _ = resolve_with(answer(850, "description", "iPhone 15 Pro 256GB - $850"))

    assert resolved == ResolvedPrice(amount=850, source=PriceSource.DESCRIPTION, note="iPhone 15 Pro 256GB - $850")


def test_not_stated_answer() -> None:
    resolved, _ = resolve_with(answer(None, "not_stated", "only 128GB is priced"))

    assert resolved == ResolvedPrice(amount=None, source=PriceSource.NOT_STATED, note="only 128GB is priced")


@pytest.mark.parametrize(
    ("response", "note"),
    [
        (text_response("not json at all"), UNREADABLE),
        (text_response('{"amount": 850}'), UNREADABLE),
        (text_response('{"amount": "lots", "source": "description", "note": ""}'), UNREADABLE),
        (answer(None, "description"), "the model named no usable amount"),
        (text_response('{"amount": 8', stop_reason="max_tokens"), "the model's answer was cut off"),
        (text_response("", stop_reason="refusal"), "the model declined to answer"),
        (SimpleNamespace(content=[], stop_reason="end_turn", usage=None), "the model returned no answer"),
    ],
)
def test_unusable_responses_become_not_stated_with_a_note(response: object, note: str) -> None:
    resolved, _ = resolve_with(response)

    assert resolved == ResolvedPrice(amount=None, source=PriceSource.NOT_STATED, note=note)


def test_long_descriptions_are_truncated() -> None:
    client = FakeAnthropicClient(answer(None, "not_stated", "no price"))
    description = "a" * MAX_DESCRIPTION_CHARACTERS + "TAIL-THAT-IS-NOT-SENT"

    ClaudePriceResolver(client, DEFAULT_PRICE_MODEL).resolve(QUERY, TITLE, description, 579)

    prompt = client.messages.calls[0]["messages"][0]["content"]
    assert "a" * MAX_DESCRIPTION_CHARACTERS in prompt
    assert "TAIL-THAT-IS-NOT-SENT" not in prompt
    assert "[cut]" in prompt


def test_blank_description_needs_no_call() -> None:
    client = FakeAnthropicClient(answer(850, "description"))

    resolved = ClaudePriceResolver(client, DEFAULT_PRICE_MODEL).resolve(QUERY, TITLE, "  \n", 579)

    assert resolved.source == PriceSource.NOT_STATED
    assert client.messages.calls == []


def test_api_failures_raise_price_resolution_error() -> None:
    anthropic = pytest.importorskip("anthropic")
    import httpx2

    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    connection_error = anthropic.APIConnectionError(message="connection reset", request=request)
    rate_limited = anthropic.RateLimitError(
        "rate limited", response=httpx2.Response(429, request=request), body=None
    )

    for error, message in [(connection_error, "could not reach"), (rate_limited, "HTTP 429")]:
        resolver = ClaudePriceResolver(FakeAnthropicClient(error=error), DEFAULT_PRICE_MODEL)
        with pytest.raises(PriceResolutionError, match=message):
            resolver.resolve(QUERY, TITLE, DESCRIPTION, 579)


def test_parse_price_answer_trims_an_empty_note() -> None:
    assert parse_price_answer('{"amount": 850, "source": "description", "note": " "}') == ResolvedPrice(
        amount=850, source=PriceSource.DESCRIPTION, note=None
    )


def test_cost_estimate_uses_haiku_prices() -> None:
    assert estimate_cost_usd(1_000_000, 0) == pytest.approx(0.10)
    assert estimate_cost_usd(0, 1_000_000) == pytest.approx(0.50)


def test_noop_resolver_keeps_the_listed_price() -> None:
    resolved = NoopPriceResolver().resolve(QUERY, TITLE, DESCRIPTION, 579)

    assert resolved == ResolvedPrice(amount=579, source=PriceSource.LISTED)


def test_factory_picks_noop_without_a_key() -> None:
    def must_not_be_called(api_key: str) -> object:
        raise AssertionError("no client without a key")

    resolver = build_price_resolver(ScreeningSettings(anthropic_api_key=None), create_client=must_not_be_called)

    assert isinstance(resolver, NoopPriceResolver)


def test_factory_picks_claude_with_a_key() -> None:
    keys_used: list[str] = []

    def create_client(api_key: str) -> FakeAnthropicClient:
        keys_used.append(api_key)
        return FakeAnthropicClient(answer(850, "description"))

    resolver = build_price_resolver(ScreeningSettings(anthropic_api_key="sk-test"), create_client=create_client)

    assert isinstance(resolver, ClaudePriceResolver)
    assert keys_used == ["sk-test"]
    assert resolver.resolve(QUERY, TITLE, DESCRIPTION, 579).amount == 850


def test_screening_settings_read_the_optional_key(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=123:abc\nANTHROPIC_API_KEY= sk-from-file \n")

    assert load_screening_settings(environment={}, env_file=env_file).anthropic_api_key == "sk-from-file"
    assert load_screening_settings(environment={"ANTHROPIC_API_KEY": ""}, env_file=env_file).anthropic_api_key is None
    missing = load_screening_settings(environment={}, env_file=tmp_path / "missing.env")
    assert missing == ScreeningSettings(anthropic_api_key=None, price_model="claude-haiku-5-5")
    assert "sk-" not in repr(load_screening_settings(environment={}, env_file=env_file))
