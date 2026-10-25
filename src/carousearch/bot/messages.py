"""Texts the bot sends: help, step questions, the request summary, the session lock reminder, listing
captions, result footers, and error messages. Pure functions and constants.

Nothing here knows about sessions or stages: callers pass in the pieces (a request, a query, a
position such as "at step 2/6 (Price)") so that state.py can use the validation texts below.
"""

from carousearch.carousell.errors import CarousellBlockedError, CarousellSchemaError
from carousearch.filters.payload import format_price
from carousearch.filters.schema import (
    Condition,
    DealMethod,
    Listing,
    ListingDetail,
    SearchRequest,
    SearchResult,
    SortOrder,
)
from carousearch.storage import SavedSearch
from carousearch.text_render import render_listing_detail

# Telegram limits (Bot API): photo captions and message texts.
MAX_CAPTION_LENGTH = 1024
MAX_MESSAGE_LENGTH = 4096

CONDITION_LABELS: dict[Condition, str] = {
    Condition.BRAND_NEW: "Brand new",
    Condition.LIKE_NEW: "Like new",
    Condition.LIGHTLY_USED: "Lightly used",
    Condition.WELL_USED: "Well used",
    Condition.HEAVILY_USED: "Heavily used",
}

DEAL_METHOD_LABELS: dict[DealMethod, str] = {DealMethod.MEETUP: "Meet-up", DealMethod.MAILING: "Mailing"}

SORT_LABELS: dict[SortOrder, str] = {
    SortOrder.BEST_MATCH: "Best match",
    SortOrder.RECENT: "Recent",
    SortOrder.PRICE_LOW_TO_HIGH: "Price low→high",
    SortOrder.PRICE_HIGH_TO_LOW: "Price high→low",
    SortOrder.NEARBY: "Nearby",
}

# Carousell collection id -> short label. A hand-picked subset of the category tree in
# CAROUSELL_ACCESS_NOTES.md section 4.8; any other id can be typed in through "Other id…".
COMMON_CATEGORIES: dict[str, str] = {
    "5707": "Mobile Phones",
    "1793": "Laptops",
    "1241": "Tablets",
    "13": "Furniture",
    "264": "Video Gaming",
    "207": "Audio",
}

HELP_TEXT = """Search Carousell Singapore, one question at a time.

Send keywords (or /search <keywords>) to start. The bot then asks for price, condition, delivery, sort and category, shows a summary, and searches when you tap Search.

/search <keywords>  start a search
/status  where the current search is
/end  finish the current search
/save <name>  save the current search
/saved  list saved searches
/help  this message

One search runs at a time: send /end before starting another."""
REFUSAL_TEXT = "Sorry, this bot is private."

# Step questions.
KEYWORDS_QUESTION_TEXT = "What are you looking for? Send the keywords."
PRICE_QUESTION_TEXT = "What price range (SGD)? Pick one, or type your own."
PRICE_INPUT_PROMPT_TEXT = "Type a price range in SGD, for example 150-400, <250 or >1000."
CONDITION_QUESTION_TEXT = "Which conditions? Tap to select one or more, then Next."
DELIVERY_QUESTION_TEXT = "Meet-up, mailing, or either? Tap to select, then Next."
SORT_QUESTION_TEXT = "How should the results be sorted?"
CATEGORY_QUESTION_TEXT = "Which category?"
CATEGORY_INPUT_PROMPT_TEXT = "Send a Carousell collection id, for example 5707 for Mobile Phones."
SUMMARY_HINT_TEXT = "Tap Search, or edit a filter first."
RESULTS_HINT_TEXT = "Showing results. Tap Next, Refine or End."

# Validation problems with typed text.
KEYWORDS_MISSING_TEXT = "Send some keywords, for example: iphone 15."
CATEGORY_ID_PROBLEM_TEXT = "A collection id is a number, for example 5707 for Mobile Phones. Try again."

# The session lifecycle.
NO_SESSION_TEXT = "There is no search in progress. Send some keywords to start one."
SESSION_EXPIRED_TEXT = "Your previous search was idle for too long and has expired, so this is a new one."
SESSION_EXPIRED_BUTTON_TEXT = "This search has expired. Send some keywords to start a new one."
SESSION_ENDED_TEXT = "Search ended. Send some keywords to start another."
NOTHING_TO_SAVE_TEXT = "There is no search to save yet. Start one by sending some keywords."

SAVE_USAGE_TEXT = "Send /save followed by a name, for example: /save cheap chairs"
NO_MORE_RESULTS_TEXT = "No more results."
SAVED_SEARCH_MISSING_TEXT = "That saved search no longer exists."
NO_SAVED_SEARCHES_TEXT = "No saved searches yet. Use /save <name> to save the current search."
INVALID_BUTTON_TEXT = "That button is no longer valid."
SEEN_TAG = "[seen]"


# --- describing a request -----------------------------------------------------------


def render_request_summary(request: SearchRequest) -> str:
    """Every filter of `request`, one per line."""
    lines = [
        f"Search: {request.query or '(no keywords)'}",
        f"Price: {describe_price_range(request.price_min, request.price_max)}",
        f"Condition: {describe_choices(request.conditions, CONDITION_LABELS)}",
        f"Delivery: {describe_choices(request.deal_methods, DEAL_METHOD_LABELS)}",
        f"Sort: {SORT_LABELS[request.sort]}",
        f"Category: {describe_category(request.category)}",
    ]
    if request.category_fields:
        fields = ", ".join(f"{name}={value}" for name, value in request.category_fields.items())
        lines.append(f"Category fields: {fields}")
    if request.buyer_protection_only:
        lines.append("Buyer Protection only")
    if request.free_delivery_only:
        lines.append("Free delivery only")
    return "\n".join(lines)


def describe_price_range(price_min: float | None, price_max: float | None) -> str:
    if price_min is None and price_max is None:
        return "any"
    if price_min is None:
        return f"up to S${format_price(price_max)}"
    if price_max is None:
        return f"S${format_price(price_min)} and up"
    return f"S${format_price(price_min)} to S${format_price(price_max)}"


def describe_category(category: str | None) -> str:
    if category is None:
        return "any"
    if category in COMMON_CATEGORIES:
        return f"{COMMON_CATEGORIES[category]} ({category})"
    return f"collection {category}"


def describe_choices[Choice](selected: list[Choice], labels: dict[Choice, str]) -> str:
    if not selected:
        return "any"
    return ", ".join(labels[choice] for choice in selected)


# --- step questions and typed-input problems --------------------------------------------


def render_keywords_change_question(current_query: str | None) -> str:
    return f"Current keywords: {current_query or '(none)'}.\nSend new keywords, or tap Next to keep these."


def render_price_range_problem(typed_text: str) -> str:
    return f'Could not read "{typed_text.strip()}" as a price range. Try 150-400, <250 or >1000.'


# --- the session lock -------------------------------------------------------------------


def render_lock_reminder(query: str | None, position: str) -> str:
    """Why a new search was refused. `position` reads like "at step 2/6 (Price)" or "showing results"."""
    subject = f"A search for '{query}'" if query else "A search"
    return (
        f"{subject} is in progress {position}. "
        "Send /end to finish it, or tap Back / Refine on the current message."
    )


def render_session_status(position: str, request: SearchRequest | None) -> str:
    summary = render_request_summary(request) if request is not None else "No keywords yet."
    return f"Search in progress {position}.\n\n{summary}\n\nSend /end to finish it."


# --- results, details and saved searches ------------------------------------------------


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
