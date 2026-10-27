"""The agent's local tools: run_python, judge_listings, judge_photos and photo_metrics.

They are Pydantic AI function tools, not MCP tools, because they need this process's state for one run: the run
directory (listings.db, downloaded photos, snippets), the worker model and the run's budget. Every call still goes
through the run's ToolCallGate (run_prompt.GatedToolset), so it is counted, timed and traced like the MCP tools,
and the Carousell requests a tool makes inside (details, photos) are counted through ToolBudget.grant_*.

I/O: the sandbox subprocess, Carousell and the shared database (through McpServices, under its lock, in worker
threads), and the worker model. The pure parts are in sandbox.py, judging.py, photo_metrics.py, listings_db.py and
run_photos.py.
"""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field
from pydantic_ai.exceptions import AgentRunError, ToolFailed
from pydantic_ai.models import Model
from pydantic_ai.toolsets import FunctionToolset

from carousearch.agent.budget import ToolBudget
from carousearch.agent.judge_cache import JudgementCache, JudgementKind
from carousearch.agent.judging import (
    LISTINGS_PER_MODEL_CALL,
    PHOTOS_PER_MODEL_CALL,
    ListingText,
    PhotoScore,
    batched,
    complete_scores,
    complete_verdicts,
    normalise_criterion,
)
from carousearch.agent.listings_db import RunListingsStore
from carousearch.agent.photo_metrics import BLURRY_BELOW, DARK_BELOW, WASHED_OUT_ABOVE, measure_photo
from carousearch.agent.run_photos import PhotoSource, RunPhotoStore, main_photo_url
from carousearch.agent.sandbox import (
    DEFAULT_SANDBOX_LIMITS,
    SandboxLimits,
    available_optional_libraries,
    format_sandbox_output,
    run_python_in_sandbox,
)
from carousearch.agent.worker import WorkerJudge
from carousearch.carousell.errors import CarousellBlockedError
from carousearch.filters.schema import ListingDetail
from carousearch.mcp_server import McpServices, carousell_call, detail_response, load_listing_detail
from carousearch.photos import Photo, downsize_to_longest_side

logger = logging.getLogger(__name__)

MAX_LISTINGS_PER_JUDGE_CALL = 30
MAX_LISTINGS_PER_PHOTO_CALL = 10
JUDGE_PHOTO_SIDE = 512  # the longer side of a photo sent to the worker model

ListingIds = Annotated[
    list[Annotated[str, Field(pattern=r"^\d+$")]],
    Field(min_length=1, description="Listing ids from search_listings results."),
]
Criterion = Annotated[
    str, Field(min_length=3, max_length=300, description="One plain-language criterion, the same wording each time.")
]


@dataclass(frozen=True)
class LocalToolServices:
    """What the local tools need beyond McpServices; the caller (CLI, web app, tests) builds it per run."""

    run_directory: Path  # this run's own folder, e.g. data/runs/20261027-101500-ab12; created if missing
    judgement_cache: JudgementCache
    worker_model: Model | None = None  # replaces the model AGENT_WORKER_MODEL names (tests pass a scripted one)
    sandbox_limits: SandboxLimits = DEFAULT_SANDBOX_LIMITS
    network_prefix: tuple[str, ...] | None = None  # the sandbox's network namespace command; None detects one


@dataclass
class RunTools:
    """One run's local tools and the state they share."""

    services: McpServices
    local: LocalToolServices
    budget: ToolBudget
    listings: RunListingsStore
    worker: WorkerJudge
    photos: RunPhotoStore

    # --- run_python ---------------------------------------------------------------------------------------------

    async def run_python(self, code: str) -> str:
        limits = self.local.sandbox_limits
        result = await asyncio.to_thread(
            run_python_in_sandbox, code, self.local.run_directory, limits, self.local.network_prefix
        )
        return format_sandbox_output(result, limits.max_output_characters)

    # --- judge_listings -----------------------------------------------------------------------------------------

    async def judge_listings(self, listing_ids: Sequence[str], criterion: str) -> list[dict[str, Any]]:
        listing_ids = unique(listing_ids)[:MAX_LISTINGS_PER_JUDGE_CALL]
        criterion_key = normalise_criterion(criterion)
        answers = await self.load_cached_judgements("listing", listing_ids, criterion_key)
        to_judge = [listing_id for listing_id in listing_ids if listing_id not in answers]
        problems = await self.ensure_details(to_judge)
        rows = self.listings.listings(to_judge)
        texts = [listing_text(rows[listing_id]) for listing_id in to_judge if listing_id in rows]
        for listing_id in to_judge:
            if listing_id not in rows:
                answers[listing_id] = {"verdict": "unsure", "reason": problems.get(listing_id, "unknown listing id")}
        for batch in batched(texts, LISTINGS_PER_MODEL_CALL):
            answers.update(await self.judge_listing_batch(criterion, criterion_key, batch))
        return [{"id": listing_id, **answers[listing_id]} for listing_id in listing_ids]

    async def judge_listing_batch(
        self, criterion: str, criterion_key: str, batch: list[ListingText]
    ) -> dict[str, dict[str, Any]]:
        batch_ids = [text.listing_id for text in batch]
        try:
            verdicts = complete_verdicts(batch_ids, await self.worker.judge_listings(criterion, batch))
        except AgentRunError as error:
            logger.warning("judge_listings: the worker model failed: %s: %s", type(error).__name__, error)
            return {listing_id: {"verdict": "unsure", "reason": "the worker model failed"} for listing_id in batch_ids}
        answers = {verdict.listing_id: {"verdict": verdict.verdict, "reason": verdict.reason} for verdict in verdicts}
        # A verdict from the title alone could change once the description is known, so only those with one are kept.
        judged_with_description = [text.listing_id for text in batch if text.description]
        await self.save_judgements("listing", criterion_key, {i: answers[i] for i in judged_with_description})
        return answers

    # --- judge_photos and photo_metrics -------------------------------------------------------------------------

    async def judge_photos(self, listing_ids: Sequence[str], criterion: str) -> list[dict[str, Any]]:
        listing_ids = unique(listing_ids)[:MAX_LISTINGS_PER_PHOTO_CALL]
        criterion_key = normalise_criterion(criterion)
        answers = await self.load_cached_judgements("photo", listing_ids, criterion_key)
        to_judge = [listing_id for listing_id in listing_ids if listing_id not in answers]
        photos, problems = await self.main_photos(to_judge)
        for listing_id, problem in problems.items():
            answers[listing_id] = {"score_1_to_5": None, "reason": problem}
        measured = {listing_id: photo_summary(data, source) for listing_id, (data, source) in photos.items()}
        small_photos = [
            (listing_id, downsize_to_longest_side(data, JUDGE_PHOTO_SIDE)) for listing_id, (data, _) in photos.items()
        ]
        for batch in batched(small_photos, PHOTOS_PER_MODEL_CALL):
            answers.update(await self.judge_photo_batch(criterion, criterion_key, batch, measured))
        return [{"id": listing_id, **answers[listing_id]} for listing_id in listing_ids]

    async def judge_photo_batch(
        self,
        criterion: str,
        criterion_key: str,
        batch: list[tuple[str, Photo]],
        measured: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        batch_ids = [listing_id for listing_id, _ in batch]
        try:
            scores = complete_scores(batch_ids, await self.worker.judge_photos(criterion, batch))
        except AgentRunError as error:
            logger.warning("judge_photos: the worker model failed: %s: %s", type(error).__name__, error)
            scores = dict.fromkeys(batch_ids)
        answers = {listing_id: photo_answer(scores[listing_id], measured[listing_id]) for listing_id in batch_ids}
        scored = {listing_id: answer for listing_id, answer in answers.items() if scores[listing_id] is not None}
        await self.save_judgements("photo", criterion_key, scored)
        return answers

    async def photo_metrics(self, listing_ids: Sequence[str]) -> list[dict[str, Any]]:
        listing_ids = unique(listing_ids)[:MAX_LISTINGS_PER_PHOTO_CALL]
        photos, problems = await self.main_photos(listing_ids)
        results = []
        for listing_id in listing_ids:
            summary = photo_summary(*photos[listing_id]) if listing_id in photos else {"problem": problems[listing_id]}
            results.append({"id": listing_id, **summary})
        return results

    # --- shared I/O ---------------------------------------------------------------------------------------------

    async def ensure_details(self, listing_ids: Sequence[str]) -> dict[str, str]:
        """Put each listing's details into listings.db unless already there: from the detail cache when fresh,
        otherwise from Carousell within the budget. Returns what went wrong, by listing id."""
        known = self.listings.listings(listing_ids)
        missing = [listing_id for listing_id in listing_ids if not known.get(listing_id, {}).get("has_details")]
        problems: dict[str, str] = {}
        to_fetch = []
        for listing_id in missing:
            cached = await asyncio.to_thread(find_cached_detail, self.services, listing_id)
            if cached is not None:
                self.listings.record_detail(detail_response(cached))
            else:
                to_fetch.append(listing_id)
        granted = self.budget.grant_detail_fetches(len(to_fetch))
        for listing_id in to_fetch[granted:]:
            problems[listing_id] = "no description: the tool-call budget is used up"
        for listing_id in to_fetch[:granted]:
            try:
                detail = await asyncio.to_thread(load_listing_detail, self.services, listing_id)
            except ToolError as error:
                raise_if_blocked(error)
                problems[listing_id] = f"no description: {error}"
                continue
            self.listings.record_detail(detail_response(detail))
        return problems

    async def main_photos(
        self, listing_ids: Sequence[str]
    ) -> tuple[dict[str, tuple[bytes, PhotoSource]], dict[str, str]]:
        """Each listing's main photo (bytes and source) from this run's photo store or Carousell, within the
        photo budget; and what went wrong for the others."""
        rows = self.listings.listings(listing_ids)
        photos: dict[str, tuple[bytes, PhotoSource]] = {}
        problems: dict[str, str] = {}
        to_download: list[tuple[str, str, PhotoSource]] = []
        for listing_id in listing_ids:
            photo_url = main_photo_url(rows[listing_id]) if listing_id in rows else None
            if photo_url is None:
                problems[listing_id] = "no photo URL: search for the listing first"
                continue
            url, source = photo_url
            stored = self.photos.load(listing_id, source)
            if stored is not None:
                photos[listing_id] = (stored, source)
            else:
                to_download.append((listing_id, url, source))
        granted = self.budget.grant_photos(len(to_download))
        for listing_id, _, _ in to_download[granted:]:
            problems[listing_id] = "not checked: the photo budget is used up"
        for listing_id, url, source in to_download[:granted]:
            try:
                data = await asyncio.to_thread(download_photo, self.services, url)
            except ToolError as error:
                raise_if_blocked(error)
                problems[listing_id] = f"photo download failed: {error}"
                continue
            self.photos.save(listing_id, source, data)
            photos[listing_id] = (data, source)
        return photos, problems

    async def load_cached_judgements(
        self, kind: JudgementKind, listing_ids: Sequence[str], criterion_key: str
    ) -> dict[str, dict[str, Any]]:
        def load_all() -> dict[str, dict[str, Any]]:
            with self.services.lock:
                found = {
                    listing_id: self.local.judgement_cache.load(kind, listing_id, criterion_key, self.worker.model_name)
                    for listing_id in listing_ids
                }
            return {listing_id: answer for listing_id, answer in found.items() if answer is not None}

        return await asyncio.to_thread(load_all)

    async def save_judgements(self, kind: JudgementKind, criterion_key: str, answers: dict[str, dict[str, Any]]) -> None:
        def save_all() -> None:
            with self.services.lock:
                judged_at = self.services.clock()
                for listing_id, answer in answers.items():
                    self.local.judgement_cache.save(
                        kind, listing_id, criterion_key, self.worker.model_name, answer, judged_at
                    )

        await asyncio.to_thread(save_all)


# --- small helpers ------------------------------------------------------------------------------------------------


def unique(listing_ids: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(listing_ids))


def listing_text(row: dict[str, Any]) -> ListingText:
    return ListingText(listing_id=row["id"], title=row["title"], price=row["price"], description=row["description"])


def photo_summary(data: bytes, source: PhotoSource) -> dict[str, Any]:
    """The zero-token metrics of a downloaded photo, with which photo it was."""
    try:
        return {"photo": source, **measure_photo(data).as_dict()}
    except ValueError as error:
        return {"photo": source, "problem": f"unreadable photo: {error}"}


def photo_answer(score: PhotoScore | None, metrics: dict[str, Any]) -> dict[str, Any]:
    if score is None:
        return {"score_1_to_5": None, "reason": "the worker model gave no score", **metrics}
    return {"score_1_to_5": score.score, "reason": score.reason, **metrics}


def find_cached_detail(services: McpServices, listing_id: str) -> ListingDetail | None:
    with services.lock:
        return services.detail_cache.load_detail(listing_id, services.clock())


def download_photo(services: McpServices, url: str) -> bytes:
    with carousell_call(services):
        return services.carousell.get_photo(url)


def raise_if_blocked(error: ToolError) -> None:
    """Carousell blocking us ends the tool call with a failure the model reads (and must stop on)."""
    if isinstance(error.__cause__, CarousellBlockedError):
        raise ToolFailed(str(error)) from error


# --- the toolset (model-facing descriptions) ----------------------------------------------------------------------


def run_python_description(optional_libraries: Sequence[str]) -> str:
    libraries = ", ".join(optional_libraries) if optional_libraries else "no pandas or numpy"
    return f"""\
Run a Python snippet over this run's listings and return what it printed (stdout; stderr when it fails). Use it \
for all arithmetic, statistics (median, mean, percentiles), regexes, date maths and set logic instead of working \
numbers out in prose. The working directory holds listings.db, a SQLite database whose table `listings` has one \
row for every listing this run's tools have returned (search pages and details), and listings.json with the same \
rows. Columns: id, title, price (SGD, null when not a plain number), currency, price_display, condition, \
condition_label, like_count, seller_username, seller_id, url, thumbnail_url, posted_at (ISO time created, when \
known), bumped_at (ISO time of the latest bump, when known), category (category id), category_name, raw_json \
(the search result as JSON), has_details (1 once details were fetched), description, seller_rating, review_count, \
seller_joined_at, response_rate, image_urls (JSON list), meetup_locations (JSON list), detail_json. Python \
standard library only ({libraries}): sqlite3, json, re, statistics, math, datetime, collections. No network, no \
files outside the working directory, {DEFAULT_SANDBOX_LIMITS.wall_seconds:.0f} s per run. Print only what you \
need: the output is cut after a few thousand characters."""


JUDGE_LISTINGS_DESCRIPTION = f"""\
Ask a small, cheap model to check listings against one criterion, from each listing's title, price and \
description, for example "COE has about 2 years left (between 1.5 and 2.5 years)" or "a whole bike for sale, not \
parts or a rental". Returns [{{id, verdict, reason}}] in the order given; verdict is yes, no or unsure. Listings \
whose details this run has not fetched yet cost one Carousell request each, counted against the tool-call budget. \
Answers are cached: asking the same criterion about the same listing again is free. Use this instead of reading \
many descriptions yourself. At most {MAX_LISTINGS_PER_JUDGE_CALL} listings per call."""

JUDGE_PHOTOS_DESCRIPTION = f"""\
Ask a small model to score each listing's main photo against one criterion, from 1 (does not match at all) to 5 \
(clearly matches), for example "blurry, dark, cluttered or otherwise low-effort photo". Uses the full-size first \
photo when the listing's details were fetched, otherwise the search card's thumbnail. Returns [{{id, \
score_1_to_5, reason, photo, width, height, sharpness, brightness}}]. Each photo not yet downloaded in this run \
counts against the photo budget and the tool-call budget; answers are cached. Use it only on your shortlist, at \
most {MAX_LISTINGS_PER_PHOTO_CALL} listings per call."""

PHOTO_METRICS_DESCRIPTION = f"""\
Measure each listing's main photo with no model at all: width and height in pixels, sharpness (the Laplacian \
variance at 512 px; under about {BLURRY_BELOW:.0f} looks blurry) and brightness (0 to 255; under about \
{DARK_BELOW:.0f} is dark, over about {WASHED_OUT_ABOVE:.0f} washed out). These thresholds are rough guides. Each \
photo not yet downloaded in this run counts against the photo budget and the tool-call budget. At most \
{MAX_LISTINGS_PER_PHOTO_CALL} listings per call."""


def build_local_toolset(tools: RunTools) -> FunctionToolset[Any]:
    toolset: FunctionToolset[Any] = FunctionToolset()

    @toolset.tool_plain(description=run_python_description(available_optional_libraries()))
    async def run_python(code: Annotated[str, Field(description="Python source; print what you need.")]) -> str:
        return await tools.run_python(code)

    @toolset.tool_plain(description=JUDGE_LISTINGS_DESCRIPTION)
    async def judge_listings(listing_ids: ListingIds, criterion: Criterion) -> list[dict[str, Any]]:
        return await tools.judge_listings(listing_ids, criterion)

    @toolset.tool_plain(description=JUDGE_PHOTOS_DESCRIPTION)
    async def judge_photos(listing_ids: ListingIds, criterion: Criterion) -> list[dict[str, Any]]:
        return await tools.judge_photos(listing_ids, criterion)

    @toolset.tool_plain(description=PHOTO_METRICS_DESCRIPTION)
    async def photo_metrics(listing_ids: ListingIds) -> list[dict[str, Any]]:
        return await tools.photo_metrics(listing_ids)

    return toolset
