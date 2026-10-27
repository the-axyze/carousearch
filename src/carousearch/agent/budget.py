"""Counts one run's tool calls against its AgentBudget. Pure: the run's tool hook asks before every call.

Three caps: tool calls in total, photos (listing_photo calls) and listings asked for from search pages. A search
that would pass the listing cap is cut to what is left; any other call over a cap is refused. Refused calls
never reach Carousell and do not count.

The local tools (agent/local_tools.py) fetch details and photos for several listings in one call. Each such
Carousell request counts as one more tool call, and each photo also as a photo, as if the model had called
listing_details or listing_photo itself: grant_detail_fetches and grant_photos say how many of those may go ahead.
The hosted web search has its own cap, counted from the run's messages (agent/web_search.py).
"""

from typing import Any

from carousearch.agent.settings import AgentBudget
from carousearch.mcp_server import DEFAULT_SEARCH_LIMIT, PHOTO_TOOL, SEARCH_TOOL


class BudgetExhausted(Exception):
    """A call was refused; the message tells the model which cap stopped it and to finish."""


class ToolBudget:
    def __init__(self, budget: AgentBudget) -> None:
        self.budget = budget
        self.tool_calls = 0
        self.photos = 0
        self.listings = 0
        self.refused_tool_calls = 0
        self.refused_photos = 0
        self.refused_searches = 0
        self.shortened_searches = 0
        self.withheld_detail_fetches = 0  # asked for by a local tool, not granted
        self.withheld_photos = 0

    def admit(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Count the call and return the arguments to make it with (a search's limit may be lowered).
        Raises BudgetExhausted, counting nothing, when the call would pass a cap."""
        if self.tool_calls >= self.budget.max_tool_calls:
            self.refused_tool_calls += 1
            raise BudgetExhausted(
                f"Refused: the tool-call budget of {self.budget.max_tool_calls} calls is used up. "
                "Call no more tools; give your answer now and say in notes what you could not check."
            )
        if tool_name == PHOTO_TOOL and self.photos >= self.budget.max_photos:
            self.refused_photos += 1
            raise BudgetExhausted(
                f"Refused: the photo budget of {self.budget.max_photos} photos is used up. Judge no more photos; "
                "finish with what you have and say in notes which listings went unchecked."
            )
        if tool_name == SEARCH_TOOL:
            arguments = self._fit_search_to_listing_budget(arguments)
        self.tool_calls += 1
        if tool_name == PHOTO_TOOL:
            self.photos += 1
        return arguments

    def grant_detail_fetches(self, wanted: int) -> int:
        """How many of `wanted` listing-detail requests a local tool may make; each granted one is a tool call."""
        granted = max(0, min(wanted, self.budget.max_tool_calls - self.tool_calls))
        self.tool_calls += granted
        self.withheld_detail_fetches += wanted - granted
        return granted

    def grant_photos(self, wanted: int) -> int:
        """How many of `wanted` photo downloads a local tool may make; each granted one is a photo and a tool call."""
        remaining = min(self.budget.max_photos - self.photos, self.budget.max_tool_calls - self.tool_calls)
        granted = max(0, min(wanted, remaining))
        self.photos += granted
        self.tool_calls += granted
        self.withheld_photos += wanted - granted
        return granted

    def notes(self) -> list[str]:
        """One line per cap that stopped or shortened something, for the answer's notes."""
        lines = []
        if self.refused_tool_calls:
            lines.append(
                f"Tool-call budget reached: {count_of(self.budget.max_tool_calls, 'call')} used, "
                f"{self.refused_tool_calls} more refused."
            )
        if self.refused_photos or self.withheld_photos:
            lines.append(
                f"Photo budget reached: {count_of(self.photos, 'photo')} judged, "
                f"{self.refused_photos + self.withheld_photos} more refused."
            )
        if self.withheld_detail_fetches:
            lines.append(
                f"Tool-call budget reached inside a tool: {count_of(self.withheld_detail_fetches, 'listing')} "
                "left without details."
            )
        if self.refused_searches or self.shortened_searches:
            lines.append(
                f"Listing budget reached: {count_of(self.budget.max_listings, 'listing')} pulled from search pages "
                f"({count_of(self.shortened_searches, 'page')} cut short, "
                f"{count_of(self.refused_searches, 'search', 'searches')} refused)."
            )
        return lines

    def _fit_search_to_listing_budget(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Counts the page size asked for (Carousell may return fewer), lowered to what is left of the cap."""
        remaining = self.budget.max_listings - self.listings
        if remaining <= 0:
            self.refused_searches += 1
            raise BudgetExhausted(
                f"Refused: the listing budget of {self.budget.max_listings} listings from search pages is used up. "
                "Work with the listings you have, give your answer now and say so in notes."
            )
        requested = arguments.get("limit", DEFAULT_SEARCH_LIMIT)
        if not isinstance(requested, int):
            requested = DEFAULT_SEARCH_LIMIT  # a limit that is not a whole number is replaced by the tool's default
        page_size = min(requested, remaining)
        if page_size < requested:
            self.shortened_searches += 1
        self.listings += page_size
        return {**arguments, "limit": page_size}


def count_of(count: int, noun: str, plural: str | None = None) -> str:
    """count_of(1, "call") -> "1 call"; count_of(2, "search", "searches") -> "2 searches"."""
    return f"{count} {noun if count == 1 else plural or noun + 's'}"
