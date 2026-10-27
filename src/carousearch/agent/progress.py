"""Progress of a run while it is in flight: one event when a tool call starts and one when it finishes.

The run's tool hook (toolsets.ToolCallGate) builds these events and hands them to an injected ProgressObserver,
for example the web app's prompt mode. A finished search_listings event carries the listings Carousell returned,
rebuilt from the tool result, so a caller can show them as cards from Carousell's data, not the model's text.

Pure: no clock, no network. The gate measures durations and passes them in.
"""

import logging
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import ValidationError

from carousearch.agent.budget import ToolBudget
from carousearch.agent.result import ToolCallStatus
from carousearch.agent.trace import summarise_arguments
from carousearch.filters.schema import Listing
from carousearch.mcp_server import SEARCH_TOOL

logger = logging.getLogger(__name__)

ToolCallPhase = Literal["started", "finished"]


@dataclass(frozen=True)
class BudgetUsed:
    """How much of the run's budget is used at the moment of an event."""

    tool_calls_used: int
    max_tool_calls: int
    photos_used: int
    max_photos: int
    listings_used: int
    max_listings: int


@dataclass(frozen=True)
class ToolCallEvent:
    """One step of a tool call. `status` is None while the call has only started; a finished event has the final
    status ("ok", "failed" or "refused"). A refused call has a finished event only, with no started event."""

    phase: ToolCallPhase
    tool_name: str
    arguments: str  # a short summary of the arguments it was made with (as asked for when refused)
    status: ToolCallStatus | None
    duration_seconds: float  # 0.0 when started or refused
    budget: BudgetUsed
    listings: tuple[Listing, ...] = ()  # what a finished, ok search_listings call returned; empty otherwise


class ProgressObserver(Protocol):
    """Receives each event synchronously on the run's event loop, so it must be quick and must not block."""

    def __call__(self, event: ToolCallEvent) -> None: ...


def budget_used(budget: ToolBudget) -> BudgetUsed:
    return BudgetUsed(
        tool_calls_used=budget.tool_calls,
        max_tool_calls=budget.budget.max_tool_calls,
        photos_used=budget.photos,
        max_photos=budget.budget.max_photos,
        listings_used=budget.listings,
        max_listings=budget.budget.max_listings,
    )


def started_event(tool_name: str, arguments: dict[str, Any], budget: ToolBudget) -> ToolCallEvent:
    return ToolCallEvent(
        phase="started",
        tool_name=tool_name,
        arguments=summarise_arguments(arguments),
        status=None,
        duration_seconds=0.0,
        budget=budget_used(budget),
    )


def finished_event(
    tool_name: str,
    arguments: dict[str, Any],
    status: ToolCallStatus,
    duration_seconds: float,
    budget: ToolBudget,
    result: Any = None,
) -> ToolCallEvent:
    """`result` is what the tool returned (None when it failed or was refused)."""
    listings = listings_from_search_result(result) if tool_name == SEARCH_TOOL and status == "ok" else []
    return ToolCallEvent(
        phase="finished",
        tool_name=tool_name,
        arguments=summarise_arguments(arguments),
        status=status,
        duration_seconds=duration_seconds,
        budget=budget_used(budget),
        listings=tuple(listings),
    )


def listings_from_search_result(result: Any) -> list[Listing]:
    """The Listings in a search_listings tool result. Pydantic AI hands the tool hook the tool's structured
    content, the dict mcp_server.search_response built: {"listings": [...], "total": ..., "next_page": ...}.
    Anything else gives []; a listing that does not validate is skipped with a warning."""
    if not isinstance(result, dict) or not isinstance(result.get("listings"), list):
        return []
    listings = []
    for listing_data in result["listings"]:
        try:
            listings.append(Listing.model_validate(listing_data))
        except ValidationError as error:
            listing_id = listing_data.get("id") if isinstance(listing_data, dict) else None
            logger.warning("Skipping a search result listing (id %s) that did not validate: %s", listing_id, error)
    return listings
