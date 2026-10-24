"""Stable short hashes for listings and search requests. Pure functions, no I/O.

`listing_content_hash` decides whether a stored listing "changed" (storage.upsert_listings);
`search_request_hash` identifies a search so a recent harvest of the same request can be reused.
"""

import hashlib
import json

from carousearch.filters.schema import Listing, SearchRequest

HASH_LENGTH = 16


def listing_content_hash(listing: Listing) -> str:
    """Hash of (title, price, condition), hex, first 16 characters.

    Likes, thumbnails and timestamps are deliberately left out: likes are demand signal, not a market
    move. The price goes through `float` first so that 750 and 750.0 hash the same.
    """
    price = float(listing.price) if listing.price is not None else None
    condition = listing.condition.value if listing.condition else None
    content = json.dumps([listing.title, price, condition], separators=(",", ":"), ensure_ascii=False)
    return _short_sha256(content)


def search_request_hash(request: SearchRequest) -> str:
    """Hash of the request's filters, hex, first 16 characters; `page_cursor` and `page_size` are ignored.

    Lists are hashed in the order given, so `conditions=[a, b]` and `[b, a]` differ; callers that build
    requests from unordered input must sort enum lists first.
    """
    canonical = request.model_copy(update={"page_cursor": None})
    return _short_sha256(canonical.model_dump_json(exclude={"page_cursor", "page_size"}))


def _short_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:HASH_LENGTH]
