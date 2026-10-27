"""The provider's hosted web search for the planner model, capped per run (AGENT_MAX_WEB_SEARCHES).

Pydantic AI's WebSearch capability adds the native WebSearchTool to a request; here its `native` argument is a
function that counts the searches already in the run's messages and leaves the tool out once the cap is reached.
Where the provider honours `max_uses` (Anthropic), the tool also carries the remaining count, so one response
cannot search past the cap; OpenAI's Responses API has no such setting, so there one response may search more than
once before the next check. A model without hosted search (Model.profile's supported_native_tools) gets no web
search, and the answer's notes say so.

Pure except for reading the model object's profile.
"""

from collections.abc import Sequence
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import WebSearch
from pydantic_ai.messages import ModelMessage, ModelResponse, NativeToolCallPart
from pydantic_ai.models import Model
from pydantic_ai.native_tools import WebSearchTool

WEB_SEARCH_TOOL_NAME = WebSearchTool.kind  # "web_search"
SEARCH_LOCATION = {"country": "SG", "city": "Singapore"}


def supports_hosted_web_search(model: Model) -> bool:
    return WebSearchTool in model.profile.get("supported_native_tools", frozenset())


def web_search_capability(model: Model, max_searches: int) -> WebSearch[Any] | None:
    """The capped web search for this model, or None when the cap is 0 or the model has no hosted search."""
    if max_searches <= 0 or not supports_hosted_web_search(model):
        return None

    def capped_web_search(context: RunContext[Any]) -> WebSearchTool | None:
        remaining = max_searches - count_web_searches(context.messages)
        return web_search_tool(model, remaining) if remaining > 0 else None

    return WebSearch(native=capped_web_search)


def web_search_tool(model: Model, remaining: int) -> WebSearchTool:
    """Low search context (cheaper result tokens), Singapore results, and max_uses where the model applies it."""
    capped = WebSearchTool(search_context_size="low", user_location=SEARCH_LOCATION, max_uses=remaining, optional=True)
    if "max_uses" in model.unsupported_native_tool_settings(capped):
        return WebSearchTool(search_context_size="low", user_location=SEARCH_LOCATION, optional=True)
    return capped


def count_web_searches(messages: Sequence[ModelMessage]) -> int:
    return sum(
        1
        for message in messages
        if isinstance(message, ModelResponse)
        for part in message.parts
        if isinstance(part, NativeToolCallPart) and part.tool_name == WEB_SEARCH_TOOL_NAME
    )


def web_search_notes(model_label: str, supported: bool, searches_used: int, max_searches: int) -> list[str]:
    """A note when web search was wanted but the model has none, or when the cap was reached."""
    if max_searches <= 0:
        return []
    if not supported:
        return [f"Web search: not available with {model_label}, so none was made."]
    if searches_used >= max_searches:
        return [f"Web search budget reached: {searches_used} of {max_searches} searches used."]
    return []
