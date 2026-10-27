"""Search plans for the planning, execution, web and eval tests: one complete plan and a helper to vary it."""

from carousearch.agent.plan import AttributeSpec, BenchmarkSpec, BudgetEstimate, Exclusion, SearchPlan

EXAMPLE_PLAN = SearchPlan(
    goal="Listings at least 30% below the median price of listings of the same size.",
    item_class="a second-hand household item sold in several sizes",
    value_drivers=["size", "condition", "age"],
    attributes=[
        AttributeSpec(name="price", purpose="what is compared", source="card_field", detail="price"),
        AttributeSpec(
            name="size_cm", purpose="groups like with like", source="text_extraction",
            detail=r"stated in titles as '<number> cm'; regex (\d+)\s*cm", unit="cm",
        ),
        AttributeSpec(name="days_listed", purpose="how long it has waited", source="derived",
                      detail="julianday('now') - julianday(posted_at)", unit="days"),
    ],
    benchmark=BenchmarkSpec(population="single-item sale listings with a plain price", grouping=["size_cm"],
                            statistic="median", normalisation=None),
    exclusions=[Exclusion(rule="posts offering several items at one price",
                          reason="their price is not the price of one item")],
    ranking="percent below the group benchmark, largest first",
    template="under_benchmark",
    budget_estimate=BudgetEstimate(searches=2, details=10, photos=0, judgements=1),
    assumptions=["'well below' read as at least 30% below"],
)


def example_plan(**changes) -> SearchPlan:
    return EXAMPLE_PLAN.model_copy(update=changes)
