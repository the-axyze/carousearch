# Carousearch roadmap

Written 2026-10-23. Effort is in solo-developer days. Each milestone ends with something you can use in Telegram. Background and evidence: [FEASIBILITY.md](FEASIBILITY.md), [CAROUSELL_ACCESS_NOTES.md](CAROUSELL_ACCESS_NOTES.md).

Rules for every milestone:

- Pure logic (mapping, parsing, scoring, rendering) is separate from I/O, takes its dependencies as arguments, and has unit tests.
- Carousell parsing is tested against **saved real responses** in `tests/fixtures/`, never against the live site in CI.
- Every Carousell call goes through one rate-limited session (at most 1 request/second, 2 to 3 in flight).
- Model ids, weights and limits live in config, not code.

---

## M0: client spike and fixtures (1 to 1.5 days)

**Goal:** prove the Carousell client in code and freeze real responses for tests.

Tasks:
1. `carousell/session.py`: browser User-Agent, cookie jar, CSRF bootstrap from one HTML page load (`initialState.Application.csrfToken`), re-bootstrap on 401/403/422, exponential backoff, rate limiter.
2. `carousell/search_api.py`: build the `/ds/search/cf/4.0/search/` body (query, filters, sortParam, count, session). Parse into `SearchPage(listings: list[ListingCard], total, next_session, applied_filters, category_cc_id)`.
3. `carousell/listing_api.py`: `/ds/listing-detail/3.1/listings/<id>/detail/` into `ListingDetail` (description, photos, timestamps, seller stats).
4. `carousell/html_fallback.py`: parse `initialState.SearchListing.listingCards` into the same `ListingCard` type. Detect the soft block (`listingCards: null`) vs genuinely empty results.
5. Save fixtures: 3 searches (furniture, phone, generic), 1 filter-service response with facets, 2 listing details, 1 SSR search page, 1 Cloudflare 403 page.
6. `scripts/canary.py`: one search plus one detail call. Exits non-zero on failure (later wired to a Telegram alert).

Acceptance criteria:
- `canary.py` passes from the home machine.
- Parser unit tests pass on all fixtures.
- A filtered search (price range, condition, deal method, sort) returns results that all satisfy the filters.
- Page 2 via `session` returns new, non-overlapping listings.

---

## Level 1: prompt to filter toggles (3 to 5 days)

**Goal:** "lightly used office chair under $300, meet-up only, newest first" becomes a correct Carousell search, shown in Telegram with editable filters and pagination.

Tasks:
1. `search/intent.py`: the `SearchIntent` pydantic model (FEASIBILITY section 4.3). Level 2 and 3 fields exist but are optional.
2. `search/to_carousell.py`: pure mapping from `SearchIntent` to the Carousell request (condition names to codes, sort names to codes, price, deal method, free delivery, Buyer Protection). Client-side `max_listing_age_days` cutoff using the card timestamp.
3. `carousell/categories.py`: category tree snapshot from `initialState.Collection.default` (id, cc_id, slug, name, parent), refreshed nightly. Category resolution order: Carousell suggestion endpoint, then `sfContext` from a probe search, then an LLM pick from the compact list.
4. `carousell/locations.py`: "near Tampines" to location filter IDs through the location lookup endpoint.
5. `llm/base.py` + `llm/anthropic_adapter.py` (Claude Haiku 5.5, structured output, low effort / thinking off) + one alternative adapter (`gpt-6-luna` or `gemini-2.5-flash-lite`).
6. `bot/`: python-telegram-bot app with a single-user filter. Prompt handler. Results message (10 per page). Keyboard with Prev/Next and filter chips (condition, price, sort, deal method). Each tap edits the message in place. Search state in SQLite under a short id, since `callback_data` is at most 64 bytes.
7. "Open in Carousell" button: build the equivalent `https://www.carousell.sg/search/...` URL.
8. Eval set: `tests/eval/prompts_level1.jsonl` with 50 real-style prompts and expected filters. Script reports field-level accuracy per model.
9. Deploy at home: systemd service in WSL2, Task Scheduler start at logon, cron watchdog + nightly canary.

Acceptance criteria:
- 45 of the 50 eval prompts map with all hard filters correct, on the chosen model.
- Median time from prompt to first results message is under 4 s.
- Every applied filter is visible as a chip and can be changed in at most 2 taps.
- Pagination works up to page 5 without re-running the LLM.
- Monthly LLM spend projection is under US$2 at 50 queries a day (logged token usage times price).

---

## Level 2: search-bar keywords and category-specific fields (2 to 4 days)

**Goal:** "iPhone 15 Pro 256GB local set, no cracks, under $800" fills the keyword plus the `mobile_model`, `mobile_storage` and `mobile_nationality` facets. The same works for furniture, shoes, laptops and cars.

Tasks:
1. `carousell/fieldsets.py`: get the facet schema for the resolved category from the `/ds/filter/cf/4.0/search/` response (`filters`). Normalise it into `FacetSchema(field_name, label, kind, options)`. Cache per cc_id for 24 h.
2. Pass 2 LLM call: user prompt + the facet schema, with options as **runtime-generated JSON-schema enums**. Output is `category_facets: dict[field_name, list[value]]`. Skip the call when the category has no facets.
3. Keyword cleanup: the keyword string keeps only what the facets don't cover ("iphone 15 pro", not "256gb local set under 800"). Add `exclude_keywords` as a client-side title/description filter.
4. Clarifying questions: if `ambiguities[]` holds a category-level ambiguity, ask one question with 2 to 4 inline buttons, then continue. Everything else is defaulted and shown as chips.
5. Facet chips in the keyboard (e.g. `Storage: 256GB`), editable through an option picker built from the cached `FacetSchema`.
6. Extend the eval set with 30 category-specific prompts (phones, furniture, shoes, laptops, cars).

Acceptance criteria:
- At least 25 of the 30 category prompts get the right category and all stated facets.
- No facet value outside Carousell's option list is ever sent (enforced by schema and asserted in tests).
- At most one clarifying question per search; prompts with no real ambiguity never trigger one.
- Combined Level 1 + 2 latency is under 6 s median.

---

## Level 3: screening and ranking (4 to 6 days)

**Goal:** results are re-ranked with badges and one-line reasons, based on seller trust, price sanity, photo quality, description quality and your soft criteria.

Tasks:
1. `screening/rules.py` (pure): hard drops (suspended seller, blocklisted keywords, out-of-range price, duplicates). Features: seller trust, price robust z-score vs result-set median, freshness, likes per day, Professional Seller flag. Perceptual-hash duplicate photos (`imagehash`) and near-duplicate descriptions across the result set.
2. Detail enrichment: fetch `ListingDetail` for the top 10 to 15 after the rules pass, 2 to 3 at a time. Cache details for 12 h and seller stats for 7 days.
3. `screening/vision.py`: downscale 1 to 2 photos to a 768 px long edge (or 384 px for the cheapest mode). One `ListingJudge` call per listing with a strict JSON schema: `photo_quality`, `condition_consistency`, `description_quality`, `scam_template_risk`, `soft_criteria_match`, each a 0 to 1 score with a one-line reason.
4. `screening/scoring.py` (pure): combine as in FEASIBILITY section 5.3. Weights in config. Badges.
5. Verdict cache keyed by (listing_id, last_modified, model, prompt_version).
6. Progressive rendering: send the unscreened top 10 immediately, then edit the message with the ranked list when screening finishes.
7. A labelled set of 100 listings (you tag good/bad photo, sketchy/fine) to calibrate weights and compare two models.
8. Budget guard: daily token and Carousell-request caps from config. If exceeded, fall back to rules-only ranking.

Acceptance criteria:
- On the labelled set, at least 80% of listings you tagged "bad photo" are in the bottom half for photo quality.
- Screening adds no more than 8 s to a search (median) for 15 listings.
- Carousell requests stay under 1,000 a day at 50 searches a day (logged).
- Projected model spend is under US$5 a month at 50 searches a day.
- Every down-ranked listing shows its reason.

---

## Optional: saved searches and alerts (1 to 2 days)

Tasks:
1. "Save this search" button stores the `SearchIntent` and resolved query.
2. JobQueue runs each saved search every 15 to 60 minutes, sorted by Recent. New listings (not in `seen_listings`) go through the Level 3 rules (and optionally the VLM) and are pushed if they pass.
3. `/alerts` command: list, pause or delete saved searches.

Acceptance criteria:
- No duplicate alerts.
- At most 1 Telegram message per alert batch (a list, not one message per listing).
- Total background Carousell traffic is under 300 requests a day for 5 saved searches.

---

## Cross-cutting: keeping it alive

- Daily canary at 08:00. On failure, a Telegram message to you with the HTTP status and which fallback was used.
- When the canary fails twice: switch the client to the HTML fallback automatically. If that also fails, to Playwright (install Playwright only at that point).
- Re-run the eval sets whenever the model id or a prompt changes.
- Check model deprecation pages quarterly (two OpenAI small models in the comparison retire between today and 2027-01-14).

---

## Phase 1 status (2026-10-24)

Built: the M0 client spike, as a tested package plus CLI (`README.md` has usage).

- `filters/schema.py`: `SearchRequest` (query, category id, price range, conditions, deal methods, Buyer Protection, free delivery, sort, `category_fields`, page size and cursor), `Listing`, `ListingDetail`, `SearchResult`. `filters/payload.py` is the single `SearchRequest` to search-body translation. Enum values are semantic (`like_new`, `recent`), so the Level 1 LLM can target this model directly or through a thin `SearchIntent` mapping.
- `carousell/client.py`: home-page bootstrap (cookies + CSRF), one automatic re-bootstrap on 401/403/422, rate limiter (1.0 s ± 0.3 s), `search`, `get_listing`, and typed errors (`CarousellBlockedError`, `CarousellSchemaError`, `CarousellHTTPError`). `carousell/parse.py` holds all response parsing as pure functions.
- `storage.py`: SQLite `seen_listings`, `listing_detail_cache`, `saved_searches`.
- CLI: `carousearch search | listing | canary | capture-fixtures`.
- Tests: 99 unit tests on real fixtures captured 2026-10-24 (two filtered searches, one listing detail, the bootstrap page, a real Cloudflare challenge), plus 1 live test (`pytest -m live`).

M0 acceptance: canary passes from the home machine; parser tests pass on all fixtures; a filtered search (price range, condition, sort) returned only matching listings, except that Carousell also returns old listings with the legacy "Used" condition (CAROUSELL_ACCESS_NOTES section 10, item 6); page 2 via `session` returned new, non-overlapping listings.

Deferred:

- HTML fallback (M0 task 4) and Playwright fallback: not built; a TODO in `CarousellClient.search` marks where they go. No SSR-page or filter-service (`/ds/filter/...` facet schema) fixture yet.
- Retries: only the single re-bootstrap. No exponential backoff and no concurrency (requests are sequential).
- "Posted within N days": Carousell has no server-side filter. Apply it client-side on `Listing.posted_or_bumped_at` with sort `recent` in Level 1.
- Category tree, category slug lookup, location filter and search suggestions (Level 1 tasks 3 and 4). `SearchRequest.category` takes a numeric collection id for now.
- Canary alerting (Telegram) and the cron entry itself (the README has the line to install).

Module layout differs from FEASIBILITY section 6.1: `session.py`, `search_api.py` and `listing_api.py` became `carousell/client.py` (I/O) plus `carousell/parse.py` (pure), and the shared model lives in `filters/schema.py`.

---

## Phase 2 task 1 status (2026-10-24)

Built: a single-user Telegram bot with manual filter buttons, on python-telegram-bot 22.8 (`README.md`, "Running the Telegram bot"). No LLM yet.

- `bot/state.py` (pure): `ChatDraft` (the `SearchRequest` being edited, the page cursor stack, listing ids shown in this chat, and any typed input being waited for). Button presses are `Action(kind, value)` values encoded as `kind:value` callback_data (at most 64 bytes, checked in `encode_action`). `apply_action` applies the filter actions; `record_page_shown` and `next_page_request` handle paging.
- `bot/keyboard.py` (pure): filter panel layout (price presets and custom, five conditions, meet-up / mailing, sort cycle, six common categories plus any collection id, Reset, Search), the summary text, and the card, paging and saved-search keyboards.
- `bot/messages.py` (pure): listing captions with the `[seen]` tag, result footers, help, prompts, and error texts (blocked, site changed, generic).
- `bot/handlers.py`: thin handlers behind two decorators, `report_errors_to_user` and `owner_only`. Dependencies arrive as one `BotServices` in `Application.bot_data`; the synchronous Carousell client runs through `asyncio.to_thread`. Search results go to `seen_listings`; Details uses `listing_detail_cache` (12 hours) before calling Carousell; `/save` and `/saved` use `saved_searches`.
- `bot/app.py`: `build_application`, `run_polling`, logging set-up (httpx is kept at WARNING because Telegram URLs contain the bot token), and the `whoami` helper.
- `config.py`: `TELEGRAM_BOT_TOKEN` and `TELEGRAM_OWNER_ID` from the environment or `.env` (python-dotenv). CLI: `carousearch bot` and `carousearch whoami`.
- Tests: 180 offline (99 from phase 1 plus 81 new: state, keyboard, handlers with a fake bot and the fixture-backed fake Carousell, config, CLI).

For the next task: `llm/prompt_to_filters.py` should return a `SearchRequest` and hand it to `handlers.show_filter_panel_for_request(bot, services, chat_id, request)`, the same entry point that typed keywords and saved searches use. The panel and summary already show every `SearchRequest` field, including `category_fields`, Buyer Protection and free delivery.

Differences from Level 1 task 6 as planned above:

- Drafts live in memory (`DraftStore`), not SQLite, so a restart forgets the draft and old buttons answer "There is no search in progress". callback_data carries the action and a small value instead of a stored search id.
- 5 results per page, each listing its own photo message with a Details button, then a paging message. Carousell is asked for `count` 5, the page size the fixtures were captured with.
- The sort button cycles best match, recent, price low to high, price high to low. Nearby is left out because there is no location filter yet.
- The category buttons are a hand-picked list from CAROUSELL_ACCESS_NOTES section 4.8; "Other id…" takes any numeric collection id. Category names are not resolved yet (Level 1 task 3).

Not done yet:

- Not run against the real Telegram API (no login available while building). All Telegram calls are covered by the fake-bot tests only.
- Prev page, an "Open in Carousell" URL button, deleting saved searches, Buyer Protection and free delivery toggles, and facet chips (Level 2).
- Cards use Carousell's 240 x 320 thumbnail; the full-size photo URL is not derived yet.
- The always-on service (systemd or Task Scheduler) and the canary alert to Telegram.

---

## Phase 2 task 1b status (2026-10-25)

Built: the single filter panel is replaced by a step-by-step wizard, and each chat runs one search at a time. This follows the owner's first live use ("the options are all over the place", "I can keep typing /search"). `README.md`, "Running the Telegram bot", describes the flow.

- `bot/state.py` (pure): `SearchSession` replaces `ChatDraft`. It holds the request, a `Stage` (KEYWORDS, PRICE, CONDITION, DELIVERY, SORT, CATEGORY, SUMMARY, RESULTS), `return_to_summary` for Edit buttons, any typed input being waited for, the page cursors, `last_activity_at`, and the id of the session's current message. Transitions are pure functions: `start_session`, `start_session_at_summary`, `apply_action` (None when Cancel or End ends the session), `apply_text_input` (returns the problem to show for bad input), `next_stage` / `previous_stage`, `is_expired`. A button that belongs to another stage raises ValueError.
- `bot/price_input.py`: `parse_price_range` returns None for bad input, and now also reads `>1000` and en dashes.
- `bot/keyboard.py`: one keyboard function per stage behind `build_keyboard_for_stage`; `render_stage_header` ("Step 3/6: Condition" plus a "So far: …" recap) and `describe_stage_position`.
- `bot/messages.py`: labels, the request summary, step questions, and the lock reminder, status, expiry and validation texts.
- `bot/session_store.py` (was `draft_store.py`): `SessionStore` with an injected clock. A session idle for 30 minutes is hidden by `get` and dropped by `remove_if_expired`.
- `bot/handlers.py`: `show_current_stage` draws any stage, editing the tapped message or sending a new one. `/end` and `/status` are new. `/start`, `/search`, new keywords and loading a saved search are refused with the lock reminder while a session is active. `show_filter_panel_for_request` stays the LLM entry point and now starts a session at the summary, so a plain-English prompt skips the wizard.
- Tests: 367 offline (bot tests went from 75 to 152: state 102, keyboard 16, handlers 34).

Differences from the brief for this task:

- `[seen]` tags come from `seen_listings` (what `record_seen_listings` returns) instead of a per-session set, because ending a session would otherwise forget them. They now survive `/end` and restarts, and also count listings shown by `carousearch search`.
- One `EDIT` action carries the step as its value, instead of an `EDIT_<step>` kind per step. The summary's Edit buttons are generated from `WIZARD_STEPS`, so a new step gets one automatically; that adds **Edit keywords**, which the brief did not list.
- `SearchSession.request` is None while a session started by a bare `/search` waits for its first keywords, because a `SearchRequest` needs a query or a category.
- The session records its current message, and buttons on any other message are rejected (and their keyboard removed). This covers double taps and old messages from ended sessions.
- After typed text the next step goes in a new message, because the typed reply sits below the old one; the old step's buttons are removed. Taps edit in place.
- Back while typing a price or id returns to that step's buttons. Back on a step opened from the summary returns to the summary. The summary also has Back (to Category).
- A typed collection id ("Other id…") is consumed like typed keywords and prices.

Not done: no run against the live bot yet; sessions are still in memory, so a restart forgets them.

---

## Next: market analysis and staged screening (2026-10-25)

The next body of work is specified in [ANALYSIS_AND_SCREENING_PLAN.md](ANALYSIS_AND_SCREENING_PLAN.md): phases A to F covering harvest-and-upsert storage, the analysis spec and CLI, the LLM layer (prompt to filters, plan and summarise an analysis), read-only SQL questions, the per-chat screening pipeline (rule, text and vision stages with cached, auditable verdicts) and scheduled surveys. That document supersedes the Level 3 section above; Level 1 task 5 and the "next task" note under phase 2 are its phase B.

### Phase A status (2026-10-25)

Built, on `feature/market-analysis-a`: the `listings` / `listing_changes` / `harvest_runs` / `harvest_run_listings` tables with upsert-by-content-hash, `market/hashing.py`, `market/limits.py`, `market/harvest.py` (multi-page pull, coverage-aware reuse of a recent run, pure `decide_stop` and `run_covers`), `market/analysis.py` and `market/render.py` (spec, price statistics, text/JSON output), and the `carousearch harvest` and `carousearch analyze --spec` commands with `docs/examples/analysis_spec.example.json`. 110 new offline tests (290 total).

Differences from the plan, all written back into it: `HarvestLimits` lives in `market/limits.py` (keeps `analysis.py` free of the I/O module); reuse checks that the earlier run covers the requested limits (`limits_json` on `harvest_runs`, `find_recent_runs` + `run_covers`) rather than matching the request hash alone; the bump timestamp is refreshed on every pull like likes; `AnalysisSpec` rejects `limits.max_age_days` without `sort=recent` before any request; a harvest closes its run as `error` on any exception, not only Carousell errors; week groups are ISO weeks in Singapore time; `decide_stop` takes `pages_fetched` (1-based) rather than a page index.

Deferred: capturing a real two-page `sort=recent` fixture pair (`search_recent_page_1/2.json`) needs a machine that can reach carousell.sg; `test_harvest.py` builds its pages from the existing fixture until then. The acceptance run of the example spec against the live site is also still to be done from the home machine.

---

## Phase 3a status (2026-10-25)

Built: screening for multi-item "catalogue" posts, whose card price is usually the cheapest variant's, plus an optional Claude lookup of the searched variant's real price. `README.md`, "Screening", has the tags, the cost estimate and the detail budget.

- `screening/models.py`: `Tag` (`code`, `label`, `reason`, `severity`), `ResolvedPrice` (`amount`, `source` listed / description / not_stated, `note`) and `ScreenedListing` (`listing`, `detail`, `tags`, `resolved_price`, computed `is_multi_item`, `summary_line()`). Labels and severities per code live in two tables next to the enum. This is the contract the web front end will consume.
- `screening/rules.py` (pure): `title_lists_multiple_variants`, `description_has_price_list`, `listed_price_is_floor`, `price_below_market`, `price_not_for_your_item`, and `extract_prices` / `find_price_mentions`. Thresholds are one constants block at the top.
- `screening/price_resolver.py`: `PriceResolver` protocol, `ClaudePriceResolver` (`claude-haiku-5-5`, `output_config.format` JSON schema, thinking off, effort low, `max_tokens` 256, description cut to 1,500 characters), `NoopPriceResolver`, `build_price_resolver(settings)`. API failures raise `PriceResolutionError`; unusable answers become `not_stated` with a note.
- `screening/store.py`: `ScreeningStore` protocol and `SqliteScreeningStore` (12-hour `listing_detail_cache`, new `price_resolution_cache` keyed by listing id and normalised keywords).
- `screening/pipeline.py`: `screen_listings(listings, request, services, fetch_details=True)` with `ScreeningServices(client, store, resolver, clock, max_detail_fetches=10)`; `screen_without_details` is the request-free first step.
- CLI: `search --screen` (YOUR PRICE and FLAGS columns, a one-line screening summary), `--hide-multi-item`, `--max-detail-fetches`; `--json` prints `screened_listings`. Bot: multi-item cards get `summary_line()` as the caption's last line (title rule only, no extra requests; 10 added lines in `handlers.py`, 4 in `messages.py`).
- `config.py`: `ScreeningSettings` / `load_screening_settings` (optional `ANTHROPIC_API_KEY` from the environment or `.env`). `pyproject.toml`: new optional extra `llm = ["anthropic>=2.3"]`, imported lazily.
- Tests: 507 offline (367 before; 140 new: rules 95, resolver 19, pipeline 14, models 4, storage 2, CLI 5, bot 1).

Live check, 2026-10-25, `carousearch search "iphone 15 pro 256gb" --price-max 1200 --limit 20 --screen`: all 20 titles were single-variant and none was tagged multi-item; the one S$8.88 placeholder ("dm for info") was tagged below market, its description fetched (1 detail request) and correctly left un-flagged. No API key was configured, so resolution ran as Noop. The title rule was checked against real catalogue titles from the bot's searches ("Nintendo Switch OLED / Switch 2 / Switch Console", the motorbike dealers' "| ADV150 | R15 | MT15 |" keyword lists), which it tags.

Differences from the brief and from ANALYSIS_AND_SCREENING_PLAN.md:

- This is a narrower first slice than the plan's phase D (stage pipeline with thresholds, text judge, verdict cache): fixed rules plus one LLM lookup, no user-defined stages. `screening/pipeline.py` here orchestrates injected I/O instead of being pure; the plan's `runner.py` / `pipeline.py` split can come with phase D.
- `listed_price_is_floor(listed_price, description)` takes the description text, not pre-extracted amounts, because the "from $X" wording is part of its evidence.
- `PriceResolver.resolve` takes `listed_price: float | None`, since a card price can be non-numeric.
- `fetch_details=False` runs step 1 only (no detail lookups, no resolver), which is what the bot uses.
- Resolver answers are cached only when they come from the description (`description` or `not_stated`); a Noop `listed` result or a failed call is not, so adding a key later still resolves those listings.
- Bare numbers count as prices only in a price context ("selling at 850", "850 nett", "15 Pro - 850"); "selling 1999" style four-digit prices in a "<label> - <number>" line without "$" are read as years and dropped.

Deferred: a live run with an API key (the resolver's prompt and the cost estimate are untested against the real API); capturing a real catalogue-post fixture (search card plus detail) for the tests; the web front end; using `is_multi_item` in ranking or the bot's filters.
