"""The tool trace of a run: which tools the model called, in order, with a short summary of the arguments.

The calls and their order come from the run's message history. How long each took, whether it ran and the
arguments it was finally made with (the budget may lower a search's limit) come from the records the tool hook
keeps by tool-call id, because the history only stamps when a tool returned.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart

from carousearch.agent.result import ToolCall, ToolCallStatus

MAX_ARGUMENTS_SUMMARY_LENGTH = 100


@dataclass(frozen=True)
class CallRecord:
    duration_seconds: float
    status: ToolCallStatus
    arguments: dict[str, Any]  # as sent to the server, or as asked for when refused


def build_tool_trace(
    messages: Sequence[ModelMessage], records: Mapping[str, CallRecord], output_tool_name: str
) -> list[ToolCall]:
    """Every tool call in the model's responses except the final-answer tool, in the order the model made them."""
    return [
        trace_entry(part, records.get(part.tool_call_id))
        for message in messages
        if isinstance(message, ModelResponse)
        for part in message.parts
        if isinstance(part, ToolCallPart) and part.tool_name != output_tool_name
    ]


def trace_entry(part: ToolCallPart, record: CallRecord | None) -> ToolCall:
    if record is None:  # never reached the hook, e.g. arguments that were not valid JSON
        return ToolCall(
            tool_name=part.tool_name,
            arguments=summarise_arguments(part.args_as_dict()),
            duration_seconds=0.0,
            status="not run",
        )
    return ToolCall(
        tool_name=part.tool_name,
        arguments=summarise_arguments(record.arguments),
        duration_seconds=record.duration_seconds,
        status=record.status,
    )


def summarise_arguments(arguments: Mapping[str, Any]) -> str:
    """{"request": {"query": "nintendo switch", "price_max": 200}, "limit": 48}
    -> 'query="nintendo switch" price_max=200 limit=48'. Empty values are left out; long summaries are cut."""
    pairs = [f"{name}={format_argument(value)}" for name, value in flatten(arguments).items() if value not in (None, "", [], {})]
    summary = " ".join(pairs)
    if len(summary) > MAX_ARGUMENTS_SUMMARY_LENGTH:
        return summary[: MAX_ARGUMENTS_SUMMARY_LENGTH - 1] + "…"
    return summary


def flatten(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Lifts the fields of a nested object (search_listings' `request`) to the top level."""
    flat: dict[str, Any] = {}
    for name, value in arguments.items():
        if isinstance(value, dict) and name == "request":
            flat.update(value)
        else:
            flat[name] = value
    return flat


def format_argument(value: Any) -> str:
    if isinstance(value, str):
        if value.startswith("https://"):
            return value.rstrip("/").rsplit("/", 1)[-1]  # a photo URL: its file name tells the photos apart
        return json.dumps(value, ensure_ascii=False) if " " in value else value
    if isinstance(value, list):
        return ",".join(str(item) for item in value)
    return json.dumps(value, ensure_ascii=False)
