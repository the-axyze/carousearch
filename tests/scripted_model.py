"""A scripted model for agent tests: a FunctionModel function that plays its turns in order, plus helpers that build
tool-call turns, the plan and the final answer. PlanFirst answers the planning phase with a fixed plan and passes the
execution phase to a script, for tests about execution. Shared by the agent, CLI and web tests; never reaches a
provider."""

from collections.abc import Callable

from pydantic_ai import models
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo

from carousearch.agent.plan import SearchPlan
from carousearch.agent.planning import PLAN_OUTPUT_TOOL_NAME
from carousearch.agent.run_prompt import OUTPUT_TOOL_NAME

from plan_examples import EXAMPLE_PLAN

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


def plan_answer(plan: SearchPlan = EXAMPLE_PLAN) -> ModelResponse:
    """The planning phase's final turn: the plan as the search_plan output tool's arguments."""
    return ModelResponse(parts=[ToolCallPart(PLAN_OUTPUT_TOOL_NAME, plan.model_dump(mode="json"))])


def is_planning(info: AgentInfo) -> bool:
    return [tool.name for tool in info.output_tools] == [PLAN_OUTPUT_TOOL_NAME]


class PlanFirst:
    """A FunctionModel function for both phases: planning requests get `plan` at once (kept in planning_requests),
    every other request goes to `script`, so the script's turns and request counts are the execution phase's."""

    def __init__(
        self, script: Callable[[list[ModelMessage], AgentInfo], ModelResponse], plan: SearchPlan = EXAMPLE_PLAN
    ) -> None:
        self.script = script
        self.plan = plan
        self.planning_requests: list[tuple[list[ModelMessage], AgentInfo]] = []

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if is_planning(info):
            self.planning_requests.append((list(messages), info))
            return plan_answer(self.plan)
        return self.script(messages, info)
