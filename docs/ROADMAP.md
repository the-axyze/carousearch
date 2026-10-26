# Carousearch roadmap

> 2026-10-26: [NEXT_STEPS.md](NEXT_STEPS.md) is now the live plan. This file keeps the original milestones and the dated status history.

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

---

## Phase 3b status (2026-10-25)

Built: a local web front end, `carousearch web` (http://localhost:8765, this machine only, no login). One page: a search box, one row of optional filters (price range, condition and delivery chips, sort, category, "Hide multi-item posts"), and a card grid of screened results whose price row shows when a catalogue post's listed price may not apply. `README.md`, "Web app", has the controls and the price-row legend. The Telegram bot stays for quick lookups and the alerts planned later.

- Stack: FastAPI + Jinja2 + HTMX 2.0.11 (one vendored file, `web/static/htmx.min.js`), one hand-written `app.css` (light and dark themes from tokens on `:root`), 39 lines of `app.js` (the `/` and Esc keys, the Details toggle, form state for history snapshots). No build step. New core dependencies: `fastapi`, `jinja2`, `uvicorn`.
- `web/query_params.py` (pure): `search_request_from_params` / `params_from_search_request`, the one URL <-> `SearchRequest` mapping (reusing the `filters/schema.py` enums and the bot's sort choices); malformed values are dropped, a reversed price range is swapped, an unknown sort is best match. `page_url` and `load_more_url`; `ResultsTally` carries the counts from one "Load more" page to the next.
- `web/view_models.py` (pure): the price row (`listed`, `resolved` with the listed price struck, `not-stated`), the Multi-item and Low price badges with their reasons, the meta line, the results line, the form and the detail panel (dates in Singapore time), and the calm error messages. Templates only print these.
- `web/services.py`: `WebServices` (Carousell client, `ScreeningServices`, whether price lookup is on, two short memories, and an asyncio lock) and the two blocking calls, `search_and_screen` (search, then `screen_listings`) and `load_listing` (detail from the screening, the 12-hour cache or Carousell). Routes run them in a worker thread one at a time, and skip a call whose browser request was already replaced.
- `web/app.py`: `create_app(settings, services_factory=None)` builds the services once at startup (lifespan), keeps them in `app.state`, mounts `/static`; `web/routes.py`: `/`, `/results` (HTMX partial; pushes the page URL with `HX-Push-Url`; with `cursor` it returns cards to append plus out-of-band updates of the results line and the Load more button), `/listing/{id}`. Carousell and unexpected errors render a calm message with status 200; the traceback goes to the log only.
- Tests: 597 offline (90 new: query params 42, view models 26, routes 20 with `TestClient` and fake Carousell, store and resolver, CLI 2).

Live check, 2026-10-25: `curl "localhost:8765/results?q=iphone+15+pro+256gb&price_max=1200"` returned 20 cards in 1.7 s (one search request, one detail from the cache); the Details partial of the first card rendered. Screenshots at 1440 and 390 px wide: `docs/examples/web-desktop.png`, `docs/examples/web-mobile.png`. That search had no catalogue posts, so the amber badge and the resolved price were checked against an offline demo page instead.

Differences from the brief for this phase:

- `search_request_from_params` returns `SearchRequest | None`: None when there are no keywords and no category, because a `SearchRequest` needs one of them. The page then shows a hint.
- One extra module, `web/services.py`, so the routes stay thin; and two extra templates, `partials/more_results.html` (the Load-more answer) and `partials/load_more.html` (the button, shared by both answers).
- `storage.open_database` takes `check_same_thread` (default unchanged), because the web app shares one SQLite connection across worker threads; the lock keeps it to one thread at a time.
- A repeat of the same search within 2 minutes is served from memory (the switch, Back, a debounced keystroke landing after Enter), to spare Carousell requests.
- The content width is 1,240 px rather than 1,200 px, and the controls use 12.5 px text, so the whole filter row fits on one line at 1,440 px. Below 640 px the grid is two fixed columns and the filters become one sideways-scrolling line with the switch underneath.
- The results line adds "price lookup off" when there are multi-item posts and no `ANTHROPIC_API_KEY`.
- The badge's native tooltip shows on hover only (browsers do not show `title` on keyboard focus); focused badges carry the reasons as screen-reader text.
- The web app does not record `seen_listings`, so it does not change the bot's `[seen]` tags.

Deferred: a live look at a real catalogue post with an API key; Buyer Protection, free delivery and category facets in the web filters; saved searches in the web app; per-search page titles; Carousell's full-size photos on cards (cards use the 240 x 320 thumbnail).


---

## MCP server: phase A status (2026-10-26)

Built: `carousearch mcp`, a thin [MCP](https://modelcontextprotocol.io) server through which an agent host (Claude Code, Codex CLI, the Claude desktop app, ChatGPT through a tunnel) searches Carousell with carousearch's client. [MCP.md](MCP.md) has the host setup, example prompts and the request budget. (Not to be confused with phase A of the market-analysis plan above.)

**Filtering, vision and planning stay with the host, by design.** The server only connects: it has no filtering, scoring, LLM calls or planning tools. The host writes its own regexes over the returned JSON, looks at photos with its own model, ranks, and decides which calls to make. That keeps the server small, and a better host model improves the results without any change here. The rule-based `screening_tags` is exposed only because those rules already existed.

- `mcp_server.py`: `build_server(services) -> MCPServer` with five read-only tools: `search_listings` (`CarousellClient.search`; `limit` and `page_token` set the page size and cursor), `listing_details` (`get_listing` through the 12-hour detail cache, `SqliteScreeningStore`), `listing_photo` (https on `media.karousell.com` only, scaled to `max_width`), `screening_tags` (the title and description rules for one listing) and `usage_notes` (`mcp_instructions.md`, also sent as the server's instructions on connect). `McpServices` holds the injected Carousell source, detail cache, clock and a lock: the SDK runs synchronous tools in worker threads, and the lock keeps them to one Carousell or database call at a time. Carousell errors become `ToolError`s whose text the host reads; a block tells it to stop.
- `carousell/client.py`: `get_photo(url)` through the shared rate limiter, refusing anything but https on `media.karousell.com` (`is_carousell_media_url`). `photos.py` (pure): `downsize_photo`.
- CLI: `carousearch mcp [--transport stdio|http] [--port 8766]` (`config.DEFAULT_MCP_HTTP_PORT`). Only this command imports the MCP SDK, which costs about half a second of start-up.
- Dependencies: `mcp>=2.3,<3`, `pillow>=13`.
- Tests: 632 offline (597 before; 35 new: MCP server 14, through the SDK's in-process client with a fake Carousell and an in-memory cache; client photo download 12; photos 6; CLI 3, one of which serves a real `CarousellClient` over the fake transport with the SQLite cache across worker threads).

Live check, 2026-10-26: the stdio server, driven by the SDK's client, listed its five tools; `search_listings` for "nintendo switch" with limit 5 returned 5 listings, the first "Nintendo Switch Console gen 1 modded with Joy-Cons and Box" at S$250; `listing_photo` on its thumbnail returned a 320 x 240 JPEG (16,817 bytes, no downsizing needed). Three Carousell requests: the home page, the search, the photo.

Differences from the brief for this phase:

- mcp 2.3 renamed FastMCP to `MCPServer` (`mcp.server.mcpserver`, which also has `Image`). The server uses it rather than pinning `mcp<2`.
- `build_server` takes the services rather than settings; `run_mcp_command` opens the client and the database and closes them when the server stops, as `carousearch bot` does.
- The photo download is a new `CarousellClient.get_photo`, so it shares the rate limiter. The host check runs in the tool (a refused URL costs no request) and again in the client.
- `screening_tags` takes an optional `query`, which the title rule uses to tell variants apart ("Switch / Switch Lite").
- Every tool is annotated read-only and open-world. With `--transport http` a tunnel must rewrite the Host header to `localhost:8766`, because the SDK accepts only local Host headers on a server bound to 127.0.0.1.

Deferred: registering and trying the server in a real host (Claude Code, Codex, the desktop app, ChatGPT); a tunnel and any authentication for the HTTP transport; caching photos; counting requests per task (the 40-request budget is advice in the usage notes; only the rate is enforced).

---

## MCP server: phase B status, the in-app agent (2026-10-26)

Built: `carousearch ask "<prompt>" [--json] [--model provider:model] [--max-photos N]`, an agent that answers a plain-English shopping prompt by calling the MCP server's own five tools. It prints the tool trace, a kept table, every dropped listing with its reason, and notes. `README.md`, "Ask the agent", has the usage and settings.

**Reuse, not a second tool set.** The agent's tools are the MCP server's tools: `run_prompt` hands `mcp_server.build_server(services)` to Pydantic AI's `MCPToolset`, which reaches it in this process through FastMCP's in-memory transport (it accepts the MCP SDK's own `MCPServer`). No subprocess, no second interpreter start-up. The server's usage notes become part of the agent's instructions (`include_instructions=True`).

- `agent/settings.py`: `AgentSettings` (`model`, `budget`, `anthropic_api_key`, `openai_api_key`) and `AgentBudget` (`max_tool_calls` 40, `max_photos` 15, `max_listings` 200), read from the environment or `.env` (`AGENT_MODEL`, `AGENT_MAX_TOOL_CALLS`, `AGENT_MAX_PHOTOS`, `AGENT_MAX_LISTINGS`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`). `missing_key_variable()` names the key the model's provider needs; providers outside the Anthropic / OpenAI table are left to Pydantic AI's own environment lookup.
- `agent/prompt.py`: `SYSTEM_PROMPT`, a plain string with the budget numbers filled in: cheapest work first (search, filter the JSON in its head, details for the shortlist, photos last), every drop kept with its reason, the budgets and what to do when one is reached, the answer's contract, and seller text treated as data.
- `agent/result.py`: `ModelAnswer` (`kept: list[KeptListing]`, `dropped: list[DroppedListing]`, `notes`) is the structured output the model returns through a `final_answer` output tool; `AgentAnswer` adds `tool_trace: list[ToolCall]` (`tool_name`, `arguments` summary, `duration_seconds`, `status`). `build_agent_answer` appends the run's budget notes to the model's notes.
- `agent/budget.py` (pure): `ToolBudget.admit(tool_name, arguments)` counts the call or raises `BudgetExhausted`; a search is cut to the listings left; `notes()` gives one line per cap reached.
- `agent/trace.py` (pure): `build_tool_trace(messages, records, output_tool_name)`: the calls and their order come from the run's message history, and the duration, status and arguments actually sent come from the hook's records, keyed by tool-call id.
- `agent/run_prompt.py`: `run_prompt` (async) and `run_prompt_sync`; `ToolCallGate`, the toolset's `process_tool_call` hook, admits and times every call; `build_model` turns `AGENT_MODEL` into a model with `infer_model` and passes the key from settings to the provider.
- CLI: `ask` in `cli.py` (Pydantic AI and the MCP SDK imported only by this command); the text rendering is `render_agent_answer` in `text_render.py`. A missing provider key exits 2 before any request; a failed model request exits 1.
- Dependency: `pydantic-ai-slim[anthropic,mcp,openai]>=2.61,<3` (2.61.0 installed).
- Tests: 679 offline (632 before; 47 new: settings 14, budget 8, trace 5, answer and rendering 4, scripted runs 9, CLI 7). The scripted runs drive the real in-process MCP server over a fake Carousell with Pydantic AI's `FunctionModel`, so they need no network and no key.

How the budgets are enforced. Pydantic AI's `UsageLimits` raises `UsageLimitExceeded` and ends the run with no answer, so it is only the backstop. The main mechanism is the hook. A call over a cap is answered with a failed tool result (`ToolFailed`) telling the model to finish with what it has, and the call never reaches Carousell. The run's notes then name the cap. A model that keeps calling tools regardless is stopped by `UsageLimits(request_limit=max_tool_calls + 5)`, and the answer says so, with the trace of what was looked at. Tool errors (Carousell blocking, a refused photo URL) come back as failed results (`tool_error_behavior="failed"`), so they do not use up the model's retries.

Photos: an MCP `ImageContent` becomes a `BinaryImage` in the tool result, which Pydantic AI sends to Anthropic and to OpenAI's Responses API inside the tool result, and to chat-only APIs as a separate user message. No workaround was needed; a test checks that the model receives an image.

Pydantic AI APIs used, checked against the docs and the installed 2.61.0 source:

- `MCPToolset(server, include_instructions=True, tool_error_behavior="failed", process_tool_call=...)`: https://pydantic.dev/docs/ai/mcp/client/
- `UsageLimits(request_limit=...)` and `UsageLimitExceeded`: https://pydantic.dev/docs/ai/agents/ (usage limits)
- Images returned by tools: https://pydantic.dev/docs/ai/tools-advanced/
- `FunctionModel`, `capture_run_messages`, `models.ALLOW_MODEL_REQUESTS`: https://pydantic.dev/docs/ai/testing/
- `Agent(model, output_type=ToolOutput(ModelAnswer, name="final_answer"), instructions=..., toolsets=[...])` and `models.infer_model(name, provider_factory=...)`, from the installed source.

Live check: not run. No `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` is configured on this machine, so the agent has only been exercised with scripted models. The model and provider plumbing (`infer_model`, keys from `.env`) is untested against a real API.

Differences from the brief for this phase:

- The package is `pydantic-ai-slim` with the `anthropic`, `mcp` and `openai` extras. It is the same library as `pydantic-ai`, without the 30 or so extra packages the full package pulls in (Google, Logfire, evals and others).
- Two extra pure modules, `budget.py` and `trace.py`, keep the counting and the trace building out of `run_prompt.py`. The text rendering sits in `text_render.py` with the other terminal output, reusing its column helpers.
- A third budget setting, `AGENT_MAX_LISTINGS` (default 200, about four pages of 48): the brief asked for a listings cap but gave no default. It counts the page sizes asked for, not the listings Carousell returned.
- `run_prompt` is the async core and `run_prompt_sync` the wrapper, following Pydantic AI's `run` / `run_sync`. Both take an optional `model` that replaces the one the settings name, which is how the tests inject a scripted model.
- `ToolCall` has a `status` (`ok`, `failed`, `refused`, `not run`) so the trace shows refused calls. Durations are measured in the hook, because the message history only stamps when a tool returned, and the server's lock runs parallel calls one at a time.
- The kept table's columns are price, title, seller, URL, reason: the reason comes last so it is never cut.
- `usage_notes` stays a tool (the agent sees exactly the five), but its text is already in the instructions and the prompt tells the model not to call it.
- A missing provider key exits with code 2, like the bot's missing settings.

Deferred: a live run with an API key, including the photo verdict on real listings and the cost per run; showing progress during a run (the trace is printed at the end); prompt caching for longer runs; the agent in the web app and the Telegram bot.

---

## Web app: phase C status, prompt mode (2026-10-26)

Built on branch `feature/phase-c-prompt-mode` (PR pending): **Ask mode** in the web app, which runs the `carousearch ask` agent from the browser, shows its progress while it runs and its answer as cards; and, in Filters mode, a **Hidden (n)** section that lists the posts "Hide multi-item posts" hides, with their reasons. `README.md`, "Web app", has the usage ("Ask mode" and the controls table). The plan was [NEXT_STEPS.md](NEXT_STEPS.md), section 4.1.

**Reuse, not a second agent.** The web app calls the same `agent.run_prompt.run_prompt` over the same in-process MCP server; there are no new tools and no second agent loop. The only addition to the agent is an optional observer that is told about each tool call.

- C1, `agent/progress.py` (pure): `ToolCallEvent` (phase `started` or `finished`, tool name, arguments summary, status, which is None while started, duration, and `BudgetUsed` with the calls, photos and listings used of their caps) and the `ProgressObserver` Protocol. A finished, ok `search_listings` event carries the `Listing`s the tool returned, rebuilt from its structured content dict by `listings_from_search_result` (a listing that does not validate is skipped with a warning). `run_prompt` and `run_prompt_sync` take a keyword `observer`; `ToolCallGate` sends `started` once a call is admitted and `finished` for every call, refused ones included (a refused call has no `started` event). An observer that raises is logged and never breaks the run. `ask` passes no observer and is unchanged.
- C2, `web/prompt_runs.py`: `PromptRun` (id, prompt, started and finished times, status `running`, `done` or `failed`, the events so far, the listings finished searches returned, by id, the answer or a calm error message); `trace_lines`, one line per call from the events; `PromptRunRegistry`, the newest 10 runs in memory with at most one running; `start_prompt_run` and `execute_prompt_run`, the only place in `web/` that calls `run_prompt`. A run is an asyncio task in the web process. It holds `WebServices.turn` for the whole run and builds `McpServices(carousell=services.carousell, detail_cache=services.screening.store, clock=...)`, so it shares the web app's client, rate limiter and 12-hour detail cache. `AgentRunError` and `UserError`, `CarousellError`, cancellation and any other exception end the run as failed with a calm message; the details go to the log. Pydantic AI and the MCP SDK are imported only when a run starts, so `import carousearch.cli` stays cheap (tested). `WebSettings.agent` is the `AgentSettings` that `carousearch web` loads with `load_agent_settings()` (a malformed value exits 2, as for `ask`); `WebServices` gained `agent`, `agent_model` (tests inject a scripted model) and `prompt_runs`. `web/query_params.py`: `PageMode` and `mode_from_params` (`mode=ask` is Ask, anything else Filters).
- C3, `web/prompt_view_models.py` (pure) and the templates: the form view (model line "Model: <AGENT_MODEL>, from AGENT_MODEL", the budgets, the missing-key message), the progress view, the answer (kept cards, dropped rows, notes, trace), the failed view and the waiting view. Templates: a Filters | Ask switch in `index.html` (two radios styled as tabs; CSS shows one section; `app.js` keeps the address at `/?mode=ask` and restores the Filters address with its search when switching back), and the partials `ask_progress`, `ask_answer`, `ask_card`, `ask_trace_row`, `ask_failed`, `ask_message` and `ask_waiting`. Keyboard: Ctrl+Enter or Cmd+Enter in the prompt runs it; `/` focuses the current mode's input.
- C4, `web/view_models.py`: `HiddenRowView` and `hidden_rows`. With "Hide multi-item posts" on, `partials/results.html` adds a collapsed **Hidden (n)** section under the grid: each hidden post's title (Carousell, new tab), listed price and one "Label: reason" line per warn tag (the text of the Multi-item badge's tooltip). Load more appends that page's hidden posts out of band and replaces the count. "Hidden (0)" when the switch is on and nothing was hidden; no section when it is off. The results line still says "N multi-item hidden".
- C5: this paragraph, README's "Ask mode" and controls table, NEXT_STEPS, CLAUDE.md, `.env.example`.
- Dependency: `python-multipart>=0.0.20` (for FastAPI's `Form`; 0.0.32 was already installed with `mcp`, now named).
- Tests: 765 offline (679 before; 86 new: 18 for C1, 59 for C2 and C3, 9 for C4). The scripted-model helpers moved to `tests/scripted_model.py`, shared by the agent and web tests.

How a run works. `POST /ask` (from the page's HTMX form) checks the request and starts the task, then returns the progress partial. That partial asks `GET /ask/{run_id}` for itself every second (`hx-trigger="every 1s"`, `outerHTML` swap) and gets back the status line ("Running · 12 s · 2 tool calls"), "calls n of 40 · photos n of 15 · listings n of 200" and one trace line per call (`running`, `ok`, `failed` or `refused`, with the duration), until the answer or the failure, which have no polling attributes, replace it. A kept card comes from the `Listing` a search in this run returned (thumbnail, `price_display`, condition · likes · seller) plus the model's reason, with a Details button that opens the Filters-mode panel; a kept id that no search returned keeps the model's title, price and link (http or https only) with an **Unverified** badge and a warning. Below the cards are a collapsed "Dropped (n)" (title link, price when seen, reason) and "Tool calls (n)". No server-sent events, websockets or new JavaScript dependency.

Refusals and concurrency. `POST /ask` starts nothing and answers with a message when: it lacks `HX-Request: true` (403), the provider key is missing (409), the prompt is empty or over 2,000 characters (422), or a run is in progress (409; the first run's progress keeps showing under the message). Without the key the page names the variable (`AgentSettings.missing_key_variable()`), says to add it to `.env` and restart `carousearch web`, and disables Run. `GET /?mode=ask` shows the form and the run in flight, if any; no GET request starts a run. While a run holds the turn, filter searches (`/results` and a full page with a search) and listing details answer at once with a waiting partial ("An Ask run is in progress; your search will run when it finishes.") that asks again every 2 s, so they never overlap an agent tool call (a fake client that fails on overlapping calls proves it) and never hang. On shutdown the registry cancels the run in flight and waits at most 5 s for its tool call to end, before the client closes (tested).

Live check: not run. There is no provider key, no `.env` and no network to Carousell in the environment where phase C was built, so Ask mode has run only with scripted models through FastAPI's `TestClient`. It has never called a real model (that is phase L) or Carousell. Nor has it been opened in a real browser: the mode switch, the CSS (dark theme, phone width), `app.js`, HTMX polling and the out-of-band swaps were checked only in tests and, for the C4 swaps, a jsdom replay the implementer reported (not kept in the repo). No screenshots were taken.

Differences from the plan (NEXT_STEPS 4.1):

- The callback is `observer`, a Protocol that receives a `ToolCallEvent`, not `on_tool_call`.
- "Filter searches wait" is a waiting partial that asks again every 2 s, not a request queued behind the run, which could wait for minutes and time out.
- `POST /ask` also refuses requests without `HX-Request: true` (403), so another site cannot post a form to localhost and spend the API budget.
- `CarousellSource` in `web/services.py` gained `get_photo`, for the agent's `listing_photo` tool.
- Three partials beyond the plan: `ask_failed`, `ask_message` and `ask_trace_row`.
- `python-multipart` is named as a dependency.
- The Ask view models are in a new `web/prompt_view_models.py`, because `view_models.py` would have grown too long.
- `trace_lines` matches a finished event to the oldest running line with the same tool and arguments, for parallel calls.
- After a reload, Ask mode shows the run in flight but not a finished run's answer: the registry still holds it, but nothing shows it.

Deferred: one real prompt in the browser with desktop and mobile screenshots in `docs/examples/`, after phase L; showing the last finished answer after a reload (open question Q13); disabling Run while a run is in progress instead of refusing a second one (Q12); keeping runs across a restart; token usage and cost per run (H9).
