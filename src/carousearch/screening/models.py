"""Screening results: tags, the resolved price and the screened listing. Pure data models, no I/O.

`ScreenedListing` is what `carousearch search --screen --json` prints and what the web front end will
consume, so the field names here are a contract: add fields, do not rename them.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, computed_field

from carousearch.filters.schema import Listing, ListingDetail


class TagCode(StrEnum):
    MULTI_ITEM_TITLE = "MULTI_ITEM_TITLE"
    PRICE_LIST_IN_DESCRIPTION = "PRICE_LIST_IN_DESCRIPTION"
    LISTED_PRICE_IS_FLOOR = "LISTED_PRICE_IS_FLOOR"
    PRICE_BELOW_MARKET = "PRICE_BELOW_MARKET"
    PRICE_NOT_FOR_YOUR_ITEM = "PRICE_NOT_FOR_YOUR_ITEM"


class Severity(StrEnum):
    INFO = "info"  # worth knowing; the listed price may still be right
    WARN = "warn"  # the listed price probably does not apply to the item searched for


# One entry per TagCode: adding a code means adding it to the enum and to both tables.
TAG_LABELS: dict[TagCode, str] = {
    TagCode.MULTI_ITEM_TITLE: "Multi-item title",
    TagCode.PRICE_LIST_IN_DESCRIPTION: "Price list",
    TagCode.LISTED_PRICE_IS_FLOOR: "Floor price",
    TagCode.PRICE_BELOW_MARKET: "Below market",
    TagCode.PRICE_NOT_FOR_YOUR_ITEM: "Price not for your item",
}
TAG_SEVERITIES: dict[TagCode, Severity] = {
    TagCode.MULTI_ITEM_TITLE: Severity.WARN,
    TagCode.PRICE_LIST_IN_DESCRIPTION: Severity.WARN,
    TagCode.LISTED_PRICE_IS_FLOOR: Severity.WARN,
    TagCode.PRICE_BELOW_MARKET: Severity.INFO,
    TagCode.PRICE_NOT_FOR_YOUR_ITEM: Severity.WARN,
}


class Tag(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: TagCode
    label: str  # short human text, e.g. "Multi-item title"
    reason: str  # one sentence with the evidence, e.g. "title names 3 variants: 15, 15 Pro, 15 Pro Max"
    severity: Severity


def build_tag(code: TagCode, reason: str) -> Tag:
    """A tag with the standard label and severity for its code."""
    return Tag(code=code, label=TAG_LABELS[code], reason=reason, severity=TAG_SEVERITIES[code])


class PriceSource(StrEnum):
    LISTED = "listed"  # the price on the listing card; nothing better was found or looked for
    DESCRIPTION = "description"  # the description states this price for the searched variant
    NOT_STATED = "not_stated"  # the description was read and gives no price for the searched variant


class ResolvedPrice(BaseModel):
    """The best price for the item searched for. Persisted in `price_resolution_cache`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    amount: float | None  # SGD; None when not stated or when the listing has no numeric price
    source: PriceSource
    note: str | None = None


class ScreenedListing(BaseModel):
    listing: Listing
    detail: ListingDetail | None = None  # present when the description was checked
    tags: list[Tag] = Field(default_factory=list)
    resolved_price: ResolvedPrice

    @computed_field
    @property
    def is_multi_item(self) -> bool:
        """True when any tag says the listed price probably does not apply to the searched item."""
        return any(tag.severity == Severity.WARN for tag in self.tags)

    def summary_line(self) -> str:
        """One line for a card, e.g. "Multi-item · listed S$579 · your variant S$850 (description)"."""
        parts = ["Multi-item"] if self.is_multi_item else []
        parts.extend(tag.label for tag in self.tags if tag.severity == Severity.INFO)
        parts.append(f"listed {self.listing.price_display}")
        your_price = describe_your_price(self.resolved_price, self.is_multi_item)
        if your_price:
            parts.append(your_price)
        return " · ".join(parts)


def describe_your_price(resolved: ResolvedPrice, is_multi_item: bool) -> str | None:
    if resolved.source == PriceSource.DESCRIPTION and resolved.amount is not None:
        return f"your variant {format_sgd(resolved.amount)} (description)"
    if resolved.source == PriceSource.NOT_STATED:
        return "no price stated for your variant"
    return "may not be your variant's price" if is_multi_item else None


def format_sgd(amount: float) -> str:
    """S$1,299 for whole dollars, S$12.50 otherwise."""
    return f"S${amount:,.0f}" if float(amount).is_integer() else f"S${amount:,.2f}"
