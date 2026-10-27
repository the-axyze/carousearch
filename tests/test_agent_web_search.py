"""The capped hosted web search (no provider is called: models are built offline or scripted)."""

from pydantic_ai import RunContext
from pydantic_ai.messages import ModelResponse, NativeToolCallPart, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.native_tools import AbstractNativeTool, WebSearchTool
from pydantic_ai.usage import RunUsage

from carousearch.agent.web_search import (
    count_web_searches,
    supports_hosted_web_search,
    web_search_capability,
    web_search_notes,
    web_search_tool,
)
from carousearch.language_models import ProviderKeys, build_model

from scripted_model import ScriptedModel, final_answer

KEYS = ProviderKeys(openai="sk-test", anthropic="sk-ant-test")


def searches(count: int) -> list[ModelResponse]:
    return [ModelResponse(parts=[NativeToolCallPart("web_search", {"query": f"q{n}"})]) for n in range(count)]


def context(model: FunctionModel, messages: list) -> RunContext[None]:
    return RunContext(deps=None, model=model, usage=RunUsage(), messages=messages)


def native_tool_of(capability, run_context: RunContext[None]) -> WebSearchTool | None:
    return capability.native(run_context)


def test_searches_are_counted_from_the_responses() -> None:
    messages = [*searches(2), ModelResponse(parts=[TextPart("done")])]

    assert count_web_searches(messages) == 2


def test_the_tool_is_offered_until_the_cap_is_reached_with_the_remaining_uses() -> None:
    model = FunctionModel(ScriptedModel(final_answer()))
    capability = web_search_capability(model, max_searches=3)

    first = native_tool_of(capability, context(model, searches(1)))

    assert isinstance(first, WebSearchTool) and first.max_uses == 2 and first.optional
    assert first.search_context_size == "low" and first.user_location == {"country": "SG", "city": "Singapore"}
    assert native_tool_of(capability, context(model, searches(3))) is None


def test_no_capability_when_the_cap_is_zero() -> None:
    assert web_search_capability(FunctionModel(ScriptedModel(final_answer())), max_searches=0) is None


class NoMaxUsesModel(FunctionModel):
    def unsupported_native_tool_settings(self, native_tool: AbstractNativeTool) -> frozenset[str]:
        return native_tool.restricting_settings & {"max_uses"}


def test_a_model_that_cannot_apply_max_uses_gets_the_tool_without_it() -> None:
    tool = web_search_tool(NoMaxUsesModel(ScriptedModel(final_answer())), remaining=2)

    assert tool.max_uses is None


def test_openai_responses_has_hosted_search_without_max_uses() -> None:
    model = build_model("openai:gpt-5.4-mini", KEYS)

    assert supports_hosted_web_search(model)
    assert web_search_tool(model, remaining=3).max_uses is None


def test_anthropic_applies_max_uses() -> None:
    model = build_model("anthropic:claude-haiku-5-5", KEYS)

    assert supports_hosted_web_search(model)
    assert web_search_tool(model, remaining=3).max_uses == 3


def test_a_model_without_hosted_search_gets_none_and_a_note() -> None:
    model = build_model("openai-chat:gpt-5.4-mini", KEYS)

    assert not supports_hosted_web_search(model)
    assert web_search_capability(model, max_searches=3) is None
    assert web_search_notes("openai-chat:gpt-5.4-mini", False, 0, 3) == [
        "Web search: not available with openai-chat:gpt-5.4-mini, so none was made."
    ]


def test_notes_say_when_the_cap_was_reached_and_nothing_otherwise() -> None:
    assert web_search_notes("m", True, 3, 3) == ["Web search budget reached: 3 of 3 searches used."]
    assert web_search_notes("m", True, 1, 3) == []
    assert web_search_notes("m", False, 0, 0) == []
