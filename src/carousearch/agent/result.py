"""The agent's answer. The model fills ModelAnswer (Pydantic AI structured output); AgentAnswer adds the tool trace
taken from the run and the notes about budgets that the run, not the model, knows for certain."""

from typing import Literal

from pydantic import BaseModel, Field


class KeptListing(BaseModel):
    listing_id: str
    title: str
    price: float | None = Field(description="The price in SGD; null when the listing has no plain price.")
    url: str = Field(description="The listing's listing_url.")
    seller: str | None = Field(description="The seller_username.")
    reason: str = Field(description="One line: why it fits the request, with the photo verdict if a photo was judged.")


class DroppedListing(BaseModel):
    listing_id: str
    title: str
    reason: str = Field(description="Why it was excluded, in a few words.")


class ModelAnswer(BaseModel):
    """The final answer: the listings kept, every listing dropped with its reason, and notes."""

    kept: list[KeptListing]
    dropped: list[DroppedListing]
    notes: str = Field(description="Limits that stopped you, assumptions, things to check. Empty if none.")


# "refused": over a budget, never sent to Carousell. "not run": rejected before the call (e.g. malformed arguments).
ToolCallStatus = Literal["ok", "failed", "refused", "not run"]


class ToolCall(BaseModel):
    """One tool call of the run, for display."""

    tool_name: str
    arguments: str  # a short summary, e.g. 'query="nintendo switch" price_max=200 limit=48'
    duration_seconds: float
    status: ToolCallStatus


class AgentAnswer(ModelAnswer):
    tool_trace: list[ToolCall]


def build_agent_answer(model_answer: ModelAnswer, tool_trace: list[ToolCall], budget_notes: list[str]) -> AgentAnswer:
    """The model's answer with the run's trace, and the budget notes appended to the model's own notes."""
    notes = "\n".join(note for note in [model_answer.notes.strip(), *budget_notes] if note)
    return AgentAnswer(kept=model_answer.kept, dropped=model_answer.dropped, notes=notes, tool_trace=tool_trace)
