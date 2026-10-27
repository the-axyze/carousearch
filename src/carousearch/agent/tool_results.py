"""What the planner model is sent of a tool result: a shorter version of what the tool returned. Pure.

The run keeps every full result in listings.db (agent/listings_db.py), so the model can be sent less:
- search_listings: only the `fields` the model asked for (mcp_server.project_search_response). The gate calls the
  MCP tool without `fields`, so listings.db and the progress observer get whole listings;
- listing_details: the description cut to DESCRIPTION_PREVIEW_CHARACTERS, with a pointer to the full text.
Every other result goes through unchanged.
"""

from collections.abc import Mapping
from typing import Any

from carousearch.mcp_server import DETAILS_TOOL, SEARCH_TOOL, project_search_response

DESCRIPTION_PREVIEW_CHARACTERS = 300
DESCRIPTION_CUT_NOTE = " [cut: the full description is in listings.db, column description]"


def arguments_for_server(tool_name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """The arguments the MCP server is called with: a search's `fields` is applied here instead."""
    if tool_name == SEARCH_TOOL:
        return {name: value for name, value in arguments.items() if name != "fields"}
    return dict(arguments)


def result_for_model(tool_name: str, arguments: Mapping[str, Any], result: Any) -> Any:
    if tool_name == SEARCH_TOOL and isinstance(result, dict) and isinstance(result.get("listings"), list):
        return project_search_response(result, arguments.get("fields"))
    if tool_name == DETAILS_TOOL and isinstance(result, dict) and isinstance(result.get("description"), str):
        return {**result, "description": preview_description(result["description"])}
    return result


def preview_description(description: str, max_characters: int = DESCRIPTION_PREVIEW_CHARACTERS) -> str:
    if len(description) <= max_characters:
        return description
    return description[:max_characters] + DESCRIPTION_CUT_NOTE
