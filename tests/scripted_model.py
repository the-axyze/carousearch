"""A scripted model for agent tests: a FunctionModel function that plays its turns in order, plus helpers that build
tool-call turns and the final answer. Shared by the agent and web tests; never reaches a provider."""

from collections.abc import Callable

from pydantic_ai import models
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo

from carousearch.agent.run_prompt import OUTPUT_TOOL_NAME

models.ALLOW_MODEL_REQUESTS = False  # a test that reaches a real provider fails instead of spending money

Turn = ModelResponse | Callable[[list[ModelMessage], AgentInfo], ModelResponse]


class ScriptedModel:
    """A FunctionModel function: plays its turns in order (the last one repeats) and keeps every request."""

    def __init__(self, *turns: Turn) -> None:
        self.turns = turns
        self.requests: list[tuple[list[ModelMessage], AgentInfo]] = []

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.requests.append((list(messages), info))
        turn = self.turns[min(len(self.requests), len(self.turns)) - 1]
        return turn(messages, info) if callable(turn) else turn

    def tool_returns_seen_last(self) -> list[ToolReturnPart]:
        """The tool results in the last request the agent sent the model."""
        messages, _ = self.requests[-1]
        last_request = messages[-1]
        assert isinstance(last_request, ModelRequest)
        return [part for part in last_request.parts if isinstance(part, ToolReturnPart)]


def calls(*tool_calls: tuple[str, dict]) -> ModelResponse:
    """One model turn with these tool calls (several run in parallel)."""
    return ModelResponse(parts=[ToolCallPart(name, arguments) for name, arguments in tool_calls])


def final_answer(notes: str = "", kept: list[dict] | None = None, dropped: list[dict] | None = None) -> ModelResponse:
    arguments = {"kept": kept or [], "dropped": dropped or [], "notes": notes}
    return ModelResponse(parts=[ToolCallPart(OUTPUT_TOOL_NAME, arguments)])
