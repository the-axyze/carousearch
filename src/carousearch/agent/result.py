"""The agent's answer. The model fills ModelAnswer (Pydantic AI structured output); AgentAnswer adds the tool trace
taken from the run, the notes about budgets that the run, not the model, knows for certain, and the token usage."""

from collections import Counter
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, Field

from carousearch.agent.cost import ModelUsage


class KeptListing(BaseModel):
    listing_id: str
    title: str
    price: float | None = Field(description="The price in SGD; null when the listing has no plain price.")
    url: str = Field(description="The listing's listing_url.")
    seller: str | None = Field(description="The seller_username.")
    reason: str = Field(
        description="One line: why it fits, with the numbers you computed (e.g. the benchmark and the percent below "
        "it) and the photo verdict if a photo was judged."
    )


class DroppedListing(BaseModel):
    """A shortlisted listing that failed a later check of a criterion the shopper stated."""

    listing_id: str
    title: str
    reason: str = Field(description="The stated criterion it failed, with the number, in a few words.")


class ModelAnswer(BaseModel):
    """The final answer: the listings kept, the listings that failed a stated criterion, and notes."""

    kept: list[KeptListing]
    dropped: list[DroppedListing] = Field(
        description="Only shortlisted listings that then failed a check of a stated criterion (a COE, a photo, a "
        "detail). Listings filtered out by price, title or post type are not dropped: count them in not_shortlisted."
    )
    not_shortlisted: int = Field(
        default=0,
        description="How many other listings you saw that you neither kept nor dropped; they are not listed.",
    )
    notes: str = Field(description="Assumptions, limits that stopped you, things to check. Empty if none.")


# "refused": over a budget, never sent to Carousell. "not run": rejected before the call (e.g. malformed arguments).
ToolCallStatus = Literal["ok", "failed", "refused", "not run"]


class ToolCall(BaseModel):
    """One tool call of the run, for display."""

    tool_name: str
    arguments: str  # a short summary, e.g. 'query="nintendo switch" price_max=200 limit=48'
    duration_seconds: float
    status: ToolCallStatus


def tool_call_counts(trace: Sequence[ToolCall]) -> list[tuple[str, int]]:
    """How many calls each tool had, most first: [("search_listings", 3), ("run_python", 2)]."""
    return Counter(call.tool_name for call in trace).most_common()


class AgentAnswer(ModelAnswer):
    tool_trace: list[ToolCall]
    usage: list[ModelUsage] = Field(default_factory=list)  # per model, with an estimated cost


def build_agent_answer(
    model_answer: ModelAnswer,
    tool_trace: list[ToolCall],
    budget_notes: list[str],
    usage: list[ModelUsage] | None = None,
) -> AgentAnswer:
    """The model's answer with the run's trace and usage, and the budget notes appended to the model's own notes."""
    notes = "\n".join(note for note in [model_answer.notes.strip(), *budget_notes] if note)
    return AgentAnswer(
        kept=model_answer.kept,
        dropped=model_answer.dropped,
        not_shortlisted=model_answer.not_shortlisted,
        notes=notes,
        tool_trace=tool_trace,
        usage=usage or [],
    )
