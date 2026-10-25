"""Unit tests for market.render (pure): the text summary, its length cap, and the JSON form."""

import json
from datetime import UTC, datetime, timedelta

from carousearch.bot.messages import MAX_MESSAGE_LENGTH
from carousearch.filters.schema import Condition, SearchRequest
from carousearch.market.analysis import AnalysisSpec, GroupBy, analyse
from carousearch.market.render import render_analysis_json, render_analysis_text
from carousearch.storage import ListingRow

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
CONDITIONS = list(Condition)


def make_row(listing_id: str, price: float | None, **overrides) -> ListingRow:
    values = {
        "listing_id": listing_id,
        "title": f"iPhone 15 Pro 256GB {listing_id}",
        "price": price,
        "currency": "SGD",
        "price_display": "S$?" if price is None else f"S${price:g}",
        "condition": Condition.LIKE_NEW,
        "condition_label": "Like new",
        "like_count": 3,
        "seller_username": f"seller{listing_id}",
        "seller_id": f"id{listing_id}",
        "thumbnail_url": None,
        "listing_url": f"https://www.carousell.sg/p/iphone-15-pro-{listing_id}",
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
    values = {"question": "Used iPhone 15 Pro 256GB prices", "searches": [SearchRequest(query="iphone 15 pro 256gb")]}
    values.update(overrides)
    return AnalysisSpec(**values)


def sample_result(**spec_overrides):
    rows = [
        make_row("1001", 1050, condition=Condition.LIKE_NEW),
        make_row("1002", 980.5, condition=Condition.LIGHTLY_USED, posted_or_bumped_at=NOW - timedelta(hours=2)),
        make_row("1003", 1150, condition=Condition.LIKE_NEW),
        make_row("1004", 1000, condition=Condition.LIGHTLY_USED),
        make_row("1005", 1100, condition=Condition.LIKE_NEW),
        make_row("1006", 300, title="iPhone 15 Pro, faulty", condition=Condition.HEAVILY_USED),
        make_row("1007", None),
    ]
    return analyse(make_spec(**spec_overrides), rows, [11, 12], NOW)


def synthetic_rows(count: int) -> list[ListingRow]:
    rows = []
    for index in range(count):
        title = f"Apple iPhone 15 Pro Max 256GB {'very long description words ' * 12} #{index}"
        price = None if index % 17 == 0 else 900 + (index * 37) % 500 + 0.37 * (index % 3)
        if index % 50 == 1:
            price = 20.0  # low outliers
        rows.append(
            make_row(
                f"{1_400_000_000 + index}",
                price,
                title=title,
                condition=CONDITIONS[index % len(CONDITIONS)],
                seller_username=f"a_seller_with_a_long_username_{index}",
                posted_or_bumped_at=NOW - timedelta(hours=index * 5) if index % 9 else None,
            )
        )
    return rows


def test_text_has_question_counts_and_overall_line() -> None:
    text = render_analysis_text(sample_result())
    lines = text.splitlines()

    assert lines[0] == "Used iPhone 15 Pro 256GB prices"
    # 7 fetched, 1 unpriced; 6 priced -> 1006 is a low outlier (bounds need >= 4 prices).
    assert lines[1] == (
        "Fetched 7 listings (2 runs); analysed 5 (1 without a price, 0 filtered out, 1 outliers excluded)"
    )
    assert lines[3].startswith("All  n=5  median S$1,050  p25–p75 S$1,000–S$1,100  min S$980.50  max S$1,150")
    assert "Outliers excluded:" in text
    assert '1006  "iPhone 15 Pro, faulty"  S$300 (low' in text
    assert "Cheapest:" in text
    assert "  S$980.50  " in text
    assert "https://www.carousell.sg/p/iphone-15-pro-1002" in text
    assert "Newest:" in text


def test_text_has_one_line_per_group() -> None:
    text = render_analysis_text(sample_result(group_by=GroupBy.CONDITION))

    group_lines = [line for line in text.splitlines() if line.startswith(("like_new", "lightly_used"))]
    assert len(group_lines) == 2
    assert group_lines[0].startswith("lightly_used  n=2  median S$990.25")
    assert group_lines[1].startswith("like_new      n=3  median S$1,100")
    assert "heavily_used" not in text  # the only heavily used listing is an excluded outlier


def test_text_mentions_kept_outliers() -> None:
    text = render_analysis_text(sample_result(exclude_outliers=False))

    assert "analysed 6 (1 without a price, 0 filtered out, 1 outliers kept)" in text
    assert "Outliers (kept in the statistics):" in text


def test_text_for_no_priced_listings() -> None:
    result = analyse(make_spec(), [make_row("1", None)], [3], NOW)

    text = render_analysis_text(result)

    assert "Fetched 1 listings (1 run); analysed 0" in text
    assert text.endswith("No priced listings to analyse.")


def test_text_rounds_long_fractions_to_cents() -> None:
    rows = [make_row(str(index), price) for index, price in enumerate([100, 100.01, 100.02])]

    text = render_analysis_text(analyse(make_spec(), rows, [1], NOW))

    assert "median S$100.01" in text
    assert "0000000" not in text


def test_text_stays_under_telegram_limit_for_many_rows_with_long_titles() -> None:
    result = analyse(make_spec(group_by=GroupBy.SELLER), synthetic_rows(200), [1, 2, 3], NOW)
    assert len(result.groups) > 100  # enough group lines to overflow on their own

    text = render_analysis_text(result)

    assert len(text) <= MAX_MESSAGE_LENGTH
    assert text.startswith("Used iPhone 15 Pro 256GB prices\nFetched 200 listings (3 runs)")
    assert "\nAll " in text
    # Newest and cheapest are emptied before the group table loses a line.
    assert "Newest:\n  … 5 more" in text
    assert "Cheapest:\n  … 5 more" in text
    assert "https://www.carousell.sg/p/" not in text


def test_text_cuts_newest_before_cheapest() -> None:
    result = analyse(make_spec(group_by=GroupBy.CONDITION), synthetic_rows(200), [1], NOW)
    full = render_analysis_text(result)
    assert len(full) <= MAX_MESSAGE_LENGTH
    assert "Newest:" in full and "Cheapest:" in full

    long_question = "q" * (MAX_MESSAGE_LENGTH - len(full) + 200)
    squeezed_spec = make_spec(question=long_question, group_by=GroupBy.CONDITION)
    squeezed = render_analysis_text(analyse(squeezed_spec, synthetic_rows(200), [1], NOW))

    assert len(squeezed) <= MAX_MESSAGE_LENGTH
    cheapest_section = squeezed.split("Cheapest:")[1].split("Newest:")[0]
    assert "https://" in cheapest_section  # cheapest lines survived; only the newest section was cut
    newest_section = squeezed.split("Newest:")[1]
    assert "more" in newest_section


def test_json_round_trips() -> None:
    result = sample_result(group_by=GroupBy.CONDITION)

    decoded = json.loads(render_analysis_json(result))

    assert decoded["spec"]["question"] == "Used iPhone 15 Pro 256GB prices"
    assert decoded["spec"]["group_by"] == "condition"
    assert decoded["spec"]["searches"][0]["query"] == "iphone 15 pro 256gb"
    assert decoded["run_ids"] == [11, 12]
    assert decoded["computed_at"] == NOW.isoformat()
    assert decoded["overall"]["median"] == 1050
    assert [group["key"] for group in decoded["groups"]] == ["lightly_used", "like_new"]
    assert decoded["outliers"][0]["listing_id"] == "1006"
    assert decoded["cheapest"][0]["condition"] == "lightly_used"
    assert decoded["cheapest"][0]["posted_or_bumped_at"] == (NOW - timedelta(hours=2)).isoformat()
    assert AnalysisSpec.model_validate(decoded["spec"]) == result.spec


def test_json_for_empty_result() -> None:
    decoded = json.loads(render_analysis_json(analyse(make_spec(), [], [], NOW)))

    assert decoded["overall"] is None
    assert decoded["cheapest"] == []


def test_json_for_many_rows_parses() -> None:
    result = analyse(make_spec(group_by=GroupBy.WEEK_POSTED), synthetic_rows(200), [1], NOW)

    decoded = json.loads(render_analysis_json(result))

    assert decoded["fetched_count"] == 200
