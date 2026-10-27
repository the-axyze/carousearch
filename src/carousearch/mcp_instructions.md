# Carousearch: usage notes

These tools search Carousell Singapore (carousell.sg) through carousearch's client and return Carousell's data as
JSON. They do no filtering, scoring or ranking: you do that yourself, over the JSON.

## Recommended order

1. `search_listings` with Carousell's own filters (keywords, price range, condition, meet-up or mailing, sort,
   category). Cheap: one Carousell request per page of up to 48 listings. For the next page, call it again with the
   same request and `page_token` set to the previous `next_page`. `fields` (for example `["id", "title", "price"]`)
   keeps each listing short; `description_snippet` is the start of each description as the card shows it. When
   the category matters, `category_fields` first: it lists the categories, a category's subcategories, and the
   filters Carousell offers there with their allowed values. `sample_descriptions` shows one page as short text
   (description excerpts and seller-entered fields), to learn what sellers in a category actually state.
2. Filter and rank the listings yourself: regexes over titles, prices, conditions and sellers. This costs nothing.
3. `listing_details` only for your shortlist: description, all photo URLs, seller rating and reviews, meet-up points.
   One request per listing (details fetched in the last 12 hours come from a cache).
4. `listing_photo` last, only for listings still in the running: one request per photo. A listing's `thumbnail_url`
   is the small cover photo buyers see in search results and needs no `listing_details` call; the `image_urls` from
   `listing_details` are the full-size photos.

`category_fields()` with no category costs nothing; with a category id it is one Carousell request.
`sample_descriptions` is one Carousell request, plus one per detail you let it fetch (`max_detail_fetches`, 0 by
default).

`screening_tags` (optional) returns carousearch's rule-based tags for one listing, such as a title that names several
models or sizes (a multi-item post whose price is usually the cheapest variant's). It uses the same cached detail as
`listing_details`.

## Etiquette

- Keep one task under about 40 Carousell requests in total: search pages, details and photos together. The first
  search or details call of a session also loads the Carousell home page once, for cookies.
- Requests are spaced about one second apart (1.0 s plus or minus 0.3 s). The server enforces this, so many calls in
  a row are slow, not faster.
- If a tool says Carousell is blocking or rate-limiting, stop and tell the user instead of retrying.

## JSON shapes

- `search_listings` -> `{"listings": [listing, ...], "total": int or null, "next_page": str or null}`.
  Each listing: `id`, `title`, `price` (SGD number, or null when the price is not a plain number), `currency`,
  `price_display` ("S$1,400"), `condition` (brand_new, like_new, lightly_used, well_used, heavily_used, or null for
  old listings), `condition_label` (Carousell's wording), `like_count`, `seller_username`, `seller_id`,
  `thumbnail_url`, `listing_url`, `posted_or_bumped_at` (ISO 8601), `posted_or_bumped_kind` (time_created,
  active_bump or expired_bump), `description_snippet` (or null). `total` is capped by Carousell: 1000 means
  "1,000+".
- `listing_details` -> the listing fields plus `description`, `image_urls`, `created_at`, `last_modified_at`,
  `seller_rating` (out of 5), `seller_review_count`, `seller_joined_at`, `seller_response_rate` ("H" is high),
  `seller_verification`, `meetup_available`, `mailing_available`, `meetup_locations` (`name`, `address`,
  `latitude`, `longitude`), `category_id` and `category_name` (the listing's own category), and `attributes` (the
  seller-entered category fields by field name, as displayed). Its `thumbnail_url` and `posted_or_bumped_at` are
  null; use `image_urls` and `created_at`.
- `listing_photo` -> one image (JPEG, PNG or WebP), at most `max_width` pixels wide.
- `screening_tags` -> `{"listing_id": str, "tags": [{"code", "label", "reason", "severity"}]}`; severity "warn"
  means the listed price probably does not apply to one specific item.
- `category_fields` -> without a category `{"categories": [{"id", "name"}], "tree_captured_on"}`; with one
  `{"category": {"id", "name", "path"}, "subcategories": [{"id", "name"}], "filters": [{"field_name", "label",
  "type", "values": [{"value", "label"}], "set_with"}], "tree_captured_on"}`. The tree is a snapshot; the filters
  are live.
- `sample_descriptions` -> `{"samples": [{"id", "title", "price", "description_excerpt", "excerpt_source",
  "structured_fields"}], "total", "with_full_description"}`; `excerpt_source` is "detail", "card" or "none".
