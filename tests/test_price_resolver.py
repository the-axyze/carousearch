"""The price resolver over a scripted Pydantic AI model: no provider, no network."""

from pathlib import Path

import pytest
from pydantic_ai import models
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from carousearch.config import ScreeningSettings, load_screening_settings
from carousearch.language_models import ProviderKeys
from carousearch.screening.models import PriceSource, ResolvedPrice
from carousearch.screening.price_resolver import (
    MAX_DESCRIPTION_CHARACTERS,
    ModelPriceResolver,
    NoopPriceResolver,
    PriceAnswer,
    PriceResolutionError,
    build_price_resolver,
    resolved_price_from_answer,
)

models.ALLOW_MODEL_REQUESTS = False  # a test that reaches a real provider fails instead of spending money

QUERY = "iphone 15 pro 256gb"
TITLE = "iPhone 15 / 15 Pro / 15 Pro Max all storage"
DESCRIPTION = "iPhone 15 128GB - $579\niPhone 15 Pro 256GB - $850\niPhone 15 Pro Max 256GB - $1,050"


class PriceScript:
    """A FunctionModel function that answers with `answer` (the price tool's arguments) and keeps each request."""

    def __init__(self, answer: dict | None = None, error: Exception | None = None) -> None:
        self.answer = answer
        self.error = error
        self.requests: list[tuple[list[ModelMessage], AgentInfo]] = []

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.requests.append((messages, info))
        if self.error is not None:
            raise self.error
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, self.answer)])

    def prompt(self) -> str:
        first_request = self.requests[0][0][0]
        assert isinstance(first_request, ModelRequest)
        return next(part.content for part in first_request.parts if isinstance(part, UserPromptPart))


def resolver_for(script: PriceScript) -> ModelPriceResolver:
    return ModelPriceResolver(lambda: FunctionModel(script), "openai:gpt-5.4-nano")


def answer(amount: float | None, source: str, note: str = "") -> dict:
    return {"amount": amount, "source": source, "note": note}


def test_the_prompt_carries_the_search_title_price_and_description() -> None:
    script = PriceScript(answer(850, "description", "iPhone 15 Pro 256GB - $850"))

    resolver_for(script).resolve(QUERY, TITLE, DESCRIPTION, 579)

    prompt = script.prompt()
    assert f"Search: {QUERY}" in prompt
    assert f"Title: {TITLE}" in prompt
    assert DESCRIPTION in prompt
    assert "Listed price: S$579" in prompt
    instructions = script.requests[0][1].instructions
    assert "not_stated" in instructions and "Never guess" in instructions


def test_a_description_price_is_returned_with_its_note() -> None:
    resolved = resolver_for(PriceScript(answer(850, "description", "iPhone 15 Pro 256GB - $850"))).resolve(
        QUERY, TITLE, DESCRIPTION, 579
    )

    assert resolved == ResolvedPrice(amount=850, source=PriceSource.DESCRIPTION, note="iPhone 15 Pro 256GB - $850")


def test_a_not_stated_answer() -> None:
    resolved = resolver_for(PriceScript(answer(None, "not_stated", "only 128GB is priced"))).resolve(
        QUERY, TITLE, DESCRIPTION, 579
    )

    assert resolved == ResolvedPrice(amount=None, source=PriceSource.NOT_STATED, note="only 128GB is priced")


@pytest.mark.parametrize("amount", [None, 0, -5])
def test_an_unusable_amount_becomes_not_stated(amount: float | None) -> None:
    resolved = resolved_price_from_answer(PriceAnswer(amount=amount, source="description", note="x"))

    assert resolved == ResolvedPrice(amount=None, source=PriceSource.NOT_STATED, note="the model named no usable amount")


def test_an_empty_note_becomes_none() -> None:
    assert resolved_price_from_answer(PriceAnswer(amount=850, source="description", note=" ")) == ResolvedPrice(
        amount=850, source=PriceSource.DESCRIPTION, note=None
    )


def test_long_descriptions_are_truncated() -> None:
    script = PriceScript(answer(None, "not_stated", "no price"))
    description = "a" * MAX_DESCRIPTION_CHARACTERS + "TAIL-THAT-IS-NOT-SENT"

    resolver_for(script).resolve(QUERY, TITLE, description, 579)

    assert "a" * MAX_DESCRIPTION_CHARACTERS in script.prompt()
    assert "TAIL-THAT-IS-NOT-SENT" not in script.prompt()
    assert "[cut]" in script.prompt()


def test_a_blank_description_needs_no_call() -> None:
    script = PriceScript(answer(850, "description"))

    resolved = resolver_for(script).resolve(QUERY, TITLE, "  \n", 579)

    assert resolved.source == PriceSource.NOT_STATED
    assert script.requests == []


def test_a_provider_error_raises_price_resolution_error() -> None:
    script = PriceScript(error=ModelHTTPError(429, "openai:gpt-5.4-nano", body="rate limited"))

    with pytest.raises(PriceResolutionError, match="openai:gpt-5.4-nano"):
        resolver_for(script).resolve(QUERY, TITLE, DESCRIPTION, 579)


def test_answers_that_never_validate_raise_price_resolution_error() -> None:
    script = PriceScript({"amount": "lots", "source": "description", "note": ""})

    with pytest.raises(PriceResolutionError):
        resolver_for(script).resolve(QUERY, TITLE, DESCRIPTION, 579)
    assert len(script.requests) == 2  # the first answer and one retry


def test_each_lookup_builds_a_fresh_model() -> None:
    built: list[FunctionModel] = []
    script = PriceScript(answer(850, "description"))

    def create_model() -> FunctionModel:
        built.append(FunctionModel(script))
        return built[-1]

    resolver = ModelPriceResolver(create_model, "openai:gpt-5.4-nano")
    resolver.resolve(QUERY, TITLE, DESCRIPTION, 579)
    resolver.resolve(QUERY, TITLE, DESCRIPTION, 579)

    assert len(built) == 2 and built[0] is not built[1]


def test_the_noop_resolver_keeps_the_listed_price() -> None:
    resolved = NoopPriceResolver().resolve(QUERY, TITLE, DESCRIPTION, 579)

    assert resolved == ResolvedPrice(amount=579, source=PriceSource.LISTED)


def test_the_factory_picks_noop_without_the_worker_models_key() -> None:
    settings = ScreeningSettings(price_model="openai:gpt-5.4-nano", keys=ProviderKeys(anthropic="sk-ant"))

    assert isinstance(build_price_resolver(settings), NoopPriceResolver)


def test_the_factory_picks_the_worker_model_with_its_key() -> None:
    settings = ScreeningSettings(price_model="anthropic:claude-haiku-5-5", keys=ProviderKeys(anthropic="sk-ant"))

    resolver = build_price_resolver(settings)

    assert isinstance(resolver, ModelPriceResolver)
    assert resolver.model_name == "anthropic:claude-haiku-5-5"


def test_the_factory_uses_an_injected_model() -> None:
    script = PriceScript(answer(850, "description"))

    resolver = build_price_resolver(ScreeningSettings(), create_model=lambda: FunctionModel(script))

    assert resolver.resolve(QUERY, TITLE, DESCRIPTION, 579).amount == 850


def test_screening_settings_use_the_worker_model_and_its_key(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=123:abc\nOPENAI_API_KEY= sk-from-file \nAGENT_WORKER_MODEL=openai:gpt-5-nano\n")

    settings = load_screening_settings(environment={}, env_file=env_file)

    assert settings.price_model == "openai:gpt-5-nano"
    assert settings.keys.openai == "sk-from-file"
    assert settings.missing_key_variable() is None
    assert "sk-" not in repr(settings)
    missing = load_screening_settings(environment={}, env_file=tmp_path / "missing.env")
    assert missing == ScreeningSettings(price_model="openai:gpt-5.4-nano", keys=ProviderKeys())
    assert missing.missing_key_variable() == "OPENAI_API_KEY"


def test_screening_settings_follow_the_deprecated_agent_model(tmp_path: Path) -> None:
    settings = load_screening_settings(environment={"AGENT_MODEL": "anthropic:claude-haiku-5-5"},
                                       env_file=tmp_path / "missing.env")

    assert settings.price_model == "anthropic:claude-haiku-5-5"
