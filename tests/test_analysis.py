"""Unit tests for market.analysis (pure): statistics, filters, grouping, outliers and the spec model."""

import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from carousearch.filters.schema import Condition, SearchRequest
from carousearch.market.analysis import (
    AnalysisSpec,
    GroupBy,
    PriceStats,
    TitleFilter,
    analyse,
    apply_title_filter,
    group_key,
    iqr_bounds,
    price_stats,
)
from carousearch.market.limits import HarvestLimits
from carousearch.storage import ListingRow

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


def make_row(listing_id: str, price: float | None = 100.0, **overrides) -> ListingRow:
    values = {
        "listing_id": listing_id,
        "title": f"Listing {listing_id}",
        "price": price,
        "currency": "SGD",
        "price_display": "S$?" if price is None else f"S${price:g}",
        "condition": Condition.LIKE_NEW,
        "condition_label": "Like new",
        "like_count": 0,
        "seller_username": f"seller{listing_id}",
        "seller_id": f"id{listing_id}",
        "thumbnail_url": None,
        "listing_url": f"https://www.carousell.sg/p/{listing_id}",
        "posted_or_bumped_at": NOW - timedelta(days=1),
        "posted_or_bumped_kind": None,
        "content_hash": "0" * 16,
        "first_seen_at": NOW,
        "last_seen_at": NOW,
        "last_changed_at": NOW,
    }
    values.update(overrides)
    return ListingRow(**values)


def make_spec(**overrides) -> AnalysisSpec:
    values = {"question": "iPhone prices", "searches": [SearchRequest(query="iphone 15 pro")]}
    values.update(overrides)
    return AnalysisSpec(**values)


# price_stats


def test_price_stats_matches_hand_computed_values() -> None:
    # Inclusive quartiles of 1..8: positions 1 + 7 * 0.25 = 2.75 and 1 + 7 * 0.75 = 6.25.
    stats = price_stats([8, 1, 7, 2, 6, 3, 5, 4])

    assert stats == PriceStats(count=8, minimum=1, p25=2.75, median=4.5, p75=6.25, maximum=8, mean=4.5)


def test_price_stats_odd_count() -> None:
    stats = price_stats([100, 200, 300, 400, 1000])

    assert (stats.p25, stats.median, stats.p75, stats.mean) == (200, 300, 400, 400)


def test_price_stats_two_prices() -> None:
    stats = price_stats([100, 200])

    assert (stats.minimum, stats.p25, stats.median, stats.p75, stats.maximum) == (100, 125, 150, 175, 200)


def test_price_stats_single_price_is_all_equal() -> None:
    assert price_stats([750]) == PriceStats(1, 750, 750, 750, 750, 750, 750)


def test_price_stats_rejects_empty() -> None:
    with pytest.raises(ValueError):
        price_stats([])


# iqr_bounds


@pytest.mark.parametrize("prices", [[], [5], [5, 6], [1, 100, 10_000]])
def test_iqr_bounds_unbounded_below_four_prices(prices: list[float]) -> None:
    assert iqr_bounds(prices) == (-math.inf, math.inf)


def test_iqr_bounds_four_or_more_prices() -> None:
    # 1..8: p25 2.75, p75 6.25, IQR 3.5 -> (2.75 - 5.25, 6.25 + 5.25).
    assert iqr_bounds([1, 2, 3, 4, 5, 6, 7, 8]) == (-2.5, 11.5)
    # Four equal prices: IQR 0, so the bounds collapse onto the price.
    assert iqr_bounds([10, 10, 10, 10]) == (10, 10)


# title filter


def test_title_filter_include_requires_every_pattern() -> None:
    rows = [
        make_row("1", title="iPhone 15 Pro 256GB"),
        make_row("2", title="iPhone 15 Pro 128GB"),
        make_row("3", title="IPHONE 15 PRO 256gb black"),
    ]

    kept = apply_title_filter(TitleFilter(include=[r"15\s*pro", "256"]), rows)

    assert [row.listing_id for row in kept] == ["1", "3"]


def test_title_filter_exclude_rejects_any_match() -> None:
    rows = [
        make_row("1", title="iPhone 15 Pro"),
        make_row("2", title="iPhone 15 Pro FAULTY screen"),
        make_row("3", title="iPhone 15 Pro case only"),
    ]

    kept = apply_title_filter(TitleFilter(exclude=["faulty", r"\bcase\b"]), rows)

    assert [row.listing_id for row in kept] == ["1"]


def test_empty_title_filter_keeps_everything() -> None:
    rows = [make_row("1"), make_row("2")]

    assert apply_title_filter(TitleFilter(), rows) == rows


def test_title_filter_rejects_invalid_regex() -> None:
    with pytest.raises(ValidationError, match="not a valid regex"):
        TitleFilter(include=["(unclosed"])


def test_title_filter_rejects_long_patterns() -> None:
    TitleFilter(exclude=["a" * 200])
    with pytest.raises(ValidationError, match="longer than 200"):
        TitleFilter(exclude=["a" * 201])


def test_title_filter_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        TitleFilter(includes=["x"])


# group_key


def test_group_key_none() -> None:
    assert group_key(GroupBy.NONE, make_row("1")) == "all"


def test_group_key_condition() -> None:
    assert group_key(GroupBy.CONDITION, make_row("1", condition=Condition.WELL_USED)) == "well_used"
    assert group_key(GroupBy.CONDITION, make_row("2", condition=None, condition_label="Used")) == "unknown"


def test_group_key_week_posted() -> None:
    # 2026-10-22 is a Thursday in ISO week 43; 2027-01-01 belongs to 2026-W53.
    assert group_key(GroupBy.WEEK_POSTED, make_row("1", posted_or_bumped_at=datetime(2026, 10, 22, tzinfo=UTC))) == (
        "2026-W43"
    )
    assert group_key(GroupBy.WEEK_POSTED, make_row("2", posted_or_bumped_at=datetime(2027, 1, 1, tzinfo=UTC))) == (
        "2026-W53"
    )
    assert group_key(GroupBy.WEEK_POSTED, make_row("3", posted_or_bumped_at=datetime(2026, 1, 5, tzinfo=UTC))) == (
        "2026-W02"
    )
    assert group_key(GroupBy.WEEK_POSTED, make_row("4", posted_or_bumped_at=None)) == "unknown"


def test_group_key_seller() -> None:
    assert group_key(GroupBy.SELLER, make_row("1", seller_username="alice", seller_id="9")) == "alice"
    assert group_key(GroupBy.SELLER, make_row("2", seller_username=None, seller_id="9")) == "9"
    assert group_key(GroupBy.SELLER, make_row("3", seller_username=None, seller_id=None)) == "unknown"


# AnalysisSpec


def test_spec_defaults() -> None:
    spec = make_spec()

    assert spec.limits == HarvestLimits()
    assert spec.title_filter == TitleFilter()
    assert spec.group_by is GroupBy.NONE
    assert spec.exclude_outliers is True
    assert spec.max_age_days is None


def test_spec_rejects_page_cursor() -> None:
    with pytest.raises(ValidationError, match="page_cursor"):
        make_spec(searches=[SearchRequest(query="iphone", page_cursor="abc")])


def test_spec_deduplicates_searches_by_request_hash() -> None:
    # page_size is not part of the request hash, so these two harvest the same run.
    spec = make_spec(
        searches=[
            SearchRequest(query="iphone 15"),
            SearchRequest(query="iphone 15", page_size=48),
            SearchRequest(query="iphone 15", price_max=1000),
        ]
    )

    assert [(search.query, search.price_max) for search in spec.searches] == [("iphone 15", None), ("iphone 15", 1000)]


def test_spec_search_count_limits() -> None:
    with pytest.raises(ValidationError):
        make_spec(searches=[])
    with pytest.raises(ValidationError):
        make_spec(searches=[SearchRequest(query=f"q{index}") for index in range(4)])


def test_spec_forbids_extra_fields_and_round_trips_through_json() -> None:
    with pytest.raises(ValidationError):
        make_spec(group="condition")
    spec = make_spec(group_by="condition", title_filter={"exclude": ["faulty"]}, limits={"max_pages": 3})

    assert AnalysisSpec.model_validate_json(spec.model_dump_json()) == spec


def test_harvest_limits_validation() -> None:
    with pytest.raises(ValidationError):
        HarvestLimits(max_pages=0)
    with pytest.raises(ValidationError):
        HarvestLimits(max_pages=51)
    with pytest.raises(ValidationError):
        HarvestLimits(page_size=49)
    with pytest.raises(ValidationError):
        HarvestLimits(max_age_days=0)
    with pytest.raises(ValidationError):
        HarvestLimits(pages=3)


# analyse


def test_analyse_empty_input() -> None:
    result = analyse(make_spec(group_by=GroupBy.CONDITION), [], [7], NOW)

    assert result.fetched_count == 0
    assert result.analysed_count == 0
    assert result.overall is None
    assert result.groups == []
    assert result.outliers == []
    assert result.cheapest == []
    assert result.newest == []
    assert result.run_ids == [7]
    assert result.computed_at == NOW


def test_analyse_counts_listings_without_a_price() -> None:
    rows = [make_row("1", 100), make_row("2", None), make_row("3", 300)]

    result = analyse(make_spec(), rows, [1], NOW)

    assert result.fetched_count == 3
    assert result.unpriced_count == 1
    assert result.analysed_count == 2
    assert result.overall is not None
    assert result.overall.median == 200
    assert "2" not in {row.listing_id for row in result.cheapest + result.newest}


def test_analyse_only_unpriced_listings() -> None:
    result = analyse(make_spec(), [make_row("1", None)], [1], NOW)

    assert (result.fetched_count, result.unpriced_count, result.analysed_count) == (1, 1, 0)
    assert result.overall is None


def test_analyse_deduplicates_rows_across_runs() -> None:
    rows = [make_row("1", 100), make_row("2", 200), make_row("1", 100)]

    result = analyse(make_spec(), rows, [1, 2], NOW)

    assert result.fetched_count == 2
    assert result.analysed_count == 2


def test_analyse_title_and_age_filters_are_counted() -> None:
    rows = [
        make_row("1", 100, title="iPhone 15 Pro"),
        make_row("2", 50, title="iPhone 15 Pro faulty"),
        make_row("3", 120, title="iPhone 15 Pro", posted_or_bumped_at=NOW - timedelta(days=40)),
        make_row("4", 130, title="iPhone 15 Pro", posted_or_bumped_at=None),
        make_row("5", None, title="iPhone 15 Pro broken faulty"),
    ]
    spec = make_spec(title_filter=TitleFilter(exclude=["faulty"]), max_age_days=30)

    result = analyse(spec, rows, [1], NOW)

    # 2 and 5 are dropped by title, 3 by age; 4 has no timestamp and is kept.
    assert result.dropped_by_filters_count == 3
    assert result.unpriced_count == 0
    assert [row.listing_id for row in result.cheapest] == ["1", "4"]


def test_analyse_excludes_outliers_from_overall_and_groups() -> None:
    normal = [
        make_row(str(index), price, condition=Condition.LIKE_NEW) for index, price in enumerate(range(1000, 1100, 10))
    ]
    low = make_row("low", 50, title="iPhone box only", condition=Condition.WELL_USED)
    high = make_row("high", 9000, condition=Condition.BRAND_NEW)
    rows = [*normal, low, high]

    result = analyse(make_spec(group_by=GroupBy.CONDITION), rows, [1], NOW)

    assert [(outlier.listing_id, outlier.side) for outlier in result.outliers] == [("low", "low"), ("high", "high")]
    low_outlier = result.outliers[0]
    assert low_outlier.price == 50 and low_outlier.title == "iPhone box only"
    assert low_outlier.bound == iqr_bounds([row.price for row in rows])[0]
    assert result.analysed_count == 10
    assert result.overall is not None
    assert (result.overall.minimum, result.overall.maximum) == (1000, 1090)
    assert [group.key for group in result.groups] == ["like_new"]
    assert result.fetched_count == result.analysed_count + len(result.outliers)


def test_analyse_keeps_outliers_when_configured_but_still_reports_them() -> None:
    rows = [make_row(str(index), price) for index, price in enumerate([100, 105, 110, 115, 120, 5000])]

    result = analyse(make_spec(exclude_outliers=False), rows, [1], NOW)

    assert [outlier.listing_id for outlier in result.outliers] == ["5"]
    assert result.analysed_count == 6
    assert result.overall is not None
    assert result.overall.maximum == 5000


def test_analyse_no_outliers_with_fewer_than_four_prices() -> None:
    rows = [make_row("1", 1), make_row("2", 100), make_row("3", 100_000)]

    result = analyse(make_spec(), rows, [1], NOW)

    assert result.outliers == []
    assert result.analysed_count == 3


def test_analyse_group_by_none_has_no_groups() -> None:
    result = analyse(make_spec(), [make_row("1", 100), make_row("2", 200)], [1], NOW)

    assert result.groups == []


def test_analyse_group_by_condition_sorted_by_key_with_ids_by_price() -> None:
    rows = [
        make_row("a", 300, condition=Condition.LIKE_NEW),
        make_row("b", 100, condition=Condition.LIKE_NEW),
        make_row("c", 200, condition=Condition.BRAND_NEW),
        make_row("d", 150, condition=None),
    ]

    result = analyse(make_spec(group_by=GroupBy.CONDITION), rows, [1], NOW)

    assert [(group.key, group.listing_ids) for group in result.groups] == [
        ("brand_new", ["c"]),
        ("like_new", ["b", "a"]),
        ("unknown", ["d"]),
    ]
    like_new = result.groups[1].stats
    assert (like_new.count, like_new.median) == (2, 200)


def test_analyse_group_by_week_posted() -> None:
    rows = [
        make_row("a", 100, posted_or_bumped_at=datetime(2026, 10, 20, tzinfo=UTC)),
        make_row("b", 200, posted_or_bumped_at=datetime(2026, 10, 14, tzinfo=UTC)),
        make_row("c", 300, posted_or_bumped_at=datetime(2026, 10, 21, tzinfo=UTC)),
        make_row("d", 400, posted_or_bumped_at=None),
    ]

    result = analyse(make_spec(group_by=GroupBy.WEEK_POSTED), rows, [1], NOW)

    assert [(group.key, group.listing_ids) for group in result.groups] == [
        ("2026-W42", ["b"]),
        ("2026-W43", ["a", "c"]),
        ("unknown", ["d"]),
    ]


def test_analyse_group_by_seller() -> None:
    rows = [
        make_row("a", 100, seller_username="bob"),
        make_row("b", 200, seller_username="alice"),
        make_row("c", 50, seller_username="bob"),
        make_row("d", 75, seller_username=None, seller_id=None),
    ]

    result = analyse(make_spec(group_by=GroupBy.SELLER), rows, [1], NOW)

    assert [(group.key, group.listing_ids) for group in result.groups] == [
        ("alice", ["b"]),
        ("bob", ["c", "a"]),
        ("unknown", ["d"]),
    ]


def test_analyse_cheapest_and_newest_are_capped_and_ordered() -> None:
    base = make_row("x")
    rows = [
        replace(
            base,
            listing_id=str(index),
            price=float(1000 - index * 10),
            posted_or_bumped_at=NOW - timedelta(hours=index),
        )
        for index in range(8)
    ]
    rows.append(replace(base, listing_id="undated", price=1.0, posted_or_bumped_at=None))
    rows.append(replace(base, listing_id="undated2", price=2.0, posted_or_bumped_at=None))

    result = analyse(make_spec(exclude_outliers=False), rows, [1], NOW)

    assert [row.listing_id for row in result.cheapest] == ["undated", "undated2", "7", "6", "5"]
    assert [row.listing_id for row in result.newest] == ["0", "1", "2", "3", "4"]


def test_analyse_newest_puts_undated_last() -> None:
    rows = [make_row("undated", 100, posted_or_bumped_at=None), make_row("dated", 200)]

    result = analyse(make_spec(), rows, [1], NOW)

    assert [row.listing_id for row in result.newest] == ["dated", "undated"]
