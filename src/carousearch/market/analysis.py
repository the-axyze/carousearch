"""Price statistics over harvested listings. Pure: rows and the current time come in, results go out.

An `AnalysisSpec` says which searches to harvest and how to slice the result; `analyse` turns the
`ListingRow`s those harvests returned into an `AnalysisResult`. The order of operations is fixed:
de-duplicate by listing id, apply the title and age filters, set aside listings without a numeric
price, find outliers on the overall price list, then compute overall and per-group statistics. The
counts in the result add up: fetched = analysed + unpriced + dropped by filters + excluded outliers.

Statistics use only the `statistics` module (decision 5 of the plan: no pandas or numpy).
"""

import math
import re
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from carousearch.filters.schema import SearchRequest, SortOrder
from carousearch.market.hashing import search_request_hash
from carousearch.market.limits import HarvestLimits
from carousearch.storage import ListingRow
from carousearch.text_render import SINGAPORE_TIME

MAX_PATTERN_LENGTH = 200
TOP_LISTING_COUNT = 5
UNKNOWN_KEY = "unknown"
ALL_KEY = "all"


class GroupBy(StrEnum):
    NONE = "none"
    CONDITION = "condition"
    WEEK_POSTED = "week_posted"  # ISO week of posted_or_bumped_at, key "2026-W43"; None -> "unknown"
    SELLER = "seller"  # seller_username or seller_id; None -> "unknown"


class TitleFilter(BaseModel):
    """Regexes over listing titles, matched case-insensitively with `re.search`."""

    model_config = ConfigDict(extra="forbid")

    include: list[str] = Field(default_factory=list)  # every one must match
    exclude: list[str] = Field(default_factory=list)  # none may match

    @field_validator("include", "exclude")
    @classmethod
    def patterns_must_compile(cls, patterns: list[str]) -> list[str]:
        # The length cap keeps LLM-written patterns (phase B) from being pathological.
        for pattern in patterns:
            if len(pattern) > MAX_PATTERN_LENGTH:
                raise ValueError(f"title pattern longer than {MAX_PATTERN_LENGTH} characters")
            try:
                re.compile(pattern, re.IGNORECASE)
            except re.error as error:
                raise ValueError(f"title pattern {pattern!r} is not a valid regex: {error}") from error
        return patterns


class AnalysisSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str  # the user's own wording
    searches: list[SearchRequest] = Field(min_length=1, max_length=3)
    limits: HarvestLimits = Field(default_factory=HarvestLimits)
    # Client-side cutoff on posted_or_bumped_at, independent of limits.max_age_days.
    max_age_days: int | None = Field(default=None, ge=1)
    title_filter: TitleFilter = Field(default_factory=TitleFilter)
    group_by: GroupBy = GroupBy.NONE
    exclude_outliers: bool = True

    @field_validator("searches")
    @classmethod
    def searches_start_at_page_one_and_are_unique(cls, searches: list[SearchRequest]) -> list[SearchRequest]:
        """Reject cursors (a harvest always starts at page one) and drop searches that would harvest the same run."""
        unique: list[SearchRequest] = []
        seen_hashes: set[str] = set()
        for search in searches:
            if search.page_cursor is not None:
                raise ValueError("searches in an analysis spec must not have a page_cursor")
            request_hash = search_request_hash(search)
            if request_hash not in seen_hashes:
                seen_hashes.add(request_hash)
                unique.append(search)
        return unique

    @model_validator(mode="after")
    def age_cutoff_needs_recent_sort(self) -> "AnalysisSpec":
        """`limits.max_age_days` is only sound when every search returns newest first (checked here, before any request)."""
        if self.limits.max_age_days is not None:
            not_recent = [search for search in self.searches if search.sort != SortOrder.RECENT]
            if not_recent:
                raise ValueError("limits.max_age_days needs every search to use sort=recent")
        return self


@dataclass(frozen=True)
class PriceStats:
    count: int
    minimum: float
    p25: float
    median: float
    p75: float
    maximum: float
    mean: float


@dataclass(frozen=True)
class GroupResult:
    key: str
    stats: PriceStats
    listing_ids: list[str]  # sorted by price ascending


@dataclass(frozen=True)
class OutlierListing:
    listing_id: str
    title: str
    price: float
    side: Literal["low", "high"]
    bound: float  # the IQR bound the price fell outside of


@dataclass(frozen=True)
class AnalysisResult:
    spec: AnalysisSpec
    run_ids: list[int]
    fetched_count: int  # unique listings across the runs
    analysed_count: int  # after title/age filters, with a numeric price, outliers excluded if configured
    unpriced_count: int
    dropped_by_filters_count: int
    overall: PriceStats | None  # None when analysed_count == 0
    groups: list[GroupResult]  # empty when group_by == NONE; sorted by key
    outliers: list[OutlierListing]  # sorted by price
    cheapest: list[ListingRow]  # up to 5 analysed listings, price ascending
    newest: list[ListingRow]  # up to 5 analysed listings, posted_or_bumped_at descending (None last)
    computed_at: datetime


def analyse(spec: AnalysisSpec, rows: Sequence[ListingRow], run_ids: Sequence[int], now: datetime) -> AnalysisResult:
    """Compute the statistics for one analysis.

    With `spec.max_age_days` set, a listing without a timestamp is kept: its age is unknown, not old.
    Outliers are always reported; `exclude_outliers` only decides whether they leave the statistics.
    """
    unique_rows = _unique_by_id(rows)

    filtered = apply_title_filter(spec.title_filter, unique_rows)
    if spec.max_age_days is not None:
        cutoff = now - timedelta(days=spec.max_age_days)
        filtered = [row for row in filtered if row.posted_or_bumped_at is None or row.posted_or_bumped_at >= cutoff]
    dropped_by_filters_count = len(unique_rows) - len(filtered)

    priced = [row for row in filtered if row.price is not None]
    unpriced_count = len(filtered) - len(priced)

    low_bound, high_bound = iqr_bounds([_price(row) for row in priced])
    outliers: list[OutlierListing] = []
    outlier_ids: set[str] = set()
    for row in priced:
        price = _price(row)
        if price < low_bound or price > high_bound:
            side: Literal["low", "high"] = "low" if price < low_bound else "high"
            bound = low_bound if side == "low" else high_bound
            outliers.append(OutlierListing(row.listing_id, row.title, price, side, bound))
            outlier_ids.add(row.listing_id)
    outliers.sort(key=lambda outlier: outlier.price)

    analysed = [row for row in priced if row.listing_id not in outlier_ids] if spec.exclude_outliers else priced
    by_price = sorted(analysed, key=_price)

    return AnalysisResult(
        spec=spec,
        run_ids=list(run_ids),
        fetched_count=len(unique_rows),
        analysed_count=len(analysed),
        unpriced_count=unpriced_count,
        dropped_by_filters_count=dropped_by_filters_count,
        overall=price_stats([_price(row) for row in by_price]) if by_price else None,
        groups=_group_results(spec.group_by, by_price),
        outliers=outliers,
        cheapest=by_price[:TOP_LISTING_COUNT],
        newest=_newest(analysed)[:TOP_LISTING_COUNT],
        computed_at=now,
    )


def price_stats(prices: Sequence[float]) -> PriceStats:
    """Summary statistics; quartiles use `statistics.quantiles(n=4, method="inclusive")`.

    "inclusive" treats the prices as the whole population, so p25 and p75 always lie within
    [minimum, maximum] (the default "exclusive" method can extrapolate past them for small n).
    """
    if not prices:
        raise ValueError("price_stats needs at least one price")
    values = [float(price) for price in prices]
    if len(values) == 1:
        only = values[0]
        return PriceStats(count=1, minimum=only, p25=only, median=only, p75=only, maximum=only, mean=only)
    p25, _, p75 = statistics.quantiles(values, n=4, method="inclusive")
    return PriceStats(
        count=len(values),
        minimum=min(values),
        p25=float(p25),
        median=float(statistics.median(values)),
        p75=float(p75),
        maximum=max(values),
        mean=float(statistics.mean(values)),
    )


def iqr_bounds(prices: Sequence[float]) -> tuple[float, float]:
    """Tukey fences (p25 - 1.5*IQR, p75 + 1.5*IQR); unbounded below 4 prices, where quartiles mean little."""
    if len(prices) < 4:
        return (-math.inf, math.inf)
    p25, _, p75 = statistics.quantiles([float(price) for price in prices], n=4, method="inclusive")
    spread = p75 - p25
    return (p25 - 1.5 * spread, p75 + 1.5 * spread)


def apply_title_filter(title_filter: TitleFilter, rows: Sequence[ListingRow]) -> list[ListingRow]:
    """Rows whose title matches every `include` pattern and no `exclude` pattern (case-insensitive, compiled once).

    The 200-character cap on patterns does not prevent catastrophic backtracking; specs are hand-written or built
    from escaped words (phase B), so that is accepted.
    """
    includes = [re.compile(pattern, re.IGNORECASE) for pattern in title_filter.include]
    excludes = [re.compile(pattern, re.IGNORECASE) for pattern in title_filter.exclude]
    return [
        row
        for row in rows
        if all(pattern.search(row.title) for pattern in includes)
        and not any(pattern.search(row.title) for pattern in excludes)
    ]


def group_key(group_by: GroupBy, row: ListingRow) -> str:
    """The group a row belongs to. Week keys are ISO weeks in Singapore time, the timezone the rest of the output uses."""
    if group_by is GroupBy.NONE:
        return ALL_KEY
    if group_by is GroupBy.CONDITION:
        # Legacy "Used" listings have no five-level condition and land in "unknown".
        return row.condition.value if row.condition is not None else UNKNOWN_KEY
    if group_by is GroupBy.WEEK_POSTED:
        if row.posted_or_bumped_at is None:
            return UNKNOWN_KEY
        year, week, _ = row.posted_or_bumped_at.astimezone(SINGAPORE_TIME).isocalendar()
        return f"{year}-W{week:02d}"
    if group_by is GroupBy.SELLER:
        return row.seller_username or row.seller_id or UNKNOWN_KEY
    raise ValueError(f"unknown group_by {group_by!r}")


def _unique_by_id(rows: Sequence[ListingRow]) -> list[ListingRow]:
    seen_ids: set[str] = set()
    unique: list[ListingRow] = []
    for row in rows:
        if row.listing_id not in seen_ids:
            seen_ids.add(row.listing_id)
            unique.append(row)
    return unique


def _group_results(group_by: GroupBy, rows_by_price: list[ListingRow]) -> list[GroupResult]:
    if group_by is GroupBy.NONE:
        return []
    members: dict[str, list[ListingRow]] = {}
    for row in rows_by_price:
        members.setdefault(group_key(group_by, row), []).append(row)
    return [
        GroupResult(
            key=key,
            stats=price_stats([_price(row) for row in members[key]]),
            listing_ids=[row.listing_id for row in members[key]],
        )
        for key in sorted(members)
    ]


def _newest(rows: Sequence[ListingRow]) -> list[ListingRow]:
    dated = sorted((row for row in rows if row.posted_or_bumped_at is not None), key=_timestamp, reverse=True)
    undated = [row for row in rows if row.posted_or_bumped_at is None]
    return dated + undated


def _timestamp(row: ListingRow) -> datetime:
    assert row.posted_or_bumped_at is not None
    return row.posted_or_bumped_at


def _price(row: ListingRow) -> float:
    assert row.price is not None
    return float(row.price)
