"""Plain-text rendering of listings for the terminal. Pure functions: models in, strings out."""

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from carousearch.agent.cost import ModelUsage, format_cost_line
from carousearch.agent.plan import PlanItemStatus, SearchPlan, review_item_label
from carousearch.agent.result import (
    NOT_SAMPLED_WARNING,
    AgentAnswer,
    DroppedListing,
    KeptListing,
    ToolCall,
    planning_sampled_listings,
    tool_call_counts,
)
from carousearch.filters.schema import Listing, ListingDetail, SearchResult, SellerVerification
from carousearch.outreach.models import OutreachMessage, SendReport
from carousearch.screening.models import PriceSource, ResolvedPrice, ScreenedListing, format_sgd

if TYPE_CHECKING:
    from carousearch.agent.evals import EvalOutcome, EvalPrompt
    from carousearch.outreach.service import DraftBatch

SINGAPORE_TIME = timezone(timedelta(hours=8), "SGT")

# (header, width) for each table column; width 0 means "no limit" (the URL is never cut).
_TABLE_COLUMNS = [("ID", 10), ("PRICE", 9), ("CONDITION", 12), ("LIKES", 5), ("TITLE", 44), ("SELLER", 18), ("URL", 0)]
_SCREENED_TABLE_COLUMNS = [
    ("ID", 10), ("PRICE", 9), ("YOUR PRICE", 12), ("FLAGS", 34), ("CONDITION", 12), ("LIKES", 5), ("TITLE", 40),
    ("SELLER", 18), ("URL", 0),
]


def render_listing_table(listings: list[Listing]) -> str:
    rows = [[header for header, _ in _TABLE_COLUMNS]]
    for listing in listings:
        rows.append(
            [
                listing.id,
                listing.price_display,
                listing.condition_label or "-",
                str(listing.like_count),
                listing.title,
                listing.seller_username or "-",
                listing.listing_url,
            ]
        )
    return "\n".join(_format_row(row, _TABLE_COLUMNS) for row in rows)


def render_screened_table(screened: list[ScreenedListing]) -> str:
    """The search table with the screening columns: YOUR PRICE and FLAGS (tag labels)."""
    rows = [[header for header, _ in _SCREENED_TABLE_COLUMNS]]
    for item in screened:
        listing = item.listing
        rows.append(
            [
                listing.id,
                listing.price_display,
                describe_resolved_price(item.resolved_price, listing.price_display),
                ", ".join(tag.label for tag in item.tags) or "-",
                listing.condition_label or "-",
                str(listing.like_count),
                listing.title,
                listing.seller_username or "-",
                listing.listing_url,
            ]
        )
    return "\n".join(_format_row(row, _SCREENED_TABLE_COLUMNS) for row in rows)


def describe_resolved_price(resolved: ResolvedPrice, price_display: str) -> str:
    """The YOUR PRICE cell: the listed price, a description price ("S$850 (desc)"), or "not stated"."""
    if resolved.source == PriceSource.DESCRIPTION and resolved.amount is not None:
        return f"{format_sgd(resolved.amount)} (desc)"
    if resolved.source == PriceSource.NOT_STATED:
        return "not stated"
    return price_display


def render_search_summary(result: SearchResult, shown_count: int) -> str:
    total = result.total_display or (str(result.total) if result.total is not None else "unknown")
    more = "more pages available (use --json to get next_page)" if result.next_page else "no more pages"
    return f"Showing {shown_count} of {total} results; {more}."


def render_listing_detail(detail: ListingDetail) -> str:
    verification = detail.seller_verification
    lines = [
        detail.title,
        f"{detail.price_display}  |  {detail.condition_label or '-'}  |  {detail.like_count} likes",
        detail.listing_url,
        "",
        _field("Seller", f"{detail.seller_username or '-'} (id {detail.seller_id or '-'})"),
        _field("Rating", _rating_text(detail)),
        _field("Joined", format_date(detail.seller_joined_at)),
        _field("Response rate", detail.seller_response_rate or "-"),
        _field("Verified", _verification_text(verification) if verification else "-"),
        _field("Created", format_datetime(detail.created_at)),
        _field("Last modified", format_datetime(detail.last_modified_at)),
        _field("Deal", f"meet-up {_yes_no(detail.meetup_available)}, mailing {_yes_no(detail.mailing_available)}"),
    ]
    for index, meetup in enumerate(detail.meetup_locations):
        place = f"{meetup.name}, {meetup.address}" if meetup.address else meetup.name
        lines.append(_field("Meet-up points" if index == 0 else "", place))
    lines.append(_field("Photos", str(len(detail.image_urls))))
    lines.extend(f"  {url}" for url in detail.image_urls)
    lines.extend(["", "Description:"])
    lines.extend(f"  {line}" for line in (detail.description or "-").splitlines())
    return "\n".join(lines)


def render_agent_answer(answer: AgentAnswer) -> str:
    """`carousearch ask`: the execution's tool trace, the plan (with the planning calls), the kept listings as a
    table, the dropped ones with reasons, how many more were seen, the plan review, the notes, a suggested knowledge
    note and the estimated cost of both phases."""
    sections = [render_tool_trace(answer.tool_trace)]
    if answer.plan is not None:
        sections.append(render_plan(answer.plan, answer.planning_trace))
    sections += [
        render_kept_listings(answer.kept),
        render_dropped_listings(answer.dropped),
        f"Not shortlisted: {answer.not_shortlisted} more listings seen.",
    ]
    if answer.plan_review:
        sections.append(render_plan_review(answer.plan_review, answer.plan))
    sections.append(render_notes(answer.notes))
    if answer.suggested_knowledge:
        sections.append("Suggested knowledge note:\n" + _indent(answer.suggested_knowledge))
    sections.append(format_cost_line(answer.usage))
    return "\n\n".join(sections)


def render_planned_search(plan: SearchPlan, planning_trace: list[ToolCall], usage: list[ModelUsage]) -> str:
    """`carousearch ask --plan-only`: the plan, the planning calls and what planning cost."""
    return "\n\n".join([render_plan(plan, planning_trace), format_cost_line(usage)])


def render_plan(plan: SearchPlan, planning_trace: list[ToolCall] | None = None) -> str:
    """The plan in readable sections, headed by a count of the planning tool calls (and a warning when planning
    read no listings; a plan from a file has no planning trace)."""
    lines = [f"Plan ({render_tool_counts('planning tool call', planning_trace or [])}):"]
    if planning_trace and not planning_sampled_listings(planning_trace):
        lines.append(f"Warning: {NOT_SAMPLED_WARNING}")
    lines.append(_field("Goal", plan.goal))
    lines.append(_field("Item class", plan.item_class))
    lines.append(_field("Value drivers", "; ".join(plan.value_drivers) or "-"))
    lines.append(f"Attributes ({len(plan.attributes)}):")
    for attribute in plan.attributes:
        unit = f" ({attribute.unit})" if attribute.unit else ""
        source = attribute.source.value
        lines.append(f"  {attribute.name}{unit} [{source}]: {attribute.detail}. For: {attribute.purpose}")
    lines.append(_field("Benchmark", describe_benchmark(plan)))
    lines.append(f"Exclusions ({len(plan.exclusions)}):")
    lines += [f"  {number}. {exclusion.rule}. Why: {exclusion.reason}"
              for number, exclusion in enumerate(plan.exclusions, start=1)]
    lines.append(_field("Ranking", plan.ranking))
    lines.append(_field("Template", plan.template or "none"))
    estimate = plan.budget_estimate
    lines.append(_field("Estimate", f"{estimate.searches} searches, {estimate.details} details, "
                                    f"{estimate.photos} photos, {estimate.judgements} worker calls"))
    lines.append("Assumptions:")
    lines += [f"  - {assumption}" for assumption in plan.assumptions] or ["  - none"]
    return "\n".join(lines)


def describe_benchmark(plan: SearchPlan) -> str:
    """ "median over <population>, grouped by a, b, normalised as <formula>", or "none"."""
    benchmark = plan.benchmark
    if benchmark is None:
        return "none"
    text = f"{benchmark.statistic} over {benchmark.population}"
    if benchmark.grouping:
        text += f", grouped by {', '.join(benchmark.grouping)}"
    if benchmark.normalisation:
        text += f", normalised as {benchmark.normalisation}"
    return text


def render_plan_review(review: list[PlanItemStatus], plan: SearchPlan | None) -> str:
    lines = ["Plan review:"]
    lines += [f"  {status.status:<8}  {review_item_label(plan, status.item)}: {status.note}" for status in review]
    return "\n".join(lines)


def render_eval_prompts(prompts: "Sequence[EvalPrompt]") -> str:
    """`carousearch eval` without --live: each prompt and the plan properties it is checked for."""
    blocks = [f"{prompt.name}: {prompt.prompt}\n  expects: {', '.join(prompt.expect)}" for prompt in prompts]
    return "\n".join(blocks) + f"\n\n{len(prompts)} prompts. Run them once for real with --live (costs money)."


def render_eval_outcome(outcome: "EvalOutcome") -> str:
    """One live eval prompt: its plan in brief, the checks, the plan review, the tool counts and the cost."""
    from carousearch.agent.evals import attribute_sources, review_counts

    lines = [f"== {outcome.prompt.name} ==", f"Prompt: {outcome.prompt.prompt}"]
    answer = outcome.answer
    if answer is None:
        return "\n".join([*lines, f"No answer: {outcome.error}"])
    if answer.plan is not None:
        sources = ", ".join(f"{source} {count}" for source, count in attribute_sources(answer.plan).items())
        lines.append(_field("Item class", answer.plan.item_class))
        lines.append(_field("Attributes", sources))
        lines.append(_field("Benchmark", describe_benchmark(answer.plan)))
        lines.append(_field("Template", answer.plan.template or "none"))
    lines.append(_field("Sampled", "yes" if planning_sampled_listings(answer.planning_trace) else "no"))
    passed = sum(check.passed for check in outcome.checks)
    failed = [check.name for check in outcome.checks if not check.passed]
    lines.append(_field("Checks", f"{passed} of {len(outcome.checks)} passed"
                                  + (f"; failed: {', '.join(failed)}" if failed else "")))
    review = ", ".join(f"{status} {count}" for status, count in review_counts(answer).items())
    lines.append(_field("Review", review or "none"))
    lines.append(_field("Result", f"{len(answer.kept)} kept, {len(answer.dropped)} dropped, "
                                  f"{answer.not_shortlisted} not shortlisted"))
    lines.append(_field("Planning", render_tool_counts("tool call", answer.planning_trace)))
    lines.append(_field("Execution", render_tool_counts("tool call", answer.tool_trace)))
    lines.append(_field("Cost", f"about US${outcome.cost_usd:.4f}"))
    return "\n".join(lines)


def render_eval_summary(outcomes: "Sequence[EvalOutcome]") -> str:
    """The table at the end of a live eval: per prompt, checks passed, review statuses and cost; then the total."""
    from carousearch.agent.evals import review_counts

    columns = [("PROMPT", 30), ("CHECKS", 7), ("REVIEW", 30), ("COST US$", 0)]
    rows = [[header for header, _ in columns]]
    for outcome in outcomes:
        if outcome.answer is None:
            rows.append([outcome.prompt.name, "-", (outcome.error or "")[:30], "-"])
            continue
        passed = f"{sum(check.passed for check in outcome.checks)}/{len(outcome.checks)}"
        review = " ".join(f"{status} {count}" for status, count in review_counts(outcome.answer).items())
        rows.append([outcome.prompt.name, passed, review or "-", f"{outcome.cost_usd:.4f}"])
    total = sum(outcome.cost_usd for outcome in outcomes)
    table = "\n".join(_format_row(row, columns) for row in rows)
    return f"{table}\nTotal: about US${total:.4f} (estimate, list prices)"


def render_tool_counts(noun: str, tool_trace: list[ToolCall]) -> str:
    """ "3 planning tool calls: category_fields 1, sample_descriptions 2" or "0 planning tool calls"."""
    counts = ", ".join(f"{name} {count}" for name, count in tool_call_counts(tool_trace))
    label = f"{len(tool_trace)} {noun}{'' if len(tool_trace) == 1 else 's'}"
    return f"{label}: {counts}" if counts else label


def _indent(text: str) -> str:
    return "\n".join(f"  {line}" for line in text.strip().splitlines())


def render_tool_trace(tool_trace: list[ToolCall]) -> str:
    """A count per tool, then one line per call: '  1.   1.3 s  search_listings  query="nintendo switch" limit=48',
    plus any non-ok status."""
    counts = ", ".join(f"{name} {count}" for name, count in tool_call_counts(tool_trace))
    lines = [f"Tool calls ({len(tool_trace)}){': ' + counts if counts else ''}:"]
    for number, call in enumerate(tool_trace, start=1):
        status = "" if call.status == "ok" else f"  [{call.status}]"
        lines.append(f"{number:>3}. {call.duration_seconds:>5.1f} s  {call.tool_name:<16} {call.arguments}{status}")
    return "\n".join(lines)


def render_kept_listings(kept: list[KeptListing]) -> str:
    """A table with the reason last, so it is never cut; the URL column is as wide as the longest URL."""
    if not kept:
        return "Kept: none."
    url_width = max(len("URL"), *(len(item.url) for item in kept))
    columns = [("PRICE", 9), ("TITLE", 40), ("SELLER", 16), ("URL", url_width), ("REASON", 0)]
    rows = [[header for header, _ in columns]]
    for item in kept:
        price = format_sgd(item.price) if item.price is not None else "-"
        rows.append([price, item.title, item.seller or "-", item.url, " ".join(item.reason.split())])
    return f"Kept ({len(kept)}):\n" + "\n".join(_format_row(row, columns) for row in rows)


def render_dropped_listings(dropped: list[DroppedListing]) -> str:
    if not dropped:
        return "Dropped: none."
    lines = [f"Dropped ({len(dropped)}):"]
    for item in dropped:
        lines.append(f"  {_fit(item.listing_id, 10)}  {_fit(item.title, 44)}  {' '.join(item.reason.split())}")
    return "\n".join(lines)


def render_notes(notes: str) -> str:
    lines = [line.strip() for line in notes.splitlines() if line.strip()]
    if not lines:
        return "Notes: none."
    return "Notes:\n" + "\n".join(f"  {line}" for line in lines)


_DRAFT_TABLE_COLUMNS = [("ID", 6), ("ASKING", 9), ("OFFER", 9), ("SELLER", 18), ("TITLE", 50), ("NOTE", 0)]
_MESSAGE_TABLE_COLUMNS = [
    ("ID", 6), ("STATUS", 9), ("WHEN", 20), ("OFFER", 9), ("SELLER", 18), ("TITLE", 36), ("RUN", 28), ("LINK", 0),
]


def render_outreach_drafts(batch: "DraftBatch") -> str:
    """The drafts as a table, each followed by its message, then what was passed over and the daily allowance."""
    lines = []
    if not batch.from_answer:
        lines.append(
            "This run has no answer.json (it ran before answers were saved), so every listing it saw stands in for "
            "the shortlist, those with details first.\n"
        )
    lines.append(_format_row([header for header, _ in _DRAFT_TABLE_COLUMNS], _DRAFT_TABLE_COLUMNS))
    for draft in batch.drafts:
        cells = [
            str(draft.id), _money(draft.listing_price), _money(draft.offer_price), draft.seller_username or "-",
            draft.listing_title, batch.send_preview.get(draft.id, ""),
        ]
        lines.append(_format_row(cells, _DRAFT_TABLE_COLUMNS))
        lines.append(f"       {draft.body}")
    if not batch.drafts:
        lines.append("(no drafts)")
    if batch.skipped:
        lines.append("\nPassed over:")
        lines += [f"  {listing.listing_id}  {listing.title}: {reason}" for listing, reason in batch.skipped]
    lines.append(f"\n{batch.cap_remaining} new conversations allowed in the next 24 hours.")
    return "\n".join(lines)


def render_send_report(report: SendReport) -> str:
    lines = ["Dry run: nothing was sent and nothing was recorded."] if report.dry_run else []
    if report.aborted:
        lines.append(f"Nothing sent: {report.aborted}")
    if report.stopped:
        lines.append(report.stopped)
    counts = f"{len(report.sent)} {'would be sent' if report.dry_run else 'sent'}, {len(report.skipped)} skipped"
    if report.failed:
        counts += f", {len(report.failed)} failed"
    if report.not_attempted:
        counts += f", {len(report.not_attempted)} left approved for a later batch"
    lines.append(counts + ".")
    left = "would be left" if report.dry_run else "left"
    lines.append(f"Daily cap: {report.cap_remaining} new conversations {left} in the next 24 hours.")
    return "\n".join(lines)


def render_outreach_messages(messages: Sequence[OutreachMessage]) -> str:
    rows = [_format_row([header for header, _ in _MESSAGE_TABLE_COLUMNS], _MESSAGE_TABLE_COLUMNS)]
    for message in messages:
        when = message.sent_at if message.status == "sent" else message.created_at
        link = message.chat_url or message.skip_reason or message.error or message.listing_url or "-"
        cells = [
            str(message.id), message.status, format_datetime(when), _money(message.offer_price),
            message.seller_username or "-", message.listing_title, message.run_id, link,
        ]
        rows.append(_format_row(cells, _MESSAGE_TABLE_COLUMNS))
    if not messages:
        rows.append("(no messages)")
    return "\n".join(rows)


def _money(amount: float | None) -> str:
    return "-" if amount is None else format_sgd(amount)


def format_datetime(moment: datetime | None) -> str:
    return moment.astimezone(SINGAPORE_TIME).strftime("%Y-%m-%d %H:%M SGT") if moment else "-"


def format_date(moment: datetime | None) -> str:
    return moment.astimezone(SINGAPORE_TIME).strftime("%Y-%m-%d") if moment else "-"


def _format_row(cells: list[str], columns: list[tuple[str, int]]) -> str:
    formatted = []
    for cell, (_, width) in zip(cells, columns, strict=True):
        formatted.append(_fit(cell, width) if width else cell)
    return "  ".join(formatted)


def _fit(text: str, width: int) -> str:
    single_line = " ".join(text.split())
    if len(single_line) > width:
        single_line = single_line[: width - 1] + "…"
    return single_line.ljust(width)


def _field(label: str, value: str) -> str:
    return f"{(label + ':') if label else '':<16}{value}"


def _rating_text(detail: ListingDetail) -> str:
    if detail.seller_rating is None:
        return "-"
    return f"{detail.seller_rating:.2f} / 5 from {detail.seller_review_count or 0} reviews"


def _verification_text(verification: SellerVerification) -> str:
    checks = {
        "email": verification.email_verified,
        "mobile": verification.mobile_verified,
        "ID": verification.id_verified,
        "Google": verification.google_verified,
        "Facebook": verification.facebook_verified,
    }
    verified = [name for name, is_verified in checks.items() if is_verified]
    return ", ".join(verified) if verified else "none"


def _yes_no(value: bool | None) -> str:
    return "unknown" if value is None else ("yes" if value else "no")
