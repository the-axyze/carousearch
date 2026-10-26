# Carousell access notes (raw technical findings)

All observations below were made on **2026-10-23** (Singapore time, roughly 16:35 to 17:00) from the user's home machine (WSL2 on Windows, Singapore residential connection, Cloudflare edge `SIN`). Tools: `curl 8.18.0` and the Python 3.14 standard library (`urllib`). No Carousell account and no login were used. About 60 requests were sent to carousell.sg in total, spaced 1.5 to 2 seconds apart during the bursts. No rate limiting was hit.

These notes record what was observed. Undocumented internal endpoints can change without notice. Anything marked **(inferred)** was not tested directly.

---

## 1. Summary of what works

| What | How | Status 2026-10-23 |
|---|---|---|
| Search results, filters applied | `POST https://www.carousell.sg/ds/search/cf/4.0/search/` (JSON) with session cookies + `csrf-token` header | Works (200, JSON) |
| Search results **plus the category's filter definitions** | `POST https://www.carousell.sg/ds/filter/cf/4.0/search/` (same body) | Works (200, JSON, includes `filters` fieldset) |
| Search results from HTML (server-side rendered) | `GET https://www.carousell.sg/search/<query>?<filters>`, parse `<script type="application/json">` (the `initialState`) | Works for most queries. Gotcha: see section 7.1 |
| Listing detail (description, all photos, seller stats) | `GET https://www.carousell.sg/ds/listing-detail/3.1/listings/<id>/detail/?country_code=SG` | Works (200, JSON) |
| Listing detail from HTML | `GET https://www.carousell.sg/p/<id>/` (redirects to `/p/<slug>-<id>/`), parse `initialState.Listing.listingsMap` | Works |
| Search suggestions (keyword to category hint) | `GET https://www.carousell.sg/ds/search-suggestions/2.5/search-suggestions/suggestions/?country_code=SG&query=...` | Works |
| Location lookup (MRT / area / neighbourhood IDs) | `GET https://www.carousell.sg/ds/location-filter-search/1.0/filters/search?country_id=1880251&query=tampines` | Works |
| Listing images | `https://media.karousell.com/media/photos/products/...` (public CDN) | Works with no cookies and no browser User-Agent |
| Legacy endpoint used by old scrapers | `POST https://www.carousell.sg/api-service/filter/cf/4.0/search/` | **Broken**: 404 even with cookies + CSRF |
| Official public API | none found | Not available (see FEASIBILITY.md section 1.1) |

---

## 2. Anti-bot behaviour observed

| Request | Result |
|---|---|
| `GET /search/sofa` with curl's default User-Agent | `403`, `cf-mitigated: challenge`, Cloudflare "Just a moment..." page |
| `GET /search/sofa` with `User-Agent: python-requests/2.32.3` | `403` Cloudflare challenge |
| `GET /search/sofa` with a desktop Chrome User-Agent (curl) | `200`, ~2.3 MB HTML |
| Same with Python stdlib `urllib` + Chrome User-Agent (different TLS stack from curl) | `200` |
| `POST /ds/search/cf/4.0/search/`, Chrome UA, **no cookies, no CSRF** | `422`, empty body |
| `POST /ds/search/cf/4.0/search/`, Chrome UA, cookies + `csrf-token` from a prior page load | `200` JSON |
| `POST /api-service/filter/cf/4.0/search/` (legacy), python-requests UA | `403` Cloudflare challenge |
| `POST /api-service/filter/cf/4.0/search/` (legacy), Chrome UA + cookies + CSRF | `404` (endpoint gone) |
| 12 listing-detail calls at 1.5 s spacing, then 6 searches at 2 s spacing | all `200`, 0.37 to 1.1 s each |
| Support site page `support.carousell.com/hc/en-us/articles/...` (HTML) | `403` Cloudflare challenge even with Chrome UA |
| Support site Zendesk JSON API `support.carousell.com/api/v2/help_center/...` | `200` (used to read the ToS) |

Blocked response headers (trimmed):

```
HTTP/2 403
cf-mitigated: challenge
server: cloudflare
cf-ray: a4ef85a4fa6ea092-SIN
```

Successful page load sets these cookies (values trimmed): `_t`, `_t2`, `siv_2`, `_csrf` (HttpOnly, 7-day max age). The CSRF token to send as a header is the `Application.csrfToken` value inside the page's `initialState` JSON.

Not observed: login walls for search or listing detail, CAPTCHAs on the JSON API, DataDome, or per-IP rate-limit responses (429). Not tested: high request rates, datacenter/VPS IPs, behaviour over days.

Third-party evidence: the Apify actor `automation-lab/carousell-listings-scraper` says Carousell "commonly serves Cloudflare verification to direct and datacenter traffic" and recommends residential proxies (https://apify.com/automation-lab/carousell-listings-scraper, accessed 2026-10-23). The open-source `hoelee/carousell-monitor` notes a **soft block** where the page returns HTTP 200 but `SearchListing.listingCards` is `null` (comment in `monitor.py`, accessed 2026-10-23).

---

## 3. Web search URLs

### 3.1 URL structure

```
https://www.carousell.sg/search/<url-encoded query>?<params>
https://www.carousell.sg/categories/<slug>-<id>/?search=<query>&<params>
https://www.carousell.sg/categories/<parent-slug>-<parent-id>/<slug>-<id>/          (canonical form after redirect)
https://www.carousell.sg/p/<listing-id>/      -> 301 to /p/<title-slug>-<listing-id>/
```

Example that worked and applied every filter (SSR, 34 results):

```
https://www.carousell.sg/search/herman%20miller%20aeron?addRecent=true&canChangeKeyword=true&includeSuggestions=true&layered_condition=3%2C4&price_start=300&price_end=900&sort_by=3&_deal_method=MEETUP
```

Server-side, the page turned those query params into this request (from `initialState.SearchListing.parameters`):

```json
{
  "fieldsetParam": {"_deal_method": "MEETUP", "layered_condition": "3,4",
                    "price_end": "900", "price_start": "300", "sort_by": "3"},
  "filters": [
    {"idsOrKeywords": {"value": ["MEETUP"]}, "fieldName": "_deal_method", "enforce": true},
    {"idsOrKeywords": {"value": ["3", "4"]}, "fieldName": "layered_condition", "enforce": true},
    {"rangedFloat": {"start": {"value": "300"}, "end": {"value": "900"}}, "fieldName": "price", "enforce": true}
  ],
  "query": "herman miller aeron"
}
```

### 3.2 Query parameters

| Param | Meaning | Values seen |
|---|---|---|
| `addRecent`, `canChangeKeyword`, `includeSuggestions` | UI flags added by the site's search box | `true` / `false`; not needed for results |
| `price_start`, `price_end` | price range in SGD | numbers |
| `layered_condition` | condition, comma-separated | `3` Brand new, `4` Like new, `7` Lightly used, `5` Well used, `6` Heavily used |
| `sort_by` | sort | `1` Best match, `3` Recent, `4` Price low to high, `5` Price high to low, `6` Nearby (needs location) |
| `_deal_method` | deal method | `MEETUP`, `MAILING` |
| `_delivery` | delivery available | boolean |
| `is_bp_enabled` | Buyer Protection | boolean |
| `shipping_offer_free_shipping` | free delivery | boolean |
| `tab` | `marketplace` or `certified` | `tab=marketplace` forces normal listings into the SSR HTML (see 7.1) |
| category facet names | e.g. `mobile_storage=MOBILE_STORAGE_256_GB` | accepted on `/categories/...` URLs; the SSR page did **not** render cards for that URL (client-loaded), the JSON API is the reliable path |
| `search` | keyword on `/categories/...` URLs | string |

`robots.txt` (fetched 2026-10-23) disallows `/search/` and `/*?` for `User-agent: *`, but has an `Allow: /` block for named AI agents (GPTBot, ChatGPT-User, ClaudeBot, Claude-User, PerplexityBot, and others). robots.txt is a crawler convention, not a licence; the Terms of Service still apply.

```
User-agent: *
Disallow: /search/
Disallow: /*?
Allow: /api-service/*?
Allow: /smart_render/*?
...
User-agent: GPTBot
User-agent: ClaudeBot
User-agent: Claude-User
...
Allow: /
```

---

## 4. Internal JSON endpoints

### 4.1 How the web client calls them

From the site's JavaScript bundle (`https://mweb-cdn.karousell.com/build/7024b4d618a2f803.min.js`, fetched 2026-10-23), every data-service call is built as:

```
URL     = /ds/<service-name><path>
query   = { _path: <path>, l: <locale>, ...extra }
headers = { "csrf-token": <Application.csrfToken>, "y-<header>": <value>, "x-telemetry-id": (optional) }
```

Search is called as service `search` (normal) or `filter` (when the page also needs the filter definitions), path `/cf/4.0/search/`, method POST.

### 4.2 Search: request

```
POST https://www.carousell.sg/ds/search/cf/4.0/search/?_path=%2Fcf%2F4.0%2Fsearch%2F&l=en
Content-Type: application/json
csrf-token: <token from page>
Cookie: <cookies from page load>
User-Agent: <desktop Chrome UA>
```

```json
{
  "bestMatchEnabled": true,
  "canChangeKeyword": false,
  "count": 20,
  "countryCode": "SG",
  "countryId": "1880251",
  "filters": [
    {"fieldName": "collections", "idsOrKeywords": {"value": ["5707"]}, "enforce": true},
    {"fieldName": "mobile_model", "idsOrKeywords": {"value": ["MOBILE_MODEL_IPHONE_15_PRO"]}, "enforce": true},
    {"fieldName": "mobile_storage", "idsOrKeywords": {"value": ["MOBILE_STORAGE_256_GB"]}, "enforce": true},
    {"fieldName": "price", "rangedFloat": {"start": {"value": "600"}, "end": {"value": "900"}}, "enforce": true},
    {"fieldName": "_deal_method", "idsOrKeywords": {"value": ["MEETUP"]}, "enforce": true}
  ],
  "includeSuggestions": false,
  "locale": "en",
  "prefill": {},
  "query": "iphone 15 pro",
  "sortParam": {"fieldName": "4"}
}
```

Result: 279 listings, every filter echoed back as applied, cheapest first (S$600, S$600, S$600, S$620, ...).

Filter grammar:

| Filter type (fieldset `filter_type`) | JSON shape |
|---|---|
| `id_or_keyword` (multi-pickers, category, deal method, condition, brand, model, storage...) | `{"fieldName": F, "idsOrKeywords": {"value": [..]}, "enforce": true}` |
| `float_range_start` / `float_range_end` (price) | `{"fieldName": "price", "rangedFloat": {"start": {"value": "300"}, "end": {"value": "900"}}}` |
| `float_range` (e.g. car `vehicle_age`, option values like `",5"` or `"5,10"`) | **(inferred)** same `rangedFloat` shape with open ends |
| `boolean` (`_delivery`, `is_bp_enabled`, `shipping_offer_free_shipping`) | **(inferred)** `{"fieldName": F, "boolean": {"value": true}}`; not tested, use the URL param form via SSR if this fails |
| category | `fieldName: "collections"`, value = collection **id** (not `cc_id`) |
| sort | top-level `sortParam: {"fieldName": "<sort value>"}` |

Pagination: the response contains `session` (a base64 string). Send it back as `"session": "<value>"` with the same body to get the next page. Tested: page 1 returned 20 ids, page 2 returned 16 new ids, zero overlap, total 36. The web client's default `count` is 48 (from the JS bundle); 20 and 40 were tested.

### 4.3 Search: response shape (trimmed)

```json
{"data": {
  "appliedContext": {"filters": [...], "query": "herman miller aeron"},
  "filterBubbles": [{"label": "Condition", "displayValue": "Brand new, Like new", "fieldName": "layered_condition"}, ...],
  "formattedTotal": "36",
  "total": {"value": {"low": 36, "high": 0, "unsigned": false}},
  "hasMore": true,
  "session": "eyJhZ2dyZWdhdGVfY291bnQiOjIwLC...",
  "sfContext": {"collectionId": "5755"},
  "searchContext": "0a0208141a04...",
  "filters": { ...fieldset, only from the /ds/filter/ variant... },
  "results": [
    {"listingCard": {
      "id": "1379060933",
      "title": "Genuine Size B and C Fully loaded Herman Miller Aeron Chair with new headrest ",
      "price": "S$680",
      "condition": 4,
      "likesCount": 462,
      "status": "L",
      "countryCollectionId": "5755",
      "seller": {"id": "33042747", "username": "kevin.goodchair", "firstName": "Kevin Genuine Great Chair",
                 "profilePicture": "https://media.karousell.com/media/photos/profiles/..."},
      "photoUrls": ["https://media.karousell.com/media/photos/products/2026/8/12/genuine_size_b_and_c_fully_loa_1786506792_e9babd1b_thumbnail"],
      "aboveFold": [{"component": "active_bump", "timestampContent": {"seconds": {"low": 1792734669, "high": 0}}}],
      "belowFold": [
        {"component": "header_1", "stringContent": "Genuine Size B and C Fully loaded ..."},
        {"component": "header_2", "stringContent": "S$680"},
        {"component": "paragraph", "stringContent": "Herman Miller Aeron chair delivery $35-$40 condition very good, ... (first ~250 chars of description)"},
        {"component": "paragraph", "stringContent": "Like new"}
      ],
      "tags": [{"content": "Buyer Protection", ...}],
      "marketPlace": {"name": "Singapore", "location": {"latitude": 1.2897, "longitude": 103.8501}},
      "ctaButtons": ["chat"],
      "isSellerVisible": true
    }},
    {"banner": {...}}
  ]
}}
```

Notes:
- Numbers come as protobuf-style `{low, high, unsigned}` objects for 64-bit values; prices come as display strings (`"S$680"`, `"S$648.88"`).
- `aboveFold[0].component` is one of `time_created`, `active_bump`, `expired_bump` (counts across 96 sampled cards: 64 / 21 / 11). Its `seconds` is a Unix timestamp. This is the only "age" signal on the card.
- `seller.sellerType` is sometimes present: `"Professional Seller"` on 41 of 96 sampled cards.
- `tags` seen: `Buyer Protection`, and a Carousell Certified logo image tag.
- `results` can contain non-listing items (`banner` seen).
- A search with no matches omits `results` and `hasMore` entirely and sends `"total": {}` (zero omitted, protobuf-style) with `"formattedTotal": "0"` (seen 2026-10-27, `tests/fixtures/search_no_results.json`). The parser reads that as zero listings; a missing `results` with a missing or nonzero `total` is still a schema error.
- `cardType` values seen: absent, 3, 9, 13. Meaning unknown.
- `sfContext.collectionId` is a **cc_id** (e.g. laptops: id 1793, cc_id 2195; the response said `2195`). Carousell auto-narrows the category from the query: "iphone 15 pro" in Mobile Phones (5707) came back with `sfContext.collectionId = 6359` ("iPhone 15 Series").

### 4.4 Filter definitions (fieldset) for a category

Call the same body against `/ds/filter/cf/4.0/search/` with a `collections` filter. The response's `filters` object is a "fieldset": `screens[].groups[].fields[]`, each field with `meta.field_name`, `meta.component`, `meta.filter_type` and `ui_rules.options[] {display_name, value}`.

The direct fieldset endpoint `GET /ds/smart-fields/1.0/fieldset/collection/5707/?journey=search` returned a generic **fallback** fieldset (`"id": "fallback"`, condition as NEW/USED, sort values like `time_created,descending`). Use the search-response fieldset instead.

Facets observed 2026-10-23 (values trimmed):

**Global (every category seen):**

| field_name | type | options |
|---|---|---|
| `sort_by` | radio | Best Match `1`, Recent `3`, Price high to low `5`, Price low to high `4`, Nearby `6` |
| `_deal_method` | multi | Delivery (with Buyer Protection) `MAILING`, Meet-up `MEETUP` |
| `layered_condition` | multi | Brand new `3`, Like new `4`, Lightly used `7`, Well used `5`, Heavily used `6` |
| `price_start` / `price_end` | number | S$ |
| `is_bp_enabled` | checkbox | Buyer Protection |
| `shipping_offer_free_shipping` | checkbox | Free delivery |
| `_delivery` | boolean | Delivery (bubble on some categories) |
| `location_ids` | location picker | see 4.7 |

**Mobile Phones (collection 5707, query "iphone 15 pro"):**

| field_name | options (trimmed) |
|---|---|
| `mobile_model` | `MOBILE_MODEL_IPHONE_15`, `MOBILE_MODEL_IPHONE_15_PLUS`, `MOBILE_MODEL_IPHONE_15_PRO`, `MOBILE_MODEL_IPHONE_15_PRO_MAX` (options depend on the auto-narrowed sub-category) |
| `mobile_storage` | `MOBILE_STORAGE_128_GB`, `_256_GB`, `_512_GB`, `MOBILE_STORAGE_1_TB` |
| `mobile_color` | Black, Green, Yellow, Pink, Blue, Blue/Natural/Black/White Titanium (`MOBILE_COLOR_*`) |
| `mobile_free_accessories` | Wall plug, Charging cable, Box, Screen protector, Tempered glass, Phone case |
| `mobile_nationality` | `MOBILE_NATIONALITY_LOCAL`, `MOBILE_NATIONALITY_INTERNATIONAL` |
| `mobile_manufacturer_warranty` | `..._YES`, `..._NO` |

**Chairs (collection 5755, query "herman miller aeron"):**

| field_name | options |
|---|---|
| `chairs_type` | Armchairs, Office & gaming chairs, Massage chairs, Benches, Stools & bar stools, Bean bags & poufs, Dining chairs, Others (`CHAIR_TYPE_*`) |
| `furniture_size` | Up to 1.2m, More than 1.2m |
| `furniture_material` | Concrete, Stone, Marble, Wood, Others |

**Laptops (collection 1793):** `brand_enum` (`BRAND_APPLE`, `BRAND_ASUS`, `BRAND_DELL`, ... 22 brands).

**Men's Footwear (collection 174):** `brand_enum` (Adidas, Asics, Converse, ...), `size` (`MEN_FOOTWEAR_SIZE_EU_40` ... EU/UK/US labels).

**Used Cars (collection 1173):** `seller_badge`, `body_type`, `style_type`, `make`/`model` (SKU pickers), `vehicle_age`, registration date range, `depreciation`, `monthly_installment`, `propellant`, `transmission`, `doors`, `owner_count`, `seating_capacity`, plus car-specific sort values (`original_registration_date,descending`, depreciation).

**Not available as a server-side filter anywhere seen:** listing age / "posted within N days", seller rating, like count, seller join date. These must be applied client-side (sort by Recent and cut off on the card timestamp; rating and join date need the detail call).

### 4.5 Listing detail

```
GET https://www.carousell.sg/ds/listing-detail/3.1/listings/1379060933/detail/?country_code=SG&_path=%2F3.1%2Flistings%2F1379060933%2Fdetail%2F&l=en
csrf-token: <token>
y-build-no: 1
```

Response: `data.screens[0].meta.default_value` holds the listing object (~25 to 50 KB per listing). Same object as `initialState.Listing.listingsMap["<id>"]` on the HTML page. Fields (trimmed values):

```json
{
  "id": 1379060933, "title": "...", "price": "680.00", "currency_code": "SGD",
  "description": "Herman Miller Aeron chair\ndelivery $35-$40\n\ncondition very good, ...",
  "condition": "1", "layered_condition": 4,
  "likes_count": 462, "comments_count": 0, "offer_count": 0,
  "status": "L", "is_free_item": false, "is_popular": false,
  "time_created": "2025-07-06T12:11:10Z", "last_modified": "2026-10-23T05:51:09Z",
  "status_updated": "2025-12-08T00:49:21Z", "time_indexed": "2026-10-23T05:51:09Z",
  "meetup": true, "mailing": false,
  "meetups": [{"name": "Admiralty MRT Station (NS10)", "address": "70 Woodlands Ave. 7, 738344", "latitude": 1.4406, "longitude": 103.8010}],
  "photos": [{"image_url": "https://media.karousell.com/media/photos/products/2026/8/12/genuine_size_b_and_c_fully_loa_1786506792_e9babd1b", "is_primary": true, ...}, ... 8 photos],
  "listing_attributes": {"category": "In {{{Chairs}}}", "chairs_type": "Office & gaming chairs", "condition": "Like new", "time_created": "2 hours ago by {{{kevin.goodchair}}}"},
  "smart_attributes": {"deal_options": "MEETUP", "fixed_price": "false", ...},
  "flags": {"feedback_count": 100, "feedback_score": 4.98, "seller_type": "", ...},
  "seller": {
    "id": 33042747, "username": "kevin.goodchair",
    "date_joined": "2021-11-16T01:26:26Z",
    "response_rate": "H",
    "feedback_score": 4.98, "feedback_count": 100,
    "positive_reviews_count": 99, "neutral_reviews_count": 1, "negative_reviews_count": 0,
    "followers_count": 45, "following_count": 3,
    "is_suspended": false, "blocked": false,
    "profile": {"is_email_verified": true, "is_mobile_verified": true, "is_id_verified": false,
                "is_gplus_verified": true, "is_facebook_verified": false, "verification_type": "VE", ...}
  }
}
```

The HTML listing page also carries `Feedback.sessions[<seller id>].complimentScores` (review compliments like "Great communication: 36").

### 4.6 Search suggestions

```
GET https://www.carousell.sg/ds/search-suggestions/2.5/search-suggestions/suggestions/?country_code=SG&query=iphone%2015%20pro&_path=%2F2.5%2Fsearch-suggestions%2Fsuggestions%2F&l=en
```

```json
{"data": {"query": "iphone 15 pro", "suggestions": [
  {"suggestion": "iphone 15 pro", "suggested_collections": [{"id": 6359, "name": "iPhone 15 Series", "parent_display_names": []}]},
  {"suggestion": "iphone 15 pro max", "suggested_collections": []},
  {"suggestion": "iphone 15 pro max 256gb", "suggested_collections": []}]}}
```

Useful as a free category hint before involving the LLM.

### 4.7 Location filter

```
GET https://www.carousell.sg/ds/location-filter-search/1.0/filters/search?country_id=1880251&query=tampines&adm_ids=&_path=%2F1.0%2Ffilters%2Fsearch&l=en
```

Returns groups `MRT`, `Area`, `Neighbourhood`, each location with `id`, `display_name`, and `filters` such as `{"key": "adm_l2_location_ids", "values": [46718]}` and `{"key": "coverage_location_ids", "values": [46666, 46668, 46718]}`.

Test: query "sofa" with no location filter returned "10,000+"; with `{"fieldName": "adm_l2_location_ids", "idsOrKeywords": {"value": ["46718"]}}` (Tampines area) it returned "1,000+", so the filter appears to apply. `coverage_location_ids` with a single ID returned 1 result, so it probably needs the full value list. **Exact semantics not fully verified.**

Singapore constants: `countryId` 1880251, marketplace id 1880252, `countryCode` SG.

### 4.8 Category tree

The HTML `initialState.Collection.default` contains the full tree: 725 collections, 34 top-level categories (`parentCatIDs`), each with `id`, `cc_id`, `slug`, `name`, `parentID`, `ancestorIDs`. `id` and `cc_id` are usually equal but not always (Laptops 1793 / 2195, Tablets 1241 / 1643, Mobile Phones & Gadgets 215 / 363). Store both.

Top-level (id | slug): 12 toys-collectibles, 32 cars, 1306 home-services, 102 property, 213 computers-tech, 215 mobile-phones-gadgets, 4 womens-fashion, 3 men-s-fashion, 11 beauty-personal-care, 20 luxury, 1898 free-items, 264 video-gaming, 207 audio, 6 photography, 13 furniture-home-living, 109 car-accessories, 30 tv-home-appliances, 2007 car-workshops-services, 19 babies-kids, 108 motorcycles, 5953 health-nutrition, 10 sports-equipment, 196 food-drinks, 29 pet-supplies, 18 tickets-vouchers, 1742 learning-enrichment, 5934 hobbies-lifestyle, 1916 lifestyle-services, 1747 business-services, 1305 jobs, 26 bulletin-board, 16 everything-else (plus 1 following, 1899 announcements).

Example children of Mobile Phones & Gadgets (215): 5707 Mobile Phones, 1241 Tablets, 5720 E-Readers, 5721 Wearables & Smart Watches, 216 Mobile & Gadget Accessories. Children of Mobile Phones (5707): 1235 iPhone, 1237 Android Phones, 5715 Early Generation Mobile Phones. iPhone 15 Series is 6359 (ancestors 215 > 5707 > 1235).

`PageCollection.purlMeta.collectionFiltersMapForDecoding` (606 collections) maps "pretty URL" slugs to filters, e.g. `brand-xiaomi` -> `brand_enum = BRAND_XIAOMI` for laptops.

---

## 5. HTML (server-side rendered) pages

Every page embeds the full Redux state:

```html
<script type="application/json">{"Application":{"csrfToken":"76be8e2d...","isBot":false,...},"SearchListing":{...},...}</script>
<script>window.initialState = JSON.parse(document.currentScript.previousSibling.textContent); ...</script>
```

There is no `__NEXT_DATA__` (the site is a custom React + redux-saga app with loadable chunks). The JSON blob is 1.7 to 2 MB per search page. Relevant keys:

| Page | Key | Content |
|---|---|---|
| search | `SearchListing.listingCards` | up to ~50 cards (same shape as API `listingCard`, plus `seller.sellerType`, `media[]`, `thumbnailURL`) |
| search | `SearchListing.parameters` | the exact API request the server made (useful to learn param names) |
| search | `SearchListing.filterBubbles`, `Fieldset.fieldsetMap.search` | applied filters and the category's facet definitions |
| search | `SearchProductListing` | Carousell "Certified" product results (catalogue-style cards with an "Inspected" ribbon) |
| search | `Collection.default` | full category tree |
| search | `Application.csrfToken` | CSRF token for the JSON API |
| listing | `Listing.listingsMap[<id>]` | full listing incl. seller stats |
| listing | `Feedback.sessions[<seller id>]` | review compliments |

Observed sizes: search page ~2.0 to 2.4 MB HTML, listing page ~0.5 MB HTML, JSON search response 44 to 60 KB for 20 cards, listing detail JSON 24 to 49 KB.

---

## 6. Images

| Variant | URL suffix | Size observed |
|---|---|---|
| Full | none (`.../genuine_size_b_and_c_fully_loa_1786506792_e9babd1b`) | 810 x 1080 px, 123 KB JPEG |
| Thumbnail | `_thumbnail` | 240 x 320 px, 10 KB JPEG |
| Progressive | `_progressive`, `_progressive_thumbnail` | progressive JPEG variants |

Search cards give one thumbnail URL. The listing detail gives every photo (8 in the sample). The CDN responded without cookies or a browser User-Agent.

---

## 7. Gotchas

1. **Certified tab hides normal results in SSR.** `GET /search/iphone%2015?...` rendered the "Certified" product tab: `SearchListing.listingCards` was `null` with `pending: true` (normal listings load client-side). Adding `tab=marketplace` returned 49 SSR cards. The JSON API is not affected.
2. **Soft block looks like an empty result.** Per `hoelee/carousell-monitor`, a challenged session can return HTTP 200 with `listingCards: null`. Treat "null cards" differently from "0 results" (`numberOfResults: 0`).
3. **Category pages do not SSR cards.** `/categories/mobile-phones-5707/?search=...` returned `pending: true` and no cards. Use the JSON API for category-scoped searches.
4. **id vs cc_id.** The `collections` filter takes the collection id; `sfContext.collectionId` returns a cc_id.
5. **Carousell may change your query.** The response has `shouldPromptKeywordCorrected`; with `canChangeKeyword: true` the site may rewrite misspellings. Send `canChangeKeyword: false` for predictable results.
6. **Auto-narrowing.** The search may narrow to a deeper category than requested (`sfContext`), which changes which facets are available. Read the fieldset from the same response you will filter.
7. **Query strings inside JSON must be plain text**, not URL-encoded.
8. **CSRF/cookie bootstrap is required.** Load any HTML page first, keep the cookie jar, read `Application.csrfToken`, send it as `csrf-token`. The `_csrf` cookie lasts 7 days.

---

## 8. Existing open-source and commercial scrapers (checked 2026-10-23)

| Project | Language, method | Last push | Status / notes |
|---|---|---|---|
| [kokseen1/wall-e](https://github.com/kokseen1/wall-e) | Python, `requests.post` to `/api-service/filter/cf/4.0/search/`, no cookies | 2022-09-08 | **Broken**: that path now returns 403 (python-requests UA), 422 (browser UA, no cookies) or 404 (browser UA + cookies + CSRF). Its request body is the same shape as today's `/ds/` body, which helps. |
| [dexterleng/carousell-telebot](https://github.com/dexterleng/carousell-telebot) | Python, Telegram notifier, old `sg.carousell.com/search/products/` URL | 2022-12-08 | Almost certainly broken (old domain/URL). |
| [hoelee/carousell-monitor](https://github.com/hoelee/carousell-monitor) | Python stdlib, parses SSR `<script type="application/json">` -> `SearchListing.listingCards`; Telegram + NocoDB; carousell.com.my | 2026-10-06 | Recent; same SSR technique verified here; handles soft blocks. Closest reference for an HTML fallback. |
| [theboi/carousell-monitoring](https://github.com/theboi/carousell-monitoring) | TypeScript, Puppeteer, intercepts responses matching `/api-service/` or `/api/` | 2026-10-23 | Active, but its interceptor patterns do not match today's `/ds/` paths **(inferred)**, so it probably relies on its DOM fallback. |
| [Ken69420/carousell-deal-scanner](https://github.com/Ken69420/carousell-deal-scanner) | JavaScript, Playwright, Telegram, carousell.com.my | 2026-10-18 | Active; headless-browser approach. |
| [LuckyProgramme/carousell_listing_scraper](https://github.com/LuckyProgramme/carousell_listing_scraper) | Python requests + BeautifulSoup (`data-testid` selectors), carousell.ph, Gemini "auditor" for deal checks | 2026-10-03 | Active; a precedent for LLM screening of listings. |
| [mirfsz/Carousell-Telegram-Bot](https://github.com/mirfsz/Carousell-Telegram-Bot) | Python, Playwright, Telegram | 2024-08-01 | Unmaintained. |
| [jiakai2002/carousell-scraper](https://github.com/jiakai2002/carousell-scraper) | Python, Selenium + chromedriver.exe | 2024-10-06 | Unmaintained. |
| [Apify automation-lab/carousell-listings-scraper](https://apify.com/automation-lab/carousell-listings-scraper) | Hosted browser + residential proxy | n/a | Paid ("from $4.24 / 1,000" items); says datacenter traffic gets Cloudflare verification. |

None of the open-source projects use the current `/ds/` JSON endpoints. None was run here; status is from reading their code against today's observations.

---

## 9. Carousell's own AI features seen in the client code

The JS bundle contains experiment flags and state slices named `BF.bf-11221-llm-shopping-assistant`, `ShoppingAssistant`, `agenticChat` (`/api/1.0/agentic-chat/1.0/send-chat-message`), `AiCarFinderSidebar`, and `SmartRefinementSuggestion`. Carousell is experimenting with an LLM shopping assistant of its own. Whether it is live for SG users was not checked.

---

## 10. Phase 1 re-check (2026-10-24, about 01:20 to 01:40 SGT)

Made while building the phase 1 client, from the same home machine, with Python `httpx` 0.28 (HTTP/1.1) and a desktop Chrome 141 User-Agent. 31 requests in total, at least 0.7 s apart (1.0 s ± 0.3 s jitter). Everything in sections 2 and 4 still behaved as documented. Nothing broke. New or newly verified points:

1. **The home page is enough to bootstrap.** `GET https://www.carousell.sg/` returned 200 (~370 KB instead of ~2 MB for a search page), set `_t`, `_t2`, `siv_2` and `_csrf`, and its `initialState.Application.csrfToken` worked as the `csrf-token` header. `Application` also carries `isBot` (false) and `wasRenderedInPanicMode` (false), which may be worth logging if blocks start.
2. **Boolean filter shape verified** for `is_bp_enabled`: `{"fieldName": "is_bp_enabled", "boolean": {"value": true}, "enforce": true}` was echoed in `appliedContext` and produced a "Buyer Protection: Yes" bubble. `shipping_offer_free_shipping` uses the same shape but was not sent separately.
3. **Open-ended price range works**: `rangedFloat` with only `end` was applied ("Up to S$900" bubble).
4. **`appliedContext.filters` echoes prices as numbers** (`{"value": 900}`) although we send strings. The search also appends `{"fieldName": "collections", "idsOrKeywords": {"value": ["0"]}}` when no category is sent.
5. **`likesCount` is omitted when zero.** Across 20 sampled cards, no card had an explicit `0` while 2 had no `likesCount` key at all, consistent with protobuf-style omission of zero values **(inferred)**. The client reads a missing key as 0.
6. **Legacy condition code on old listings.** Search cards' `condition` is the five-level code (3, 4, 7, 5, 6) for current listings, but an old listing (id 1070734588, first posted 2021) had `condition: 2` with the label "Used". Carousell returned it for a `layered_condition` filter of `["4", "7"]` (like new, lightly used), so old "Used" listings leak into used-condition filters. Code 1 (presumably legacy "New") was not seen. The client keeps such listings with `condition = None` and `condition_label = "Used"`.
7. **Listing detail `condition` is not the layered code.** A "Like new" listing had `condition: "1"` and `layered_condition: 4`; `listing_attributes.condition` holds the label ("Like new"). Use `layered_condition`.
8. **Listing detail `data.screens`** had two screens: `screen_0` (`screen_name: "listing_details_screen"`, carries the listing in `meta.default_value`) and `screen_faq_0` (`default_value: null`). The screens' `groups` (UI layout) are ~34 KB of the ~46 KB response.
9. **Cards carry extra fields** not listed in 4.3: `media[]`, `photos[]` with thumbnail sizes, `overlayContent.timestampContent` (same time as `aboveFold`), and sometimes `labelContent` (e.g. `{"text": "Good Deal"}`). Card ids are strings; detail ids are integers.
10. **Bot User-Agent still challenged.** `GET /search/iphone` with `User-Agent: python-httpx/0.28.1` returned `403`, `cf-mitigated: challenge`, `<title>Just a moment...</title>` (saved as `tests/fixtures/cloudflare_challenge.json`).
11. **Keyword is loose under some sorts.** "iphone 15 pro" in Mobile Phones with `mobile_storage` 256GB and sort Recent returned an iPhone 17 Pro Max and an iPhone 13 Pro on page 1. Use best match, or filter by `mobile_model`, when precision matters.
