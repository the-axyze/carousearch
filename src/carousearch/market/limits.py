"""How far a harvest may go: page count, listing age and page size. Pure pydantic model, no I/O.

It lives in its own module so that both `market.analysis` (an `AnalysisSpec` embeds one) and
`market.harvest` can import it without `analysis` depending on the I/O module.
"""

from pydantic import BaseModel, ConfigDict, Field

from carousearch.filters.schema import MAX_PAGE_SIZE


class HarvestLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_pages: int = Field(default=10, ge=1, le=50)
    # Only meaningful with sort=RECENT; `market.harvest.harvest` rejects it otherwise.
    max_age_days: int | None = Field(default=None, ge=1)
    page_size: int = Field(default=MAX_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)
