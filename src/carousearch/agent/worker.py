"""The worker model's jobs for the judge tools and extract_fields: one structured Pydantic AI call per batch of
listings or photos.

I/O: the model provider, through the injected Pydantic AI Model (tests pass a scripted FunctionModel). Every call's
token usage goes into the run's UsageLedger under the worker's model name. Prompts and answer shapes are in
agent/judging.py and agent/extraction.py.
"""

from collections.abc import Sequence
from typing import Any

from pydantic_ai import Agent, ToolOutput, UsageLimits
from pydantic_ai.capabilities import Caching
from pydantic_ai.models import Model

from carousearch.agent.cost import UsageLedger
from carousearch.agent.extraction import (
    EXTRACTION_INSTRUCTIONS,
    FieldSpec,
    build_extraction_prompt,
    extraction_answer_model,
)
from carousearch.agent.judging import (
    LISTING_JUDGE_INSTRUCTIONS,
    PHOTO_JUDGE_INSTRUCTIONS,
    ListingText,
    ListingVerdict,
    ListingVerdicts,
    PhotoScore,
    PhotoScores,
    build_listing_prompt,
    build_photo_prompt,
)
from carousearch.photos import Photo

# One answer per call; a retry or two covers an answer that fails validation.
WORKER_REQUEST_LIMIT = 3
WORKER_RETRIES = 1


class WorkerJudge:
    def __init__(self, model: Model, model_name: str, ledger: UsageLedger) -> None:
        self.model_name = model_name  # the configured string, e.g. "openai:gpt-5.4-nano"; the cache keys on it
        self._ledger = ledger
        self._listing_agent = Agent(
            model,
            output_type=ToolOutput(ListingVerdicts, name="verdicts"),
            instructions=LISTING_JUDGE_INSTRUCTIONS,
            retries=WORKER_RETRIES,
            capabilities=[Caching()],
        )
        self._photo_agent = Agent(
            model,
            output_type=ToolOutput(PhotoScores, name="scores"),
            instructions=PHOTO_JUDGE_INSTRUCTIONS,
            retries=WORKER_RETRIES,
            capabilities=[Caching()],
        )
        # Its answer shape depends on the fields asked for, so the output type is given per call.
        self._extraction_agent = Agent(
            model, instructions=EXTRACTION_INSTRUCTIONS, retries=WORKER_RETRIES, capabilities=[Caching()]
        )

    async def judge_listings(self, criterion: str, listings: Sequence[ListingText]) -> list[ListingVerdict]:
        """The model's verdicts for one batch (it may leave some out; judging.complete_verdicts fills them)."""
        result = await self._listing_agent.run(
            build_listing_prompt(criterion, listings), usage_limits=UsageLimits(request_limit=WORKER_REQUEST_LIMIT)
        )
        self._ledger.add_usage("worker", self.model_name, result.usage, requests=result.usage.requests)
        return result.output.verdicts

    async def judge_photos(self, criterion: str, photos: Sequence[tuple[str, Photo]]) -> list[PhotoScore]:
        """The model's scores for one batch of (listing id, downsized photo)."""
        result = await self._photo_agent.run(
            build_photo_prompt(criterion, photos), usage_limits=UsageLimits(request_limit=WORKER_REQUEST_LIMIT)
        )
        self._ledger.add_usage("worker", self.model_name, result.usage, requests=result.usage.requests)
        return result.output.scores

    async def extract_fields(self, specs: Sequence[FieldSpec], listing_blocks: Sequence[str]) -> list[dict[str, Any]]:
        """The model's rows for one batch, {"listing_id": ..., <field>: value or None}; it may leave listings out."""
        answer_model = extraction_answer_model(specs)
        result = await self._extraction_agent.run(
            build_extraction_prompt(specs, listing_blocks),
            output_type=ToolOutput(answer_model, name="extracted", strict=True),
            usage_limits=UsageLimits(request_limit=WORKER_REQUEST_LIMIT),
        )
        self._ledger.add_usage("worker", self.model_name, result.usage, requests=result.usage.requests)
        return [row.model_dump() for row in result.output.rows]
