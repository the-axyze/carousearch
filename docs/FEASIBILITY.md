# Carousearch: feasibility study

**Date:** 2026-10-23 (all prices, versions and observations are as of this date)
**Scope:** a personal Telegram bot that searches carousell.sg from natural-language prompts, in three levels: (1) prompt to filter toggles, (2) prompt to keywords and category-specific fields, (3) LLM/VLM screening of listings.
**Status:** research only. No application code exists.
**Companion docs:** [CAROUSELL_ACCESS_NOTES.md](CAROUSELL_ACCESS_NOTES.md) (raw endpoint findings), [ROADMAP.md](ROADMAP.md) (milestones).

---

## Executive summary

**Verdict: GO, for personal low-volume use.** Every piece was checked against the live site or current vendor docs today. Carousell has no public API, but its website calls internal JSON endpoints, and those work from a plain HTTP client (no browser) once you load one page to get cookies and a CSRF token. Those endpoints accept every filter Level 1 and Level 2 need, including category-specific facets (phone model, storage, colour, chair type, shoe size, car make). Listing details expose the seller signals Level 3 needs (rating, review counts, join date, response rate, verification flags, all photos). LLM costs at the stated volume are a few dollars a month.

**Recommended stack**

| Layer | Choice |
|---|---|
| Language | Python 3.12+ (3.14 is installed locally) |
| Carousell access | Plain HTTP client (`httpx`) with a browser User-Agent, cookie jar and CSRF bootstrap, calling `/ds/search/cf/4.0/search/` and `/ds/listing-detail/3.1/listings/<id>/detail/`. Fallback 1: parse the server-rendered JSON in the HTML. Fallback 2: Playwright. |
| Telegram | `python-telegram-bot` 22.x (async, built-in JobQueue), long polling |
| LLM, Levels 1 and 2 | Claude Haiku 5.5 (`claude-haiku-5-5`, US$0.10 in / US$0.50 out per 1M tokens) with JSON-schema structured output. `gpt-6-luna` costs the same and is the drop-in alternative. |
| VLM, Level 3 | Same model (Haiku 5.5 takes images), after rule-based pre-filtering |
| Storage | SQLite |
| Hosting | The user's home WSL2 machine (a residential Singapore IP is the least likely to be challenged by Cloudflare) |

**Estimated monthly cost** at 50 queries a day: about **US$0.70** for Levels 1 and 2. Level 3 at 30 listings per query is **US$3 to 13**, depending on how much pre-filtering and caching you do. Hosting at home is free. Expect **about US$5 to 15 a month** all in.

**Biggest risk:** Carousell changes or locks down its undocumented internal endpoints, or flags the home IP. Its Terms of Service prohibit automated access outright (section 2). Mitigations: a thin, swappable client layer, fixture-based contract tests, a daily canary, caching, polite rate limits, and the HTML and Playwright fallbacks.

**Could not verify:** rate-limit thresholds (no stress test was run), behaviour from datacenter/VPS IPs, model quality and latency on these tasks (no model API calls were made), local VLM speed (no usable GPU in this WSL instance), and current VPS prices. See [section 8](#8-what-could-not-be-verified).

---

## 1. Carousell data access

### 1.1 Official API

None for consumers. No developer portal or partner API was found. API Evangelist lists Carousell as having no publicly documented API (https://providers.apievangelist.com/providers/carousell/, accessed 2026-10-23). The Terms of Service mention third-party APIs only for "Carousell Global" listings sourced from eBay. Third-party "Carousell APIs" (Apify, Zyla) are scrapers.

### 1.2 How carousell.sg search works

- It is a custom React + redux-saga app, not Next.js. There is no `__NEXT_DATA__`.
- Pages are **server-rendered**. Each page embeds its whole app state as one `<script type="application/json">` blob (1.7 to 2 MB), which becomes `window.initialState`. For a normal keyword search, the first ~50 listing cards, the applied filters, the category's facet definitions and the full category tree are all in that blob.
- After load, the browser fetches more pages, and refetches when filters change, from internal JSON endpoints under `/ds/<service>/...`.
- Search URL: `https://www.carousell.sg/search/<query>?price_start=..&price_end=..&layered_condition=3,4&sort_by=3&_deal_method=MEETUP`. This was verified today: every filter was applied server-side.

### 1.3 Internal JSON endpoints (verified 2026-10-23)

| Purpose | Method and path | Notes |
|---|---|---|
| Search | `POST /ds/search/cf/4.0/search/` | JSON body: query, `filters[]`, `sortParam`, `count`, `session` for the next page |
| Search plus facet definitions | `POST /ds/filter/cf/4.0/search/` | Same body. The response adds `filters`, the category's full facet schema with option values. |
| Listing detail | `GET /ds/listing-detail/3.1/listings/<id>/detail/?country_code=SG` | Description, all photos, timestamps, meet-up points, seller stats |
| Category hint | `GET /ds/search-suggestions/2.5/search-suggestions/suggestions/?country_code=SG&query=` | Returns suggested categories |
| Location IDs | `GET /ds/location-filter-search/1.0/filters/search?country_id=1880251&query=` | MRT, area and neighbourhood IDs for the location filter |

Requirements: a desktop browser User-Agent, cookies from one prior page load, and the `csrf-token` header (read from `initialState.Application.csrfToken`). Without cookies and CSRF the API returns 422. The endpoint that older open-source scrapers used, `/api-service/filter/cf/4.0/search/`, now returns 404. Full request and response examples are in [CAROUSELL_ACCESS_NOTES.md](CAROUSELL_ACCESS_NOTES.md#4-internal-json-endpoints).

### 1.4 Anti-bot protection (observed 2026-10-23)

| Client | Result |
|---|---|
| curl or python-requests default User-Agent | 403 Cloudflare challenge (`cf-mitigated: challenge`) |
| curl or Python stdlib with a Chrome User-Agent | 200 |
| JSON API without cookies/CSRF | 422 |
| JSON API with cookies + CSRF | 200, 0.4 to 1.1 s per call |
| ~30 calls at 1.5 to 2 s spacing | All 200, no throttling seen |

So: Cloudflare bot management screens on obvious bot signatures. There is no login wall for search or listing detail, and no CAPTCHA or 429 was seen at this low rate. A third-party scraper vendor reports Cloudflare challenges for **datacenter** IPs and recommends residential proxies ([Apify actor page](https://apify.com/automation-lab/carousell-listings-scraper), accessed 2026-10-23). This was not tested here. Another public scraper documents a **soft block**: HTTP 200 with a null result list.

### 1.5 Filter facets (what Level 1 and Level 2 map onto)

**Global facets (all categories checked):**

| Facet | API field | Values |
|---|---|---|
| Category | `collections` | collection id from a 725-node tree (34 top-level) |
| Price range | `price` (`price_start`/`price_end` in URLs) | SGD min/max |
| Condition | `layered_condition` | Brand new 3, Like new 4, Lightly used 7, Well used 5, Heavily used 6 |
| Deal method | `_deal_method` | MEETUP, MAILING |
| Sort | `sortParam` / `sort_by` | Best match 1, Recent 3, Price ascending 4, Price descending 5, Nearby 6 |
| Buyer Protection | `is_bp_enabled` | boolean |
| Free delivery | `shipping_offer_free_shipping` | boolean |
| Location | `adm_l2_location_ids` and related | IDs from the location lookup (appears to work; exact semantics not fully verified) |

**Category-specific examples (from live fieldsets):**

| Category | Facets |
|---|---|
| Mobile Phones | model (e.g. `MOBILE_MODEL_IPHONE_15_PRO`), storage (128GB to 1TB), colour, free accessories, local/international set, manufacturer warranty |
| Chairs (furniture) | chair type (office/gaming, dining, armchair...), size (up to or over 1.2m), material |
| Laptops | brand (22 brands) |
| Men's footwear | brand, EU/UK/US size |
| Used cars | make, model, body type, vehicle age, registration date, depreciation, monthly instalment, fuel, transmission, doors, owner count, seats, seller type |

**Not filterable server-side:** listing age ("posted in the last N days"), seller rating, like count, seller join date. Do these client-side: sort by Recent and cut off on the card timestamp. Rating and join date need the detail call.

Facet sets depend on the category, and Carousell auto-narrows the category from the query ("iphone 15 pro" was narrowed to "iPhone 15 Series"). So Level 2 must read the facet schema from the live response, not from a hard-coded list.

### 1.6 Data available per listing

| Field | Search card (free, part of the search response) | Detail call (one extra request) |
|---|---|---|
| Title, price (display string), condition code | yes | yes (price as number) |
| Like count | yes | yes |
| Seller username, id, avatar | yes | yes |
| Seller type "Professional Seller" | sometimes | via flags |
| Posted / bumped time | yes (`time_created`, `active_bump`, `expired_bump` epoch) | `time_created`, `last_modified`, `status_updated` |
| Description | first ~250 characters | full text |
| Photos | 1 thumbnail (240x320) | all photos, full size (~810x1080, ~120 KB) |
| Buyer Protection / Certified tags | yes | yes |
| Meet-up locations, mailing yes/no | no | yes |
| Seller rating, review counts (positive/neutral/negative) | no | yes |
| Seller join date, response rate (H/M/L), followers | no | yes |
| Seller verification (email, mobile, ID, Google, Facebook), suspended flag | no | yes |
| Offer count, comment count | no | yes |

### 1.7 Existing open-source scrapers

Summary (full table in [CAROUSELL_ACCESS_NOTES.md](CAROUSELL_ACCESS_NOTES.md#8-existing-open-source-and-commercial-scrapers-checked-2026-10-23)):

- **None use the current `/ds/` JSON endpoints.** The best-known JSON-API scraper ([kokseen1/wall-e](https://github.com/kokseen1/wall-e), last push 2022) targets the dead `/api-service/` path.
- **Recent and maintained:** [hoelee/carousell-monitor](https://github.com/hoelee/carousell-monitor) (Python, Oct 2026) parses the server-rendered JSON, the same technique verified here. [Ken69420/carousell-deal-scanner](https://github.com/Ken69420/carousell-deal-scanner) (Oct 2026) and [theboi/carousell-monitoring](https://github.com/theboi/carousell-monitoring) (Oct 2026) use headless browsers. [LuckyProgramme/carousell_listing_scraper](https://github.com/LuckyProgramme/carousell_listing_scraper) (Oct 2026) already uses Gemini to screen listings.
- None is a natural-language search bot. Writing a small client is easier than adopting any of them.

### 1.8 Recommended access strategy

1. **Primary:** the JSON API through a small `CarousellClient`. Bootstrap cookies and CSRF from one HTML page load, keep the session (the CSRF cookie lasts 7 days), and refresh on 401/403/422.
2. **Fallback A:** fetch the HTML search page and parse `initialState.SearchListing.listingCards` (add `tab=marketplace` for phone-type queries).
3. **Fallback B:** Playwright (headless Chromium) when Cloudflare starts challenging plain HTTP.
4. **Politeness:** at most 1 request/second, 2 to 3 in flight, cache search results for ~5 minutes and listing details for 12 hours, and stay under roughly 1,000 Carousell requests a day.

---

## 2. Legal and Terms of Service risk (not legal advice)

**Carousell Terms of Service**, last updated 17 June 2026, read on 2026-10-23 via the Zendesk API mirror of https://support.carousell.com/hc/en-us/articles/115011881808-Terms-of-Service. The HTML page is behind a Cloudflare challenge. Contracting party: Carousell Pte. Ltd. Governing law: Singapore (section 32). By browsing at all, a user "agree[s] to observe and be bound by these Terms" (section 1).

Section 2 ("Use of the Services") says you shall not:

> "Use any scraper, robot, bot, spider, crawler or any other automated device or means to access, acquire, copy or monitor any portion of the Services, or any data or content found or access through the Services."

> "Collect any information in respect of other users without their consent."

> "Copy, decompile, reverse engineer, disassemble, attempt to derive the source code of, decrypt, interfere with, or disrupt the integrity or the performance of the Services."

> "Use the Services to create or promote a product, service or software that is, directly or indirectly, competitive with or in any way a substitute for the Services or any services, product or software offered by Carousell."

Section 24 ("Suspension and Termination") lets Carousell suspend or terminate an account or access "at any time, for any reason, and without advance notice."

**Plain reading:** this project breaches the anti-automation clause. That holds even though it is read-only, personal and low-volume. The realistic consequences are blocking, or termination of your Carousell account if one is linked. The bot needs no login, so don't give it one. Lawsuits against individual low-volume personal users are not something these sources document, but the contractual breach is clear.

**Singapore law notes (secondary sources, not verified against statute text):**

- **PDPA:** the data-protection obligations do not apply to "any individual acting in a personal or domestic capacity" (s 4(1)(a); PDPC guidance at https://www.pdpc.gov.sg/complaints-and-reviews/before-you-lodge-a-complaint-with-us-3/complaints-against-an-individual). A personal bot storing seller usernames and ratings fits this. Sharing or selling that data would not.
- **Copyright Act 2021 ss 243-244 (computational data analysis):** commentators say s 187 stops contracts from overriding this exception, provided access was lawful ([Rouse, 2024](https://rouse.com/insights/news/2024/artificial-intelligence-in-singapore-copyright-infringement-defence-for-artificial-intelligence-machine-learning)). Its fit for a search bot is uncertain.
- **Computer Misuse Act:** circumventing access controls is where criminal exposure would arise. Reading public pages with a browser-like client is different from bypassing a login or a CAPTCHA. Do not build CAPTCHA-solving or challenge-bypass tooling. This point was not researched in depth.

**Practical rules for the build:** personal use only. No login. No redistribution or resale of data. Don't keep seller data longer than needed. Low request volume. Honour blocks rather than evading them aggressively.

`robots.txt` disallows `/search/` and query URLs for generic crawlers, while explicitly allowing named AI agents (GPTBot, ClaudeBot, Claude-User and others). It is a crawler convention, not permission under the ToS.

---

## 3. Telegram bot layer

### 3.1 Library choice

| Library | Language | Latest (PyPI/npm, 2026-10-23) | Fit for this bot |
|---|---|---|---|
| **python-telegram-bot** | Python | 22.8 (2026-06-12) | Recommended. Async, good docs, `CallbackQueryHandler` for inline keyboards, `ConversationHandler` for clarifying questions, built-in `JobQueue` for alerts, user-id filters for single-user lock-down. |
| aiogram | Python | 3.31.0 (2026-08-26) | Also good: routers, FSM, middlewares. Slightly more boilerplate, no built-in scheduler. |
| pyTelegramBotAPI | Python | 4.37.1 (2026-10-17) | Simple, but its sync-first design fits a concurrent pipeline worse. |
| grammY | TypeScript | 1.46.0 (2026-08-26) | Excellent, but it would split the project across two languages. Python is better for the LLM and scraping side. |

### 3.2 Telegram limits that shape the UX (Bot API 10.3, docs read 2026-10-23)

| Limit | Value |
|---|---|
| Message text | 1 to 4096 characters |
| Photo caption | 0 to 1024 characters |
| Media group (album) | 2 to 10 items; **cannot carry an inline keyboard** (`sendMediaGroup` has no `reply_markup`) |
| `callback_data` per button | 1 to 64 bytes |
| Photo by URL | 5 MB max; uploaded photo 10 MB max; width + height at most 10000 |
| Rate | avoid more than 1 message/second per chat; about 30 messages/second overall ([Bots FAQ](https://core.telegram.org/bots/faq)) |
| Files | send up to 50 MB, download up to 20 MB |

### 3.3 Proposed UX

- **Search:** the user types a prompt. The bot replies with one message: an "Applied filters" line, then up to 10 results ("S$680 · Like new · 462 likes · bumped 2h ago · title" with a link).
- **Keyboard on that message:** `< Prev | 1/4 | Next >`, then filter chips such as `Condition: Like new+`, `S$300-900`, `Sort: Recent`, `Meet-up only`, `Edit`. Tapping a chip edits the same message in place (`editMessageText`), which keeps chat noise down and stays inside rate limits.
- **Images:** a `Photos` button sends one album of up to 10 thumbnails captioned "#1 S$680". The keyboard stays on the text message, because albums can't carry one.
- **State:** callback data is limited to 64 bytes. Keep search state in SQLite under a short id and put only `p:<id>:<page>` style tokens in buttons.
- **Single user:** reject updates from any Telegram user id except yours.

### 3.4 Hosting

| Option | Cost | Notes |
|---|---|---|
| **Home WSL2 (recommended)** | free | Residential SG IP, which is what was tested to work. Use long polling (no inbound port or domain needed). Run the bot as a systemd service (`[boot] systemd=true` in `/etc/wsl.conf`) and start WSL at Windows logon with Task Scheduler. Cron can do a watchdog restart and nightly category refresh. Downside: down when the PC sleeps. |
| Raspberry Pi or another always-on home box | ~US$60 one-off | Same IP benefits, always on. |
| Hetzner Cloud, Singapore | CPX11 launched at EUR 7.90/month (Aug 2024 announcement); prices rose in 2026, not re-checked | Datacenter IP: Carousell may challenge it (untested). |
| Oracle Cloud Always Free (ARM) | free | Reportedly cut to 2 OCPU / 12 GB in mid-2026 ([InfoQ](https://infoq.com/news/2026/07/oracle-cloud-free-tier-limits/)); same datacenter-IP caveat. |

---

## 4. LLM for prompt to filters (Levels 1 and 2)

### 4.1 Model comparison (list prices per 1M tokens, verified 2026-10-23)

| Model | Input | Output | Structured output | Status | Est. per query | Est. per month (1,500 queries) |
|---|---|---|---|---|---|---|
| **Claude Haiku 5.5** `claude-haiku-5-5` | $0.10 | $0.50 | yes (`output_config.format` JSON schema, strict tools) | current | $0.00048 | **$0.72** |
| OpenAI `gpt-6-luna` | $0.10 | $0.50 | yes | current; replaces gpt-4.1-mini and gpt-4o-mini | $0.00048 | $0.72 |
| OpenAI `gpt-5-nano` | $0.05 | $0.40 | yes | **shuts down 2026-12-11** (replacement: gpt-5.6-luna) | $0.00027 | $0.41 |
| OpenAI `gpt-4.1-nano` | $0.10 | $0.40 | yes | **shuts down 2026-10-23 (today)** | n/a | n/a |
| OpenAI `gpt-5.4-nano` | $0.20 | $1.25 | yes | current | $0.00101 | $1.52 |
| OpenAI `gpt-5-mini` | $0.25 | $2.00 | yes | shuts down 2026-12-11 | $0.00135 | $2.03 |
| OpenAI `gpt-4.1-mini` | $0.40 | $1.60 | yes | shuts down 2027-01-14 | $0.00184 | $2.77 |
| OpenAI `gpt-5.4-mini` | $0.75 | $4.50 | yes | current | $0.00376 | $5.63 |
| Google `gemini-2.5-flash-lite` | $0.10 | $0.40 | yes (JSON schema) | current; has a free tier | $0.00046 | $0.69 |
| Google `gemini-3.1-flash-lite` | $0.25 | $1.50 | yes | current; has a free tier | $0.00125 | $1.88 |
| Google `gemini-3.5-flash-lite` | $0.30 | $2.50 | yes | current; has a free tier | $0.00164 | $2.46 |
| Local: Ollama + Qwen3 / Qwen3-VL 4B-8B | $0 | $0 | JSON-schema `format` in Ollama | runs on your hardware | $0 | $0 (electricity) |

Assumptions per query: pass 1 is 3,100 input / 150 output tokens (system prompt, schema, compact category list, user prompt). Pass 2 (category facets) runs on 60% of queries at 1,200 / 80. That averages 3,820 input and 198 output tokens. Batch tiers (50% off) don't apply, because the user is waiting. Prompt caching would save a little more, but at under $1 a month it isn't worth engineering for.

Sources: [Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing) (Haiku 5.5 rate applies to prompts of 100K tokens or fewer); [OpenAI pricing](https://developers.openai.com/api/docs/pricing), [gpt-6-luna model page](https://developers.openai.com/api/docs/models/gpt-6-luna), [OpenAI deprecations](https://developers.openai.com/api/docs/deprecations); [Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing). All accessed 2026-10-23.

### 4.2 Recommendation

Use **Claude Haiku 5.5** behind a provider-neutral adapter, with `gpt-6-luna` and `gemini-2.5-flash-lite` as tested alternatives.

- The cheapest current-generation option that isn't scheduled for shutdown is a three-way tie on price (Haiku 5.5, gpt-6-luna, gemini-2.5-flash-lite). At under US$1 a month, cost does not decide this. Reliability of schema-following does, so measure it (ROADMAP Level 1 includes a 50-prompt eval set) and keep the winner.
- **Do not build on gpt-4.1-nano or gpt-5-nano.** The first shuts down today and the second on 2026-12-11.
- Haiku 5.5 runs adaptive thinking by default. For this extraction task, set low effort or disable thinking (allowed at effort `high` or below), otherwise hidden thinking tokens inflate output cost. It also rejects non-default `temperature`; use structured output for determinism. `gpt-6-luna` supports `reasoning_effort: none`.
- **Local fallback:** a Qwen3 4B-class model via Ollama with a JSON-schema `format` works offline at zero cost. It is slower, and on a CPU-only machine it is weaker at mapping vague prompts. Use it if the API key is missing or the budget is exceeded.

### 4.3 Structured-output design

Keep the LLM away from Carousell's raw IDs where possible. It outputs a **semantic intent**, and plain, unit-tested code maps that to the API body.

```python
class SearchIntent(BaseModel):            # pydantic; JSON schema generated from this
    keywords: str                           # what goes in the search bar (Level 2)
    exclude_keywords: list[str]             # e.g. ["case", "box only", "spoilt"]
    category_hint: str | None               # free text, resolved by code (see below)
    price_min: float | None
    price_max: float | None
    conditions: list[Literal["brand_new","like_new","lightly_used","well_used","heavily_used"]]
    deal_methods: list[Literal["meetup","mailing"]]
    sort: Literal["best_match","recent","price_low_to_high","price_high_to_low","nearby"]
    location_text: str | None               # "near Tampines" -> location lookup API, not the LLM
    max_listing_age_days: int | None        # applied client-side
    free_delivery_only: bool
    buyer_protection_only: bool
    soft_criteria: list[str]                # "good photos", "no resellers" -> Level 3
    ambiguities: list[Ambiguity]            # {field, question, options[]}
```

**Two passes, with Carousell doing half the work:**

1. **Pass 1 (LLM):** prompt to `SearchIntent`. Category resolution is done by code: call the search-suggestions endpoint and/or run the `/ds/filter/...` search with the keywords. Carousell returns `sfContext` (its own category guess) and the category's facet schema. Fall back to an LLM choice from a compact category list only when Carousell's guess is missing or conflicts with the prompt.
2. **Pass 2 (LLM, Level 2, only if the category has facets):** send the user prompt plus that category's facets, with options rendered as **JSON-schema enums built at runtime** (e.g. `mobile_storage: list[Literal["MOBILE_STORAGE_256_GB", ...]]`). The model can only pick values that exist. Then re-run the search with the facets applied.

Mapping semantic intent to Carousell values (`like_new` to `"4"`, `meetup` to `"MEETUP"`, `recent` to sort `"3"`) is a pure function with unit tests, so the LLM never has to memorise Carousell's codes.

### 4.4 Ambiguous prompts: default first, ask rarely

- **Default and show** (most cases): apply a sensible default, show it as a chip, and let the user fix it with one tap. Example: "cheap aeron chair" means no hard price cap, sort by price ascending, and show `Sort: Price low to high`.
- **Ask one question** only when a wrong guess makes the whole result set useless: unrelated categories ("mouse", "apple watch band vs apple watch"), or a hard constraint that can't be mapped. Ask with an inline keyboard of 2 to 4 options from `ambiguities[]`, never free-text follow-ups. At most one question per search.
- Never invent numbers. "Not too expensive" stays a soft criterion or a sort order, not a made-up price cap.

---

## 5. Level 3: screening listings with rules and vision models

### 5.1 What needs a model and what does not

| Signal | Method | Data source |
|---|---|---|
| Seller rating, review count, negative reviews | rule | detail call |
| Seller account age, response rate, verification flags, suspended | rule | detail call |
| "Professional Seller" (reseller) | rule | card or detail |
| Like count (normalise by listing age; bumps inflate it) | rule | card |
| Freshness (created or bumped N hours ago) | rule | card timestamps |
| Price outlier: too cheap means scam risk, too expensive means overpriced | rule: robust z-score vs the median of the same result set | card |
| Keyword blocklist ("WTB", "looking for", "for parts", "rent", "spoilt") | rule (regex) | title and description |
| Duplicate or stock photos reused across sellers | rule: perceptual hash (`imagehash`) across the result set | photos |
| Same title/description from many accounts (template) | rule: text similarity across results | card or detail |
| Photo blurry, dark, cluttered, low effort, screenshot of a website | **VLM** | 1 to 2 photos |
| Condition in photos consistent with the claimed condition | **VLM** | photos + condition + description |
| Description quality: specific details vs vague one-liner | **LLM** (text) | description |
| Scam / dropship / reseller template language | **LLM** (text), backed by the rules above | description |
| Match to the user's soft criteria ("in original box", "no scratches") | **LLM / VLM** | description + photos |

Rule signals are free, deterministic and testable. Use models only for the subjective items.

### 5.2 Vision model comparison

**How each provider counts image tokens (docs read 2026-10-23):**

- **Claude:** `ceil(w/28) x ceil(h/28)` tokens. Claude 4.7 and later models are capped at 2,576 px on the long edge and 4,784 tokens ([vision docs](https://platform.claude.com/docs/en/build-with-claude/vision)). A Carousell full photo (810x1080) costs 1,131 tokens; downscaled to 576x768 it costs 588; to 288x384, 154; the 240x320 thumbnail, 108.
- **OpenAI** (current models): 32 px patches times a per-model multiplier (1.2 for gpt-5.4/5.6 family, 1.5 for gpt-5-nano, 2.46 for gpt-4.1-nano) ([images guide](https://developers.openai.com/api/docs/guides/images-vision)). A 576x768 image is 432 patches, or 519 tokens at x1.2. The gpt-6-luna multiplier is **not listed**; x1.2 is assumed.
- **Gemini:** 2.5 models use 258 tokens per image when both sides are 384 px or less, otherwise 258 per 768 px tile ([image understanding](https://ai.google.dev/gemini-api/docs/image-understanding)). Gemini 3.x uses `media_resolution`: low 280, medium 560, high 1,120 (default) tokens per image ([media resolution](https://ai.google.dev/gemini-api/docs/media-resolution)).

**Cost per screened listing** (2 photos + ~1,000 text tokens in, ~150 out):

| Model | Image setting | Input tokens | Cost per listing | Per 1,000 images (image part only) | Status |
|---|---|---|---|---|---|
| **Claude Haiku 5.5** | 2 x 576x768 | 2,176 | $0.00029 | $0.059 | current |
| Claude Haiku 5.5 | 2 x 288x384 | 1,308 | $0.00021 | $0.015 | current |
| gpt-6-luna | 2 x 576x768, x1.2 assumed | 2,038 | $0.00028 | $0.052 | current |
| gpt-5-nano | 2 x 576x768, x1.5 | 2,296 | $0.00018 (plus any reasoning tokens) | $0.032 | shuts down 2026-12-11 |
| gpt-5.4-nano | 2 x 576x768 | 2,038 | $0.00060 | $0.104 | current |
| gemini-2.5-flash-lite | 2 x 384 px or less (258 each) | 1,516 | $0.00021 | $0.026 | current; free tier |
| gemini-3.1-flash-lite | 2 x media_resolution low | 1,560 | $0.00062 | $0.070 | current; free tier |
| Local Qwen3-VL 4B/8B (Ollama) | any | n/a | $0 | $0 | 3.3 GB / 6.2 GB downloads ([ollama.com](https://ollama.com/library/qwen3-vl)) |

**Monthly cost at the stated load** (30 listings x 50 queries x 30 days = 45,000 screenings):

| Strategy | Screenings/month | Haiku 5.5 @576px | gemini-2.5-flash-lite |
|---|---|---|---|
| Screen everything, no cache | 45,000 | $13.17 | $9.52 |
| **Lean:** rules first, model-screen the top 15, cache verdicts by listing id (assume 50% repeats) | 11,250 | **$3.29** | $2.38 |

**Latency.** Cloud vision calls are typically 1 to 3 s each; run them in parallel (8 to 10 at once), so 15 listings take roughly 3 to 5 s. This is an estimate: no calls were made. A local 4B VLM on CPU will likely take 5 to 30 s **per image** (unmeasured; this WSL instance has no usable GPU, since `nvidia-smi` fails), so 15 listings would take minutes. It works for background alerts, not interactive search.

**Quality expectations (unbenchmarked):**

- **Blurry, dark, low-effort photos:** cheap VLMs do this well. It is a coarse visual judgement, and 384 to 768 px is enough.
- **Condition vs description consistency:** moderate. Visible damage is caught, small scratches at 768 px are not. Use it as a ranking signal, never a hard filter.
- **Scam / reseller / dropship template:** text models catch generic template phrasing. The rule signals (price far below median, new account, no reviews, reused photos, Professional Seller tag) are more reliable. Anthropic's docs warn that Claude cannot reliably tell whether an image is AI-generated, so use perceptual hashing for "stock photo" detection.
- Always return a one-line reason per judgement so the user can see why something was down-ranked.

### 5.3 Scoring scheme

1. **Hard drops (rules):** suspended seller, blocklisted keywords, out-of-range price, not live, seller on your personal blocklist, exact duplicates.
2. **Rule score R (0 to 1)**, weighted:
   - seller trust 0.35 (rating x log review count, account age, response rate, verification)
   - price sanity 0.25 (penalise below 0.5x median as risky, mild penalty above median)
   - freshness 0.15
   - likes per day 0.10
   - reseller penalty 0.15, if you said "no resellers"
3. **Model score M (0 to 1):**
   - photo quality 0.30
   - description quality 0.20
   - condition consistency 0.20
   - soft-criteria match 0.30
   - scam-template risk 0 to 1, used as a multiplier `(1 - 0.5 x risk)`
4. **Final:** `score = 0.5 x R + 0.5 x M x (1 - 0.5 x scam_risk)`. Weights live in config. Listings flagged "price under 0.5x median and account under 30 days old" get a visible warning badge, not silent removal.
5. **Display:** sort by score and show badges (`Trusted seller`, `Great photos`, `Possible reseller`, `Too cheap?`) with one-line reasons.

### 5.4 Carousell request budget (the real Level 3 constraint)

Seller rating, join date and full photos need **one detail request per listing**. At 30 listings x 50 queries that would be 1,500 extra Carousell requests a day. That is the most likely thing to trigger blocking, more so than LLM cost. Mitigations:

- Rank on card-level rules first (free).
- Fetch details only for the top 10 to 15.
- Cache details for 12 h and seller stats for 7 days.
- Fetch 2 to 3 at a time.

Expected total: about 500 to 700 Carousell requests a day, plus image downloads from the CDN.

---

## 6. Architecture

### 6.1 Modules (Python package `carousearch`)

```
carousearch/
  carousell/
    session.py        # cookie jar + CSRF bootstrap, browser UA, rate limiter, retry/backoff
    search_api.py     # build request body from CarousellQuery, parse SearchPage (pure parse functions)
    listing_api.py    # listing detail -> ListingDetail
    html_fallback.py  # parse initialState from SSR HTML (same output types)
    categories.py     # category tree snapshot (refreshed nightly), id <-> cc_id
    fieldsets.py      # per-category facet schema, cached
    locations.py      # location text -> location filter IDs
  search/
    intent.py         # SearchIntent model (the contract between LLM and code)
    to_carousell.py   # pure: SearchIntent + facets -> CarousellQuery
    pipeline.py       # orchestrates: intent -> query -> results -> (screen) -> ranked results
  llm/
    base.py           # IntentExtractor / ListingJudge protocols
    anthropic_adapter.py, openai_adapter.py, gemini_adapter.py, ollama_adapter.py
    prompts/          # versioned prompt files
  screening/
    rules.py          # pure rule features and hard drops
    vision.py         # ListingJudge calls, image downscaling
    scoring.py        # pure: features -> score + badges
  storage/
    db.py             # SQLite: searches, results cache, listing cache, verdict cache, saved searches, seen listings
  bot/
    app.py            # python-telegram-bot Application, single-user filter
    handlers.py       # message, callback, conversation handlers
    render.py         # pure: results -> message text + keyboard
  config.py           # env vars, model choice, weights, rate limits
tests/
  fixtures/           # saved real responses (search, filter, detail, SSR HTML)
```

### 6.2 Why Levels 2 and 3 slot in without rewrites

- `SearchIntent` has the Level 2 fields (`keywords`, facets) and the Level 3 field (`soft_criteria`) from day one. Level 1 just leaves them empty.
- `pipeline.py` has a fixed stage order: `extract -> resolve category -> fetch facets -> map -> search -> screen -> rank -> render`. Level 1 uses a `NoFacetMapper` and a `NoOpScreener`. Levels 2 and 3 swap in real ones. Dependencies are injected, so each stage is unit-testable with fakes.
- The Carousell client returns typed domain objects (`ListingCard`, `ListingDetail`, `FacetSchema`), not raw JSON. If Carousell changes its API, or you fall back to HTML or Playwright, only `carousell/` changes.

### 6.3 Storage (SQLite tables)

`search_sessions` (id, prompt, intent_json, query_json, created_at), `result_pages` (session_id, page, listing_ids, fetched_at), `listing_cache` (listing_id, detail_json, fetched_at), `verdict_cache` (listing_id, model, prompt_version, verdict_json), `saved_searches` (id, intent_json, interval_minutes, last_run_at), `seen_listings` (saved_search_id, listing_id, first_seen_at), `seller_blocklist`.

### 6.4 Risk register

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| 1 | Carousell changes `/ds/` endpoints, payloads or the CSRF scheme | High (over 12 months) | High | Thin client; fixture-based contract tests; daily canary search that alerts you in Telegram; HTML fallback; Playwright fallback |
| 2 | Cloudflare starts challenging plain HTTP clients | Medium | High | Browser-like headers; `curl_cffi` (browser TLS fingerprint) as a drop-in; Playwright; keep volume low |
| 3 | Home IP flagged, which also affects your own browsing of Carousell | Low-Medium | Medium | At most 1 req/s, under about 1,000 requests a day, caching, back off for hours on any 403 |
| 4 | ToS enforcement against your account | Low | Medium | No login in the bot; personal use only; no redistribution |
| 5 | LLM maps filters wrongly | Medium | Low | Show applied filters as editable chips; 50-prompt eval set; enum-constrained schemas |
| 6 | VLM judgements noisy | Medium | Medium | Rank, don't hide; show reasons; rules carry half the score |
| 7 | Model deprecation (gpt-4.1-nano ends today, gpt-5-nano on 2026-12-11) | Certain for some models | Low | Provider-neutral adapter; model id in config; eval set to re-qualify a replacement |
| 8 | Level 3 detail calls multiply Carousell traffic | High | Medium | Screen only the top 10 to 15; long caches (section 5.4) |
| 9 | Home machine asleep or off | Medium | Low | systemd service + Task Scheduler; optional Raspberry Pi |
| 10 | Carousell ships its own LLM search assistant (flags already in its JS) | Medium | Low | Personal value remains: your own screening rules and alerts |

### 6.5 Effort estimate (solo developer days; details in [ROADMAP.md](ROADMAP.md))

| Milestone | Effort |
|---|---|
| M0: client spike, fixtures, canary | 1 to 1.5 days |
| Level 1: prompt to global filters, bot with toggles and pagination | 3 to 5 days |
| Level 2: keywords + category facets, clarifying questions | 2 to 4 days |
| Level 3: rules + VLM screening + scoring | 4 to 6 days |
| Optional: saved-search alerts | 1 to 2 days |
| **Total** | **11 to 18 days** |

---

## 7. Go / no-go

**GO.** The core risk, data access, was checked live: search with all filters, category facets and listing details are reachable today from a plain HTTP client on the home connection. LLM and VLM costs are trivial at personal scale.

Key assumptions behind the GO:

1. Personal use only, from a residential Singapore IP, at roughly 1,000 Carousell requests a day or fewer.
2. You accept that this breaches Carousell's anti-automation clause, and the bot never logs in.
3. You accept maintenance: expect the client to break occasionally. Budget an hour or two a month and keep the fallbacks.
4. Level 3 is a ranking aid, not a guarantee against scams.

**Stop criteria:** if within the first two weeks of Level 1 Carousell starts returning challenges to the plain HTTP client and the Playwright fallback is also challenged, stop or scale down. For example, keep only a manual "open this search on Carousell" link builder (Level 1 output as a URL), which needs no scraping at all.

---

## 8. What could not be verified

- **Rate-limit thresholds.** Only about 60 requests were sent today, none rejected. No stress test was run, deliberately.
- **Datacenter / VPS behaviour.** Not tested; third-party reports say it gets challenged.
- **Long-term stability** of the `/ds/` endpoints and CSRF scheme.
- **Location filter semantics.** `adm_l2_location_ids` appears to filter; `coverage_location_ids` needs more testing.
- **Boolean filter JSON shape** (`_delivery`, `is_bp_enabled`): inferred, not tested. The URL-param form is verified.
- **Meaning of `cardType` and `status` codes** on cards (`status: "L"` is presumably "live").
- **Model quality and latency** on these tasks. No LLM or VLM API calls were made; latency figures are typical-range estimates.
- **gpt-6-luna image token multiplier.** Not listed in OpenAI's table; assumed 1.2 like gpt-5.6-luna.
- **Gemini free-tier rate limits and data-use terms.** Not on the pricing page.
- **Local VLM speed.** No usable GPU in this WSL instance (`nvidia-smi` failed to initialise NVML). Whether the host has an NVIDIA GPU is unknown.
- **Current Hetzner Singapore and Oracle Always Free terms.** Secondary sources conflict.
- **Legal points.** Based on the ToS text and secondary commentary; statutes were not read directly; not legal advice.

---

## 9. Sources (all accessed 2026-10-23)

**Carousell (live fetches):**
- https://www.carousell.sg/search/herman%20miller%20aeron (and filtered variants)
- https://www.carousell.sg/p/1379060933/
- https://www.carousell.sg/ds/search/cf/4.0/search/
- https://www.carousell.sg/ds/filter/cf/4.0/search/
- https://www.carousell.sg/ds/listing-detail/3.1/listings/1379060933/detail/
- https://www.carousell.sg/robots.txt
- https://www.carousell.sg/sitemap.xml
- JS bundle https://mweb-cdn.karousell.com/build/7024b4d618a2f803.min.js

**Carousell ToS:** https://support.carousell.com/hc/en-us/articles/115011881808-Terms-of-Service (read via https://support.carousell.com/api/v2/help_center/en-us/articles/115011881808.json)

**Official API check:** https://providers.apievangelist.com/providers/carousell/

**Scrapers:**
- https://apify.com/automation-lab/carousell-listings-scraper
- https://github.com/kokseen1/wall-e
- https://github.com/hoelee/carousell-monitor
- https://github.com/theboi/carousell-monitoring
- https://github.com/Ken69420/carousell-deal-scanner
- https://github.com/LuckyProgramme/carousell_listing_scraper
- https://github.com/dexterleng/carousell-telebot
- https://github.com/mirfsz/Carousell-Telegram-Bot
- https://github.com/jiakai2002/carousell-scraper

**Legal:**
- https://www.pdpc.gov.sg/complaints-and-reviews/before-you-lodge-a-complaint-with-us-3/complaints-against-an-individual
- https://lawplayer.com/sg/act/PDPA2012/4
- https://rouse.com/insights/news/2024/artificial-intelligence-in-singapore-copyright-infringement-defence-for-artificial-intelligence-machine-learning

**Telegram:**
- https://core.telegram.org/bots/api (Bot API 10.3)
- https://core.telegram.org/bots/faq
- PyPI: https://pypi.org/project/python-telegram-bot/, https://pypi.org/project/aiogram/, https://pypi.org/project/pyTelegramBotAPI/
- npm: https://www.npmjs.com/package/grammy

**Anthropic:**
- https://platform.claude.com/docs/en/about-claude/pricing
- https://platform.claude.com/docs/en/build-with-claude/vision

**OpenAI:**
- https://developers.openai.com/api/docs/pricing
- https://developers.openai.com/api/docs/guides/images-vision
- https://developers.openai.com/api/docs/models/gpt-6-luna
- https://developers.openai.com/api/docs/deprecations

**Google:**
- https://ai.google.dev/gemini-api/docs/pricing
- https://ai.google.dev/gemini-api/docs/image-understanding
- https://ai.google.dev/gemini-api/docs/media-resolution

**Local models:**
- https://ollama.com/library/qwen3-vl
- https://docs.photoprism.app/developer-guide/vision/model-comparison/

**Hosting:**
- https://www.hetzner.com/pressroom/new-location-singapore/
- https://northflank.com/blog/hetzner-cloud-server-price-increases
- https://infoq.com/news/2026/07/oracle-cloud-free-tier-limits/
