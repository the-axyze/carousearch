"""A scripted worker model for the judge tools: answers each batch from the listing ids it finds in the prompt.
Shared by the local-tool tests and the end-to-end agent tests; never reaches a provider."""

import re
from collections.abc import Callable

from pydantic_ai import models
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo

models.ALLOW_MODEL_REQUESTS = False

ListingAnswer = Callable[[str, str], tuple[str, str]]  # (listing id, its prompt block) -> (verdict, reason)
PhotoAnswer = Callable[[str], tuple[int, str]]  # listing id -> (score, reason)
FieldAnswer = Callable[[str, str, str], object]  # (listing id, its prompt block, field name) -> value or None


def always_yes(listing_id: str, block: str) -> tuple[str, str]:
    return "yes", f"listing {listing_id} fits"


def always_three(listing_id: str) -> tuple[int, str]:
    return 3, "an ordinary photo"


def never_stated(listing_id: str, block: str, field_name: str) -> object:
    return None


class ScriptedWorker:
    """A FunctionModel function for the judge agents and the extraction agent; `skip` ids are left out of the
    answer."""

    def __init__(
        self,
        listing_answer: ListingAnswer = always_yes,
        photo_answer: PhotoAnswer = always_three,
        skip: frozenset[str] = frozenset(),
        field_answer: FieldAnswer = never_stated,
    ) -> None:
        self.listing_answer = listing_answer
        self.photo_answer = photo_answer
        self.field_answer = field_answer
        self.skip = skip
        self.listing_batches: list[list[str]] = []
        self.photo_batches: list[list[str]] = []
        self.extraction_batches: list[tuple[list[str], list[str]]] = []  # (listing ids, field names) per call

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        output_tool = info.output_tools[0].name
        content = user_content(messages)
        if output_tool == "verdicts":
            return ModelResponse(parts=[ToolCallPart(output_tool, {"verdicts": self.verdicts(content[0])})])
        if output_tool == "extracted":
            return ModelResponse(parts=[ToolCallPart(output_tool, {"rows": self.extracted_rows(content[0])})])
        return ModelResponse(parts=[ToolCallPart(output_tool, {"scores": self.scores(content)})])

    def extracted_rows(self, prompt: str) -> list[dict]:
        field_names = re.findall(r"^- (\w+) \(", prompt, re.MULTILINE)
        blocks = dict(re.findall(r'<listing id="(\d+)">(.*?)</listing>', prompt, re.DOTALL))
        self.extraction_batches.append((list(blocks), field_names))
        return [
            {"listing_id": listing_id, **{name: self.field_answer(listing_id, block, name) for name in field_names}}
            for listing_id, block in blocks.items()
            if listing_id not in self.skip
        ]

    def verdicts(self, prompt: str) -> list[dict]:
        blocks = dict(re.findall(r'<listing id="(\d+)">(.*?)</listing>', prompt, re.DOTALL))
        self.listing_batches.append(list(blocks))
        answers = []
        for listing_id, block in blocks.items():
            if listing_id in self.skip:
                continue
            verdict, reason = self.listing_answer(listing_id, block)
            answers.append({"listing_id": listing_id, "verdict": verdict, "reason": reason})
        return answers

    def scores(self, content: list) -> list[dict]:
        listing_ids = [match.group(1) for part in content if isinstance(part, str)
                       if (match := re.fullmatch(r"Listing (\d+):", part))]
        self.photo_batches.append(listing_ids)
        answers = []
        for listing_id in listing_ids:
            if listing_id in self.skip:
                continue
            score, reason = self.photo_answer(listing_id)
            answers.append({"listing_id": listing_id, "score": score, "reason": reason})
        return answers


def user_content(messages: list[ModelMessage]) -> list:
    first_request = messages[0]
    assert isinstance(first_request, ModelRequest)
    part = next(part for part in first_request.parts if isinstance(part, UserPromptPart))
    return [part.content] if isinstance(part.content, str) else list(part.content)
