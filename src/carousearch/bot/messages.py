"""Texts the bot sends: help, prompts, listing captions, result footers and error messages. Pure functions."""

from pydantic import ValidationError

from carousearch.carousell.errors import CarousellBlockedError, CarousellSchemaError
from carousearch.filters.schema import Listing, ListingDetail, SearchResult
from carousearch.storage import SavedSearch
from carousearch.text_render import render_listing_detail

# Telegram limits (Bot API): photo captions and message texts.
MAX_CAPTION_LENGTH = 1024
MAX_MESSAGE_LENGTH = 4096

HELP_TEXT = """Search Carousell Singapore.

Send keywords (or /search <keywords>) to open a filter panel.
Tap the buttons to set price, condition, delivery, sort and category, then Search.

/save <name>  save the current search
/saved  list saved searches
/help  this message"""
REFUSAL_TEXT = "Sorry, this bot is private."
SEARCH_USAGE_TEXT = "Send /search followed by keywords, for example: /search iphone 15"
SAVE_USAGE_TEXT = "Send /save followed by a name, for example: /save cheap chairs"
NO_DRAFT_TEXT = "There is no search in progress. Send some keywords to start one."
PRICE_PROMPT_TEXT = "Send a price range in SGD, for example 100-500, <300 or 800+."
CATEGORY_PROMPT_TEXT = "Send a Carousell collection id, for example 5707 for Mobile Phones."
NO_MORE_RESULTS_TEXT = "No more results."
CHANGE_NOT_APPLIED_TEXT = "That change could not be applied."
SAVED_SEARCH_MISSING_TEXT = "That saved search no longer exists."
NO_SAVED_SEARCHES_TEXT = "No saved searches yet. Use /save <name> to save the current search."
INVALID_BUTTON_TEXT = "That button is no longer valid."
SEEN_TAG = "[seen]"


def render_listing_caption(listing: Listing, already_seen: bool) -> str:
    likes = "1 like" if listing.like_count == 1 else f"{listing.like_count} likes"
    headline = f"{listing.price_display} · {listing.condition_label or 'condition unknown'} · {likes}"
    lines = [
        f"{SEEN_TAG} {headline}" if already_seen else headline,
        listing.title,
        f"Seller: {listing.seller_username or 'unknown'}",
        listing.listing_url,
    ]
    return truncate("\n".join(lines), MAX_CAPTION_LENGTH)


def render_results_footer(result: SearchResult, page_number: int, shown_count: int) -> str:
    if shown_count == 0:
        return "No listings on this page." if page_number > 1 else "No listings match these filters."
    total = result.total_display or (str(result.total) if result.total is not None else "unknown")
    more = "" if result.next_page else " No more pages."
    return f"Page {page_number}: {shown_count} listings of {total} results.{more}"


def render_detail_message(detail: ListingDetail) -> str:
    return truncate(render_listing_detail(detail), MAX_MESSAGE_LENGTH)


def render_saved_searches_text(saved_searches: list[SavedSearch]) -> str:
    lines = ["Saved searches:"]
    lines.extend(f"- {saved.name}: {saved.request.query or '(no keywords)'}" for saved in saved_searches)
    return "\n".join(lines)


def render_input_problem(error: ValueError) -> str:
    """Why typed price or category text was rejected, plus how to start over instead."""
    if isinstance(error, ValidationError):
        reason = "; ".join(problem["msg"].removeprefix("Value error, ") for problem in error.errors())
    else:
        reason = str(error)
    return f"{reason[:1].upper()}{reason[1:]}. Try again, or send /search <keywords> to start a new search."


def describe_error_for_user(error: Exception) -> str:
    if isinstance(error, CarousellBlockedError):
        return "Carousell is blocking requests right now. Please try again later."
    if isinstance(error, CarousellSchemaError):
        return "Carousell's site seems to have changed, so the results could not be read. Check the canary (carousearch canary)."
    return "Sorry, something went wrong. The error has been logged."


def truncate(text: str, max_length: int) -> str:
    if len(text) <= max_length:
        return text
    return text[: max_length - 1] + "…"
