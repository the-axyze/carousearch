"""Method templates: generic shapes of a search plan the planner may follow, with no item knowledge in them.

Each template names its slots, gives one sentence the planner instantiates from the request and what discovery
showed, and an example request with <placeholders> that the web app's Ask page offers as a starting point. The
planner picks one (SearchPlan.template) or none. watch_for describes an alert; alerts are not built yet, so it only
shapes a plan.

Pure: constants and lookups.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class MethodTemplate:
    name: str
    description: str  # one line
    slots: tuple[str, ...]
    method: str  # the sentence the planner fills, with {slot} placeholders
    example_request: str  # for the Ask page, with <placeholders> the owner replaces


METHOD_TEMPLATES: tuple[MethodTemplate, ...] = (
    MethodTemplate(
        name="under_benchmark",
        description="Listings priced well below what comparable listings ask.",
        slots=("population", "grouping", "statistic", "normalisation", "threshold", "exclusions", "ranking"),
        method=(
            "Compute the {statistic} of {normalisation} over {population}, grouped by {grouping}; keep listings at "
            "least {threshold} below their group's benchmark after excluding {exclusions}; rank by {ranking}."
        ),
        example_request="Find <item> priced at least <percent>% below the typical asking price for the same "
        "<variant attribute>.",
    ),
    MethodTemplate(
        name="best_under_budget",
        description="The best listings that fit a budget and stated requirements.",
        slots=("population", "budget", "requirements", "quality_signals", "exclusions", "ranking"),
        method=(
            "From {population} at or under {budget}, keep listings that meet {requirements}, excluding "
            "{exclusions}; rank by {quality_signals}, then {ranking}."
        ),
        example_request="Find the best <item> under S$<budget> in <condition> condition with <requirement>.",
    ),
    MethodTemplate(
        name="neglected_listings",
        description="Listings that have sat unsold for a long time, where the seller may accept less.",
        slots=("population", "age_threshold", "engagement_signal", "price_comparison", "exclusions", "ranking"),
        method=(
            "Among {population}, keep listings up for longer than {age_threshold} with little {engagement_signal}; "
            "compare their prices with {price_comparison}; exclude {exclusions}; rank by {ranking}."
        ),
        example_request="Find <item> listings that have been up for more than <days> days with few likes, where "
        "the seller may take a lower offer.",
    ),
    MethodTemplate(
        name="compare_variants",
        description="Typical prices and supply of each variant of an item, side by side.",
        slots=("population", "variant_attribute", "statistic", "minimum_group_size", "exclusions"),
        method=(
            "Group {population} by {variant_attribute}; for each variant with at least {minimum_group_size} "
            "listings, report the count and the {statistic} price and keep its best-value listing; exclude "
            "{exclusions}."
        ),
        example_request="Compare asking prices of <item> by <variant attribute> and show the best-value listing of "
        "each.",
    ),
    MethodTemplate(
        name="watch_for",
        description="An alert condition to check new listings against later (planning only: alerts are not built "
        "yet).",
        slots=("population", "condition", "threshold", "exclusions", "check_interval"),
        method=(
            "Watch new {population} for listings that meet {condition} at {threshold}, excluding {exclusions}; "
            "check every {check_interval}."
        ),
        example_request="Tell me when <item> under S$<budget> with <requirement> is listed.",
    ),
)


def template_names() -> list[str]:
    return [template.name for template in METHOD_TEMPLATES]


def find_template(name: str | None) -> MethodTemplate | None:
    return next((template for template in METHOD_TEMPLATES if template.name == name), None)


def templates_for_planner() -> str:
    """One line per template, for the planning prompt: name, description, slots and the sentence to fill."""
    return "\n".join(
        f"- {template.name}: {template.description} Slots: {', '.join(template.slots)}. Method: {template.method}"
        for template in METHOD_TEMPLATES
    )
