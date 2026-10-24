"""Text and JSON renderings of an `AnalysisResult`. Pure functions: a result in, a string out.

The text form is shared by the terminal and Telegram, so it is plain text and always fits in one
Telegram message. When it would not, the least important lines go first: newest listings, then
cheapest listings, then outliers, then group rows (each cut section says how many lines it lost).
"""

import json
from dataclasses import asdict, dataclass
from datetime import datetime

from pydantic import BaseModel

from carousearch.bot.messages import MAX_MESSAGE_LENGTH, truncate
from carousearch.filters.payload import format_price
from carousearch.market.analysis import AnalysisResult, PriceStats
from carousearch.text_render import format_date

ALL_LABEL = "All"
MAX_TITLE_LENGTH = 60


@dataclass
class _Section:
    """A heading plus lines that can be dropped one by one from the end."""

    heading: str | None
    lines: list[str]
    shown: int  # how many of `lines` are rendered


def render_analysis_text(result: AnalysisResult) -> str:
    head = [result.spec.question, _counts_line(result), ""]
    if result.overall is None:
        head.append("No priced listings to analyse.")
        return truncate("\n".join(head), MAX_MESSAGE_LENGTH)

    labels = [ALL_LABEL, *(group.key for group in result.groups)]
    label_width = max(len(label) for label in labels)
    count_width = max(len(f"n={result.overall.count}"), *(len(f"n={g.stats.count}") for g in result.groups))
    head.append(_stats_line(ALL_LABEL, result.overall, label_width, count_width))
    group_lines = [_stats_line(group.key, group.stats, label_width, count_width) for group in result.groups]

    outlier_heading = "Outliers excluded:" if result.spec.exclude_outliers else "Outliers (kept in the statistics):"
    outlier_lines = [
        f"  {outlier.listing_id}  {_title(outlier.title)}  {_money(outlier.price)}"
        f" ({outlier.side}, bound {_money(outlier.bound)})"
        for outlier in result.outliers
    ]
    cheapest_lines = [f"  {_money(row.price)}  {_title(row.title)}  {row.listing_url}" for row in result.cheapest]
    newest_lines = [
        f"  {format_date(row.posted_or_bumped_at)}  {_money(row.price)}  {_title(row.title)}  {row.listing_url}"
        for row in result.newest
    ]

    # Listed in the order they are cut when the text is too long.
    sections = {
        "newest": _Section("Newest:", newest_lines, len(newest_lines)),
        "cheapest": _Section("Cheapest:", cheapest_lines, len(cheapest_lines)),
        "outliers": _Section(outlier_heading, outlier_lines, len(outlier_lines)),
        "groups": _Section(None, group_lines, len(group_lines)),
    }
    order = ["groups", "outliers", "cheapest", "newest"]

    def assemble() -> str:
        lines = list(head)
        for name in order:
            section = sections[name]
            if not section.lines:
                continue
            if section.heading is not None:
                lines.extend(["", section.heading])
            lines.extend(section.lines[: section.shown])
            hidden = len(section.lines) - section.shown
            if hidden:
                lines.append(f"  … {hidden} more")
        return "\n".join(lines)

    text = assemble()
    for section in sections.values():
        while len(text) > MAX_MESSAGE_LENGTH and section.shown > 0:
            section.shown -= 1
            text = assemble()
    return truncate(text, MAX_MESSAGE_LENGTH)


def render_analysis_json(result: AnalysisResult) -> str:
    payload = asdict(result)
    payload["spec"] = result.spec.model_dump(mode="json")
    return json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default)


def _counts_line(result: AnalysisResult) -> str:
    runs = "1 run" if len(result.run_ids) == 1 else f"{len(result.run_ids)} runs"
    outlier_fate = "excluded" if result.spec.exclude_outliers else "kept"
    details = [
        f"{result.unpriced_count} without a price",
        f"{result.dropped_by_filters_count} filtered out",
        f"{len(result.outliers)} outliers {outlier_fate}",
    ]
    return f"Fetched {result.fetched_count} listings ({runs}); analysed {result.analysed_count} ({', '.join(details)})"


def _stats_line(label: str, stats: PriceStats, label_width: int, count_width: int) -> str:
    return (
        f"{label:<{label_width}}  {f'n={stats.count}':<{count_width}}  median {_money(stats.median)}"
        f"  p25–p75 {_money(stats.p25)}–{_money(stats.p75)}  min {_money(stats.minimum)}  max {_money(stats.maximum)}"
    )


def _money(price: float | None) -> str:
    # Quartiles and means can have long fractions; cents are the finest unit that means anything here.
    return "-" if price is None else f"S${format_price(round(price, 2))}"


def _title(title: str) -> str:
    single_line = " ".join(title.split())
    if len(single_line) > MAX_TITLE_LENGTH:
        single_line = single_line[: MAX_TITLE_LENGTH - 1] + "…"
    return f'"{single_line}"'


def _json_default(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    raise TypeError(f"cannot serialise {type(value).__name__} to JSON")
