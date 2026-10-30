# carousearch

Carousearch is a planned personal Telegram bot for searching [Carousell Singapore](https://www.carousell.sg) in plain English. A cheap LLM turns a prompt like "lightly used Aeron chair under $600, meet-up near Tampines, newest first" into Carousell's own search filters (Level 1). It then fills the search keywords and category-specific fields such as phone model, storage or shoe size (Level 2). Finally it screens the listings it finds: plain rules for seller rating, account age, price outliers and keyword blocklists, plus a vision model for subjective calls like low-effort photos or reseller-template descriptions (Level 3). It ranks results and shows them in Telegram with editable filter buttons. **Phase 1 is built** (2026-10-24): a tested Python client and command line for structured Carousell searches, listing details and a breakage canary. **Phase 2 task 1** adds a single-user Telegram bot that builds a search one question at a time with buttons (see [Running the Telegram bot](#running-the-telegram-bot)). **Phase 3a** adds [screening](#screening) for multi-item "catalogue" posts, with an optional price lookup by the worker model; prompt-to-filters has no LLM yet. **Phase 3b** adds a local [web app](#web-app) for searching with filters and reading screened results in a card grid; the Telegram bot stays for quick lookups and future alerts. An [MCP server](#mcp-server) lets agent hosts such as Claude Code search Carousell through the same client. **Phase B** adds [`carousearch ask`](#ask-the-agent): carousearch's own agent, which answers a plain-English shopping prompt by calling that server's tools with a model of your choice. **Phase C** adds the same agent to the web app as [Ask mode](#ask-mode), with live progress and the answer as cards, and lists the posts "Hide multi-item posts" hides, with their reasons. The **tiered agent** (2026-10-27) splits the agent into a planner model and a cheap worker model, and adds a Python sandbox over the run's listings, judge tools, photo metrics, category discovery, capped web search and a cost estimate per run. The **planning layer** (2026-10-27) makes the agent plan first, for any kind of item: it samples what sellers actually write, shows a plan (attributes and where each comes from, benchmark, exclusions, ranking) before spending anything else, and reports on every plan item afterwards.

- [docs/FEASIBILITY.md](docs/FEASIBILITY.md): full feasibility study: verdict, recommended stack, costs, risks, architecture
- [docs/CAROUSELL_ACCESS_NOTES.md](docs/CAROUSELL_ACCESS_NOTES.md): raw technical findings on Carousell's endpoints, filters, response shapes and anti-bot behaviour
- [docs/ROADMAP.md](docs/ROADMAP.md): Levels 1 to 3 as milestones with tasks, acceptance criteria and effort estimates (and the phase 1 status)
- [docs/MCP.md](docs/MCP.md): the MCP server for agent hosts: its tools, registering it with Claude Code, Codex CLI, the Claude desktop app and ChatGPT, example prompts and the request budget
- [docs/ANALYSIS_AND_SCREENING_PLAN.md](docs/ANALYSIS_AND_SCREENING_PLAN.md): implementation spec for market analysis (fresh pulls, upsert-by-content-hash storage, plan → scrape → compute → summarise), read-only SQL questions, and the staged screening pipeline with auditable vision verdicts; supersedes the roadmap's Level 3

## For contributors and future sessions

Read [CLAUDE.md](CLAUDE.md) first: setup, checks, code and git rules, and the hard constraints. [docs/NEXT_STEPS.md](docs/NEXT_STEPS.md) is the live plan: current state, what has been verified live, remaining phases, open questions and how to brief and review work.

## Layout

```
src/carousearch/
  filters/schema.py      SearchRequest, Listing, ListingDetail, SearchResult (the shared data model)
  filters/payload.py     SearchRequest -> exact JSON body for Carousell's search endpoint (all Carousell codes live here)
  carousell/client.py    CarousellClient: session bootstrap, CSRF, re-bootstrap, rate limiting (all network I/O)
  carousell/parse.py     pure functions: response JSON / HTML -> models, block detection
  carousell/rate_limit.py, carousell/errors.py
  carousell/categories.py, category_tree.json  Carousell's category tree, from a snapshot taken 2026-10-27
  photos.py              downsize a listing photo to a maximum width (Pillow, pure)
  storage.py             SQLite: seen_listings, listing_detail_cache, saved_searches, listings (upsert by content hash),
                         listing_changes, harvest_runs
  market/hashing.py      content hash of a listing, hash of a search request (pure)
  market/limits.py       HarvestLimits (pages, age cutoff, page size)
  market/harvest.py      walks a search's pages into `listings` and records the run; reuses a recent identical run
  market/analysis.py     AnalysisSpec and the price statistics (pure)
  market/render.py       analysis result as text / JSON (pure)
  screening/models.py    Tag, ResolvedPrice, ScreenedListing (the contract for --json and the web front end)
  screening/rules.py     title, description and price rules, and dollar-amount extraction (pure)
  screening/price_resolver.py  PriceResolver: the worker model through Pydantic AI, or Noop; prompt and checks are pure
  screening/store.py     detail and resolved-price caches behind a small interface (SQLite)
  screening/pipeline.py  screen_listings: rules, budgeted detail fetches, price resolution (I/O injected)
  config.py              data folder, the Telegram settings and the screening settings (environment or .env)
  language_models.py     planner and worker model names, provider keys, building a Pydantic AI model (vendor-neutral)
  bot/state.py           SearchSession, the wizard stages and their transitions, button actions and
                         their callback_data encoding (pure)
  bot/price_input.py     typed price ranges ("150-400", "<250", ">1000") to and from text (pure)
  bot/keyboard.py        each stage's inline keyboard, the "Step n/6" header and recap (pure)
  bot/messages.py        help, step questions, request summary, lock reminder, captions, error texts (pure)
  bot/session_store.py   the active session for each chat, with 30-minute idle expiry (in memory)
  bot/handlers.py        python-telegram-bot handlers, BotServices, owner check (Telegram and Carousell I/O)
  bot/app.py             builds the Application, runs polling, and the `whoami` helper
  web/app.py             create_app(settings): FastAPI app, services built once at startup, static files
  web/routes.py          the page (/), the results partial (/results), the detail partial (/listing/{id}),
                         and Ask mode's POST /ask, GET /ask/{run_id} and the plan's execute, revise, cancel and
                         knowledge (save a suggested note) POSTs
  web/query_params.py    URL query string <-> SearchRequest, Load-more counts, the page mode (pure)
  web/view_models.py     price row, badges, meta line, results line, hidden posts, form and detail views (pure)
  web/prompt_view_models.py  Ask mode's form (with method templates), progress, plan, answer (with the plan
                         review), failure, cancellation and waiting views (pure)
  web/prompt_runs.py     PromptRun and the in-memory registry of Ask runs; plans, then executes on Execute, as
                         background tasks; saves a suggested knowledge note when asked
  web/services.py        WebServices; the screened search and listing detail (blocking, run one at a time)
  web/templates/, web/static/  Jinja2 templates; app.css, app.js, htmx.min.js (2.0.11, vendored)
  mcp_server.py          build_server(services): the MCP tools (search, details, photo, screening tags, category
                         fields, sample descriptions, usage notes)
  mcp_instructions.md    the usage notes the MCP server sends to hosts
  agent/settings.py      planner and worker models, the per-run budgets, provider keys, the price table
  agent/prompt.py        the planning and execution instructions (method only: no item knowledge)
  agent/plan.py          SearchPlan, PlanItemStatus, the plan checks and the plan review (pure)
  agent/templates.py     generic method templates the planner may follow (pure)
  agent/knowledge.py     the owner's knowledge/ notes: parsing and keyword selection (pure), loading and saving
  agent/planning.py      plan_prompt: the planning phase (discovery tools, small budget, plan checks)
  agent/evals.py         the cross-category eval prompts and their plan checks (pure), the live runner
  agent/result.py        AgentAnswer: kept, dropped, not_shortlisted, notes, plan, plan_review,
                         suggested_knowledge, tool_trace, planning_trace, usage (`ask --json`)
  agent/budget.py        ToolBudget: counts a run's tool calls, photos and listings against the caps (pure)
  agent/trace.py         the tool trace, from a run's message history (pure)
  agent/progress.py      ToolCallEvent: what an observer is told as each tool call starts and finishes (pure)
  agent/cost.py          token usage per model and the cost estimate (pure)
  agent/local_tools.py   run_python, extract_fields, judge_listings, judge_photos, photo_metrics (function tools)
  agent/extraction.py    extract_fields' field specs, regexes, typing and worker prompt (pure)
  agent/sandbox.py, sandbox_bootstrap.py  run_python's confined subprocess (Landlock, network namespace, limits)
  agent/listings_db.py   the run's listings.db and listings.json (row building is pure)
  agent/tool_results.py  the shorter tool results the planner reads (pure)
  agent/judging.py, worker.py, judge_cache.py  judge prompts and answers (pure), worker-model calls, the cache
  agent/photo_metrics.py, run_photos.py  sharpness and brightness of a photo (pure), a run's photo files
  agent/web_search.py    the provider's hosted web search, capped per run
  agent/run_directory.py data/runs/<time>-<id>/, one folder per run
  agent/toolsets.py      ToolCallGate, the hook every tool call passes through, and the toolsets built on it
  agent/run_prompt.py    execute_plan (the execution phase, which saves answer.json) and run_prompt (plan, then execute)
  outreach/models.py     OfferRule, ShortlistedListing, OutreachDraft, OutreachMessage, OutreachPolicy, SendOutcome
  outreach/run_shortlist.py  a run's kept listings from answer.json and listings.db (row conversion is pure)
  outreach/policy.py     what to draft, what a send may send (de-duplication, daily cap), the gap (pure)
  outreach/prompt.py, drafting.py  the drafter's generic instructions; the offer price and prompt (pure), model calls
  outreach/store.py      data/outreach.db: every drafted and sent message (with empty reply columns for later)
  outreach/selectors.py  every carousell.sg selector outreach uses, in one place
  outreach/browser.py    ChatTransport: Playwright over the logged-in profile, or a dry run that sends nothing
  outreach/sender.py     one batch: policy, one session check, sends spaced by the gap, stop at the first failure
  outreach/service.py    wiring: draft for a run, preview, approve and send, the login wait
  web/outreach_routes.py, outreach_runs.py, outreach_view_models.py  the Message sellers panel, batches, /outreach
  canary.py              the health check behind `carousearch canary`
  fixture_capture.py     the dev command behind `carousearch capture-fixtures`
  text_render.py, cli.py terminal output and the `carousearch` command
tests/                   pytest; fixtures/ holds trimmed real responses captured 2026-10-24
knowledge/               your own notes for the planner (gitignored except README.md, which has the format)
evals/prompts.json       the cross-category eval prompts behind `carousearch eval`
```

## Setup

Python 3.12 or newer.

```bash
cd ~/carousearch
python3 -m venv .venv            # Debian/Ubuntu needs the python3.X-venv apt package for this
.venv/bin/pip install -e ".[dev]"
```

If `python3 -m venv` fails with "ensurepip is not available" (the case on this machine), create the venv with [uv](https://docs.astral.sh/uv/) instead:

```bash
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python pip -e ".[dev]"   # the old llm extra is now empty and harmless
```

Runtime data goes to `~/carousearch/data/` (`carousearch.db`, `canary.log`). Override with `--data-dir` or the `CAROUSEARCH_DATA_DIR` environment variable.

## Commands

Every command makes one bootstrap request (the home page, for cookies and the CSRF token) plus its own calls, at least 1.0 s apart with ±0.3 s jitter (`--min-interval`, `--jitter`).

### search

```
$ carousearch search "iphone 15" --price-max 900 --condition used --limit 10
ID          PRICE      CONDITION     LIKES  TITLE                                         SELLER              URL
1469923094  S$579      Like new      1      iPhone 15 256GB Blue                          urameshi1984        https://www.carousell.sg/p/1469923094/
1463757293  S$750      Lightly used  3      iPhone 15 Pro 128GB Natural Titanium          grandstone.022d1e   https://www.carousell.sg/p/1463757293/
1469915662  S$650      Like new      1      iPhone 15 Pro 128GB Natural Titanium          invalidcode         https://www.carousell.sg/p/1469915662/
...
Showing 10 of 1,000+ results; more pages available (use --json to get next_page).
```

Options:

| Option | Meaning |
|---|---|
| `--price-min`, `--price-max` | SGD |
| `--condition` | comma-separated `brand_new`, `like_new`, `lightly_used`, `well_used`, `heavily_used`, or the groups `new` / `used` (`used` = the four used levels) |
| `--sort` | `best_match` (default), `recent`, `price_low_to_high`, `price_high_to_low`, `nearby` (needs a location filter, not in phase 1) |
| `--deal-method` | comma-separated `meetup`, `mailing` |
| `--buyer-protection`, `--free-delivery` | only listings with Buyer Protection / free delivery |
| `--category` | Carousell collection **id**, e.g. `5707` Mobile Phones, `5755` Chairs, `1793` Laptops (ids in the access notes, section 4.8) |
| `--field KEY=VALUE` | category facet, repeatable; repeat a key for several values, e.g. `--field mobile_storage=MOBILE_STORAGE_256_GB` |
| `--limit` | page size, 1 to 48 (default 20) |
| `--json` | print `{listings, total, total_display, next_page}` as JSON |
| `--page-cursor` | the `next_page` value from a previous `--json` run, to fetch the next page |
| `--screen` | tag multi-item posts and add the YOUR PRICE and FLAGS columns ([Screening](#screening)) |
| `--hide-multi-item` | leave multi-item posts out (implies `--screen`) |
| `--max-detail-fetches` | with `--screen`: Carousell detail requests to make at most (default 10) |

Example with a category facet:

```bash
carousearch search "iphone 15 pro" --category 5707 --field mobile_storage=MOBILE_STORAGE_256_GB --deal-method meetup --sort recent
```

Listings shown are recorded in the `seen_listings` table.

## Screening

Carousell "catalogue" posts advertise many variants in one listing ("iPhone 15 / 15 Pro / 15 Pro Max all storage") and show one price, usually the cheapest variant's. `carousearch search ... --screen` tags such posts and, where the description has a price list, looks up the price of the variant you searched for.

```
$ carousearch search "iphone 15 pro 256gb" --price-max 1200 --limit 20 --screen
ID          PRICE      YOUR PRICE    FLAGS                               CONDITION     LIKES  TITLE ...
1470223613  S$610      S$610         -                                   Lightly used  6      iPhone 15 Pro Blue Titanium 256GB
1470142763  S$8.88     S$8.88        Below market                        Lightly used  1      iPhone 15 Pro 256GB Black Titanium
...
Screening: 0 of 20 multi-item; 1 description checked (detail budget 10); your variant's price read from the description by openai:gpt-5.4-nano.
```

Tags (`code`, label, severity). Each tag also carries a one-sentence `reason` with the evidence, e.g. "title names 3 variants: iPhone 15, 15 Pro, 15 Pro Max; says 'all storage'":

| Code | Label | Severity | When |
|---|---|---|---|
| `MULTI_ITEM_TITLE` | Multi-item title | warn | the title names 2+ distinct models ("15 / 15 Pro"), 2+ storage sizes ("128GB/256GB"), or says "all models", "all storage", "many sets", "from $..." |
| `PRICE_LIST_IN_DESCRIPTION` | Price list | warn | the description offers 3+ distinct prices, or has 2+ lines like "15 Pro 256GB - $850" |
| `LISTED_PRICE_IS_FLOOR` | Floor price | warn | the listed price is the lowest of 2+ prices in the description, or the description says "from $..." |
| `PRICE_BELOW_MARKET` | Below market | info | the price is under 60% of the median of the other results on the page (needs 5+ others) |
| `PRICE_NOT_FOR_YOUR_ITEM` | Price not for your item | warn | the price lookup read the description and found no price for your variant |

A listing with any warn tag is multi-item (`is_multi_item`). Reference prices ("bought at $1,299", "retail $1,599") are not counted as offered prices; storage sizes, percentages, durations, years and Singapore phone numbers are never read as prices.

How it runs, per page of results:

1. The title rule and the below-market rule run on every listing; they need no requests.
2. Listings tagged in step 1 get their listing detail, through the 12-hour detail cache, for at most `--max-detail-fetches` (default 10) Carousell requests per search, so a page of 20 to 48 results never costs more than 10 extra requests. Then the description rules run.
3. Listings that are multi-item after step 2 go to the price resolver, through `price_resolution_cache` (keyed by listing id and search keywords, so a listing is never looked up twice for the same search).

**Price lookup.** Screening asks the worker model (`AGENT_WORKER_MODEL`, default `openai:gpt-5.4-nano`; see [Ask the agent](#ask-the-agent)) through Pydantic AI, with structured output, for the price the description states for exactly your search; it answers `not_stated` rather than guess, which adds the "Price not for your item" tag. Descriptions are cut to 1,500 characters. It needs that model's provider key in `.env` (`OPENAI_API_KEY` for the default); without it, multi-item posts keep their listed price and the summary line says "price resolution off". Any provider Pydantic AI supports works, for example `AGENT_WORKER_MODEL=anthropic:claude-haiku-5-5` with `ANTHROPIC_API_KEY`. A lookup whose answer never validates, or whose API call fails, is not cached, so the next search tries again.

Cost estimate per looked-up listing (from character counts; not measured): about 750 to 1,300 input tokens and 30 to 80 output tokens. At gpt-5.4-nano's list prices (US$0.20 per million input, US$1.25 per million output, checked 2026-10-27) that is about US$0.0002 to US$0.0004 per listing. Only multi-item listings with a fetched description are looked up, and cached answers are free.

`--json` prints `{screened_listings, total, total_display, next_page}`; each item has `listing`, `detail` (when the description was checked), `tags`, `resolved_price` (`amount`, `source`: `listed` / `description` / `not_stated`, `note`) and `is_multi_item`. In Telegram, result cards of multi-item posts get the one-line summary from the title rule only (no extra requests), e.g. "Multi-item · listed S$579 · may not be your variant's price".

### listing

```
$ carousearch listing 1463757293
iPhone 15 Pro 128GB Natural Titanium
S$750  |  Lightly used  |  3 likes
https://www.carousell.sg/p/1463757293/

Seller:         grandstone.022d1e (id 37448921)
Rating:         5.00 / 5 from 3 reviews
Joined:         2022-08-30
Response rate:  H
Verified:       email, mobile, Google
Created:        2026-09-24 10:15 SGT
Last modified:  2026-10-24 00:59 SGT
Deal:           meet-up yes, mailing no
Meet-up points: HarbourFront Centre, 1 Maritime Sq., 099253
Photos:         2
  https://media.karousell.com/media/photos/products/2026/9/24/iphone_15_pro_128gb_natural_ti_1790216103_80c8500c.jpg
  ...

Description:
  This is my backup phone for my US number. ...
```

`--json` prints the full `ListingDetail`, including the `raw` listing object from Carousell. The detail is also stored in `listing_detail_cache`.

### harvest

Pulls every page of one search (up to `--max-pages`, default 10, 48 listings a page) into the `listings` table, one row per listing, and records the run in `harvest_runs` so an analysis can read exactly what that pull returned. Takes the same filter options as `search`.

```
$ carousearch harvest "iphone 15 pro" --category 5707 --condition used --sort recent --max-pages 5
run 12: 212 listings, 5 pages, stopped: max_pages (37 new, 4 changed)
```

A listing already in the table is overwritten only when its title, price or condition changed (a content hash decides); that change is also appended to `listing_changes` with the old and new price. Likes, the bump time and `last_seen_at` are refreshed on every pull. An identical search run in the last 30 minutes is reused instead of scraped again (`reused run 11: …`) when that run covered at least as much (it exhausted the pages, or fetched at least as many pages, or used the same or a longer age cutoff); pass `--no-reuse` to force a fresh pull. `--max-age-days N` stops once listings get older than N days and needs `--sort recent`.

### analyze

Runs the searches in an analysis spec, then prints price statistics for the union of their listings: count, min, p25, median, p75 and max, overall and per group (the mean is in the `--json` output), with IQR outliers listed separately and excluded by default. The spec is JSON ([docs/examples/analysis_spec.example.json](docs/examples/analysis_spec.example.json)):

```json
{
  "question": "What do used iPhone 15 Pro 256GB listings go for, by condition?",
  "searches": [{"query": "iphone 15 pro 256gb", "category": "5707", "sort": "recent"}],
  "limits": {"max_pages": 5, "max_age_days": 30},
  "title_filter": {"include": ["15 ?pro"], "exclude": ["pro ?max", "case"]},
  "group_by": "condition"
}
```

```
$ carousearch analyze --spec docs/examples/analysis_spec.example.json
run 12: 212 listings, 5 pages, stopped: max_pages (37 new, 4 changed)
What do used iPhone 15 Pro 256GB listings go for, by condition?
Fetched 212 listings (1 run); analysed 184 (14 without a price, 9 filtered out, 5 outliers excluded)
All           n=184  median S$1,050  p25–p75 S$980–S$1,150  min S$700  max S$1,400
...
```

`group_by` is `none`, `condition`, `week_posted` (ISO weeks in Singapore time) or `seller`; `title_filter` holds case-insensitive regexes (every `include` must match, no `exclude` may). `--json` prints the full result; `--spec -` reads the spec from stdin. The statistics are asking prices, not sold prices.

### canary

Runs a fixed search for "iphone" (20 per page), checks that at least 10 listings parse (`--min-listings`), then fetches and parses the first listing's detail. Prints one line, appends it to `~/carousearch/data/canary.log` (`--log-file` to change), and exits 0 on success or 1 on failure.

```
$ carousearch canary; echo "exit code: $?"
2026-10-24T01:38:12+08:00 OK 20 listings parsed, detail 1451884114 parsed (2.2s)
exit code: 0
```

A failure looks like `... FAIL search failed: CarousellBlockedError: Cloudflare challenge (HTTP 403, cf-mitigated: challenge) (1.3s)`.

### capture-fixtures (developer command)

Run from the project root. Re-fetches the fixture set (bootstrap page, two searches, one listing detail, one deliberately provoked Cloudflare challenge; 5 requests) and overwrites the files in `tests/fixtures/`.

```
$ carousearch capture-fixtures
wrote tests/fixtures/bootstrap_page.html (1,205 bytes)
wrote tests/fixtures/search_price_condition.json (24,123 bytes)
...
wrote tests/fixtures/cloudflare_challenge.json (5,683 bytes)
```

## Tests

```bash
.venv/bin/pytest           # unit tests, offline, driven by tests/fixtures/ (live test deselected)
.venv/bin/pytest -m live   # one integration test against the real carousell.sg (about 4 requests)
.venv/bin/ruff check src tests
```

## Daily canary with cron (WSL2)

Add this with `crontab -e` (08:00 every day):

```
0 8 * * * /home/moses/carousearch/.venv/bin/carousearch canary >> /home/moses/carousearch/data/cron.log 2>&1
```

WSL2 does not start cron by itself. Either:

- enable systemd in WSL (`/etc/wsl.conf`: `[boot]` then `systemd=true`, run `wsl --shutdown` from Windows) and then `sudo systemctl enable --now cron`; or
- start it after each WSL boot with `sudo service cron start`; or
- skip cron and create a Windows Task Scheduler task that runs
  `wsl.exe -d <your distro> -u moses -- /home/moses/carousearch/.venv/bin/carousearch canary`
  daily. This also works when no WSL session is open.

The canary's own log is `~/carousearch/data/canary.log`; `cron.log` only catches crashes.

## Web app

A one-page web app for searching Carousell with filters and spotting catalogue posts whose shown price may not be the price of what you searched for. It runs on this machine only, without a login.

```bash
.venv/bin/carousearch web              # then open http://localhost:8765
.venv/bin/carousearch web --port 9000  # another port; --host 127.0.0.1 is the default
```

On WSL2, open http://localhost:8765 in the Windows browser (WSL forwards localhost). Ctrl+C stops the server. It can run next to the Telegram bot: both use `~/carousearch/data/carousearch.db`, and each has its own Carousell session and rate limiter. The bot stays the tool for quick lookups from your phone and for the alerts planned later.

![Desktop](docs/examples/web-desktop.png)

The controls (all optional; after the first search, changing any of them searches again):

| Control | What it does |
|---|---|
| Search box | Keywords. Enter or **Search** runs the search; after that, a pause in typing (0.6 s) re-runs it. |
| S$ Min, S$ Max | Price range in SGD. `1,200` and `S$1,200` work; anything else is ignored, and a reversed range is swapped. |
| Condition chips | Brand new, Like new, Lightly used, Well used, Heavily used. Select any number; none means any condition. |
| Delivery chips | Meet-up, Mailing. None means either. |
| Sort | Best match, Recent, Price ↑ (low to high), Price ↓ (high to low). |
| Category | Any, or one of the common categories (Mobile Phones, Laptops, Tablets, Furniture, Video Gaming, Audio). A URL with any other collection id, e.g. `category=5755`, also works. |
| Hide multi-item posts | Leaves out listings that screening flags as multi-item; the results line says how many were hidden. A collapsed **Hidden (n)** section under the grid lists each one: its title (opens Carousell in a new tab), the listed price, and one line per reason, the same text as the Multi-item badge's tooltip. With the switch on and nothing hidden it reads "Hidden (0)"; with the switch off there is no section. |

The address bar follows the search (`/?q=iphone+15+pro&price_max=1200&condition=like_new`), so a search can be bookmarked and Back and Forward step through earlier searches.

Each card shows the photo, the title, the price row, then condition · likes · seller. The whole card opens the listing on Carousell in a new tab. **Details** opens a panel under the card's row with the full description, all photos, the seller's rating, reviews, joined date and response rate, the meet-up points, and every screening tag with its reason; click **Details** again or **Close** to fold it.

The price row:

| You see | Meaning |
|---|---|
| S$720 | The listed price. Nothing suggests it is wrong. |
| **S$850** ~~S$579~~ *from description* | A catalogue post: the description gives S$850 for the item you searched for, while the card shows S$579 (usually the cheapest variant). Needs the worker model's API key ([Screening](#screening)). |
| S$579 *price for your item not stated* | The description was read and has no price for the item you searched for, so the listed price probably belongs to another variant. |

Badges on the photo: **Multi-item** (amber) means a screening rule thinks the price does not apply to your item; hover over it for the reasons. **Low price** (grey) means the price is under 60% of the median of the other results on the page (often a placeholder such as S$1 or S$8.88).

The line above the grid reads like `20 of 10,000+ results · 3 multi-item hidden · screened 6 descriptions`. "Screened" counts listings whose description was checked; "price lookup off" appears when there are multi-item posts but no API key for the worker model, so their listed price is kept. **Load more** at the bottom appends the next page; with "Hide multi-item posts" on, that page's hidden posts join the Hidden list and its count goes up.

Keyboard: `/` puts the cursor in the search box from anywhere on the page; Esc leaves a field.

Requests: a search costs one Carousell search plus, for flagged listings, up to 10 detail requests (cached for 12 hours, shared with the CLI and the bot), all through one rate limiter and one call at a time. The same search again within 2 minutes (toggling the switch, going Back) is answered from memory, and a search the browser has already replaced is skipped. If Carousell blocks the client the page says "Carousell is rate-limiting us, try again in a minute"; if its response changed shape it says to run `carousearch canary` (see [When Carousell changes](#when-carousell-changes)). Details of any error go to the terminal running `carousearch web`, never to the page.

### Ask mode

**Ask**, next to **Filters** above the search box, switches the page to a plain-English prompt for the same agent `carousearch ask` runs ([Ask the agent](#ask-the-agent)): the same tools, the same budgets, the same answer. The address becomes `/?mode=ask`, and switching back to Filters brings back the filter search you had. Opening or reloading `/?mode=ask` never runs anything; only **Run** does.

Settings are the ones `carousearch ask` reads, from `.env` or the environment: `AGENT_PLANNER_MODEL`, `AGENT_WORKER_MODEL`, the `AGENT_MAX_*` budgets, and each model's provider key (`OPENAI_API_KEY` for the defaults). There are no `--model` or `--max-photos` overrides here. Under the prompt box the page shows the two models ("Models: openai:gpt-5.4-mini plans, openai:gpt-5.4-nano judges") and the budgets. Without the provider's key it names the missing variable, says to add it to `.env` and restart `carousearch web`, and **Run** is disabled. `carousearch web` reads `.env` once at start-up, so restart it after any change; a malformed `AGENT_*` value stops it from starting (exit code 2), as it does for `ask`.

A prompt can be up to 2,000 characters. Under the prompt box, **Start from a method** offers the five method templates (under benchmark, best under budget, neglected listings, compare variants, watch for); pressing one puts an example request with `<placeholders>` in the box, with the first placeholder selected, and runs nothing.

**Run** first plans, in the background of the web process, and the page asks for its progress every second ("Planning · 6 s · 2 tool calls", against the planning budget of 6 tool calls). Planning only discovers: the category and its filters, and a sample of what sellers write. Then the **plan** replaces the progress, and nothing more is spent until you decide:

- the goal, the item class, the value drivers, the method template and the ranking;
- **Attributes**, each with its source (structured filter, card field, text extraction, derived, external fact), how it is read and why the goal needs it;
- **Benchmark** (population, grouping, statistic, normalisation), **Exclusions** with their reasons, **Assumptions**, the owner's notes used, and the estimate against the run budget;
- what planning cost so far, the planning calls, and a warning if the planner sampled no listings.

Three buttons: **Execute** runs the plan (the progress shows again: "Running · 12 s · 2 tool calls", the budget used so far, one line per tool call with its status and duration); **Edit** opens a box for a change in plain words ("compare within each size", "keep posts listing several items"), which goes back to the planner once and shows the revised plan; **Cancel** drops the plan. A reload shows a plan still waiting for a decision.

When the run ends, the answer replaces the progress: a summary line ("2 kept · 5 dropped · 30 not shortlisted · 6 tool calls · 14 s"), the plan's item class, the agent's **Notes** ending with the cost estimate line (planning and execution together), the **Plan review** (every attribute, the benchmark, each exclusion and the ranking marked done, partial or not done, with what was done; an item the agent did not report on is marked not done), a card for each kept listing, then the collapsed sections **Dropped (n)**, **Planning calls (n)** and **Tool calls (n)**. When the agent learned something reusable about this kind of item, a **Suggested note** box shows it with **Save to knowledge/**, which writes it as `knowledge/<item class>.md` (never overwriting a file); nothing is saved unless you press it.

A kept card is built from what Carousell's search returned, not from the model's text: the thumbnail, the listed price and condition · likes · seller, plus the agent's reason and a **Details** button that opens the same panel as in Filters mode. A kept listing that no search in the run returned gets an **Unverified** badge and a warning that its title, price and link are the model's. **Dropped** lists the shortlisted listings that failed one of your criteria: the title (linked to Carousell), the price when a search returned the listing, and the reason. Everything else the agent saw is only counted, as "not shortlisted". When Carousell blocks a tool call, that call shows as `failed`; the agent is told and can still answer with what it has. When the run itself fails (the model's API refuses the key or errors, or a model setting is unusable), the page says so calmly and lists the calls made before it stopped; the details go to the terminal running `carousearch web`.

One run at a time. A second **Run** while one is planning or running is refused with a message, and the first run's progress keeps showing. Planning and execution each hold the web app's Carousell client from start to end, so meanwhile a filter search shows "An Ask run is in progress; your search will run when it finishes." (a **Details** panel says the same of its details) and asks again every 2 seconds, then shows the real results once the run has ended. It never overlaps an agent tool call. Only the page itself can start a run: `POST /ask` and the plan's buttons are refused without the `HX-Request` header the page's HTMX sends, so another website cannot post a form to your localhost and spend your API budget. Runs are kept in memory, the last 10, until `carousearch web` restarts, and stopping the web app stops a run in flight. After a reload, Ask mode shows a run that is still in progress, or a plan waiting for a decision, but not the answer of a finished one.

Keyboard: `/` puts the cursor in the current mode's box (the search box or the prompt box); Ctrl+Enter (Cmd+Enter on a Mac) in the prompt box runs it.

Requests and cost are as for `carousearch ask`: every Carousell request goes through the rate limiter (1.0 s ± 0.3 s apart), so a run with 20 tool calls takes at least 20 seconds plus the models' time. Each run's files go to `data/runs/<time>-<run id>/`.

Status (2026-10-27): Ask mode, with its plan step, is tested with scripted models only and has not been opened in a real browser; the agent behind it has run live through `carousearch ask` and `carousearch eval --live` (see below). The screenshot above predates the Filters | Ask switch.

## MCP server

`carousearch mcp` lets an agent host (Claude Code, Codex CLI, the Claude desktop app, or ChatGPT through a tunnel) search Carousell through this client: `search_listings` (optionally only some fields per listing), `listing_details` (12-hour cache), `listing_photo` (downsized, Carousell's media host only), the optional `screening_tags`, `category_fields` (the categories, their subcategories and the filters each offers), `sample_descriptions` (one search page as description excerpts and seller-entered fields, to learn what sellers in a category state) and `usage_notes`. Search results carry each card's `description_snippet` and details the seller-entered `attributes`. It is only a connector: the host filters, ranks, looks at photos and plans; the server does none of that. Over stdio by default; `--transport http --port 8766` serves streamable HTTP on 127.0.0.1 for tunnelling.

```bash
claude mcp add --scope user carousearch -- /home/moses/carousearch/.venv/bin/carousearch mcp
```

[docs/MCP.md](docs/MCP.md) has the setup for every host, example prompts and the request budget (about 40 Carousell requests per task).

## Ask the agent

`carousearch ask` answers a request in plain English about any kind of item, including multi-step ones such as "work out the market price, take 80% as a benchmark, then find listings 50% below it". It runs an agent built with [Pydantic AI](https://pydantic.dev/docs/ai/), in two phases: it plans, then it carries the plan out. The web app's [Ask mode](#ask-mode) runs the same agent from the browser, with a stop to approve the plan.

```bash
.venv/bin/carousearch ask "a standing desk under S\$300, two motors, frame at least 140 cm"
.venv/bin/carousearch ask "used Aeron chair under S\$600, meet-up" --json        # the answer as AgentAnswer JSON
.venv/bin/carousearch ask "road bike under S\$800" --plan-only                   # plan, print it, save plan.json, stop
.venv/bin/carousearch ask "road bike under S\$800" --plan-file data/runs/<run>/plan.json   # execute a saved plan
.venv/bin/carousearch ask "..." --model openai:gpt-5.4 --worker-model openai:gpt-5.4-nano --max-photos 5
```

**No item knowledge.** Nothing in the code, the prompts or the tool descriptions knows any kind of item (a test checks every model-facing text for item and domain words). The agent works out the meta-information itself: what drives value for this kind of item, what the listings actually contain, which attributes the goal needs and where each comes from, how to compare fairly, what to leave out and why, and how to rank. What it knows comes only from the model's general knowledge, web search, the listings it samples, and your own [knowledge notes](#knowledge-notes).

**The plan.** The planning phase runs the planner model with three discovery tools on a small budget (`AGENT_PLAN_MAX_TOOL_CALLS`, default 6 tool calls, no photos, two search pages, two web searches): `category_fields`, `search_listings` and `sample_descriptions` (one search page shown as description excerpts and seller-entered fields). A first plan made without reading a single listing is sent back once with a reminder to sample. The plan (`SearchPlan`, `agent/plan.py`) has: the goal, the item class (the planner's reading of what is searched), the value drivers, the attributes (each with its source: `structured_filter`, `card_field`, `text_extraction`, `derived` or `external_fact`, and how it is read), an optional benchmark (population, grouping, statistic, normalisation), exclusions each with a reason tied to the goal, the ranking, the method template followed if any, a budget estimate and every assumption. Plans whose parts do not fit together (a grouping that names no attribute, an unknown template, text extraction with no worker calls budgeted) go back to the planner to be fixed. `--plan-only` stops here, prints the plan and saves it as `plan.json` in the run folder; edit it and run it with `--plan-file`.

**The execution and its review.** The execution phase gets the request, the plan as JSON and the list of plan items it must review, and runs with the full toolset and the run budget. Its answer includes a `plan_review`: every attribute, the benchmark, each exclusion and the ranking marked `done`, `partial` or `not_done` with what was done and its numbers; an item it leaves out is filled in as `not_done`. It may also return `suggested_knowledge`, a short note of reusable facts, which `ask` prints and the web app offers to save.

**Method templates.** `agent/templates.py` holds five generic shapes of a plan, with slots and a sentence the planner fills: `under_benchmark` (priced well below comparable listings), `best_under_budget`, `neglected_listings` (up a long time, little interest), `compare_variants` and `watch_for` (an alert condition; planning only, alerts are not built yet). The planner picks one or none; the web app offers them as starting points.

**Two models.** The planner (`AGENT_PLANNER_MODEL`, default `openai:gpt-5.4-mini`) runs the agent loop: it plans, calls tools and writes the answer. The worker (`AGENT_WORKER_MODEL`, default `openai:gpt-5.4-nano`) does the many small jobs: one-line judgements per listing, photo scores, `extract_fields` and screening's price lookup. Any Pydantic AI model string works for either (`anthropic:claude-haiku-5-5`, `openai:gpt-5.4`, ...); each needs its provider's key in `.env` or the environment (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`), and `ask` exits before any request when one is missing. The older `AGENT_MODEL` still works as a deprecated alias that sets both tiers (a tier set on its own wins), and `ask` warns while it is used. Both phases ask the provider to enforce their output's schema (strict structured output), and each planner request has a 180-second deadline, after which the run fails with a clear error.

**Tools.** The [MCP server](#mcp-server)'s tools run inside the same process: `search_listings` (with `fields`, so the planner reads only, say, id, title and price), `listing_details` (the planner sees the first 300 characters of the description), `listing_photo`, `screening_tags` and `category_fields`; `sample_descriptions` is the planning phase's. Five local tools work on the run's own data:

| Tool | What it does | Cost |
|---|---|---|
| `run_python(code)` | Runs a Python snippet in a sandbox whose working directory holds `listings.db` (SQLite, table `listings`) and `listings.json`: every listing the run's searches and details returned, with card columns (price, condition, likes, seller, posted_at, bumped_at, category, raw_json) and detail columns (description, seller_rating, review_count, image_urls, meetup_locations, ...). The agent does all arithmetic, medians, benchmarks and regexes here. Standard library only (pandas only if installed). | no tokens, no Carousell request |
| `extract_fields(listing_ids, field_specs, fetch_details)` | Reads named fields from titles and descriptions into typed columns of `listings.db`. Each spec has a name, a type (`str`, `int`, `float`, `date`, `bool`, `enum`), a unit, a hint, an optional regex (tried first) and the allowed values of an enum. What the regex does not find goes to the worker model in batches of 12 with a strict schema; anything the text does not state stays null, never guessed. Values land in columns `x_<name>` and in the JSON column `extracted`, so `run_python` can `SELECT id, price, x_size_cm FROM listings`. Without `fetch_details` it reads the title and the card's ~300-character snippet. Cached per listing, spec and worker model. | worker tokens for what the regex misses; with `fetch_details`, one request per uncached detail |
| `judge_listings(listing_ids, criterion)` | The worker model answers yes, no or unsure with a one-line reason per listing, from title, price and description, 12 listings per model call. Missing details are fetched first. | worker tokens; one Carousell request per uncached detail |
| `judge_photos(listing_ids, criterion)` | The worker model scores each main photo 1 to 5 against the criterion (the photo is downsized to 512 px), plus the metrics below. Uses the full-size photo once details are known, else the thumbnail. | worker tokens; one request per photo not yet downloaded in the run |
| `photo_metrics(listing_ids)` | Width, height, sharpness (Laplacian variance at 512 px; under about 100 looks blurry) and brightness (0 to 255), with no model. | one request per photo not yet downloaded |

Judgements are cached in `data/carousearch.db` (`judgement_cache`) by listing, criterion (case and spacing ignored) and worker model, so asking again is free. Where the planner's provider has a hosted web search (OpenAI's Responses API, Anthropic), the agent may use it, up to `AGENT_MAX_WEB_SEARCHES` per run, to learn an unfamiliar term; without one, the notes say so. Prompt caching uses Pydantic AI's `Caching` capability (OpenAI caches long prompts on its own; Anthropic gets cache points).

**The sandbox.** `run_python` starts `python -I -S` (no site-packages, no `PYTHON*` variables) in the run folder with an empty environment, so no API key is visible. On Linux 5.13 and later it confines itself with Landlock before running the snippet: it can read only the interpreter and system libraries and write only inside the run folder, and TCP connections are refused (verified on this machine: reading `../../../.env` from the run folder raises `PermissionError`). Where unprivileged user namespaces exist it also runs under `unshare -rn`, with no network at all. Limits: 20 s wall clock (the process group is killed), 512 MB of memory, 64 MB per written file, and only the first few thousand characters of output come back. Without Landlock (an older kernel, macOS) only the working directory, the empty environment, the limits and the network namespace remain; the tests skip the confinement checks there and say why. Each snippet and its output stay in `python/NNN.*` in the run folder.

**Run folders.** Each run writes `data/runs/<UTC time>-<id>/`: `listings.db`, `listings.json`, `photos/`, `python/` and, with `--plan-only`, `plan.json`. Planning and execution share the folder, so the pages the planner read are already in `listings.db`. Nothing deletes old run folders.

The output, top to bottom:

- **Tool calls**: the execution's calls: a count per tool, then one line per call with its duration, the tool and its arguments, and `[refused]` (over a budget, never sent to Carousell), `[failed]` (the tool reported an error, such as Carousell blocking) or `[not run]`. Hosted web searches appear as `web_search`.
- **Plan**: headed by the planning calls ("Plan (3 planning tool calls: category_fields 1, sample_descriptions 2)"), then the plan's sections as above; a warning line if the planner sampled no listings.
- **Kept**: a table of price, title, seller, URL and the agent's reason, which quotes the numbers it computed (benchmark, percent below) and what it checked.
- **Dropped**: only shortlisted listings that failed one of your criteria, each with that criterion and its number.
- **Not shortlisted**: how many more listings the agent saw; they are not listed.
- **Plan review**: one line per plan item: `done`, `partial` or `not_done`, the item and what was done.
- **Notes**: the agent's assumptions (how it read a vague criterion, what the benchmark was computed from) and what you should check, plus a line for each budget that stopped or shortened the run.
- **Suggested knowledge note**, when the agent has one.
- **Cost (estimate)**: both phases together; tokens per model (input, of which cached, and output), web searches, and US$ at list prices, for example `Cost (estimate, list prices of 2026-10-27): about US$0.0255. planner openai:gpt-5.4-mini: 6 requests, 92,483 input tokens (76,032 cached), 1,531 output; worker openai:gpt-5.4-nano: 1 request, 1,237 input tokens (0 cached), 260 output.` The prices are in `agent/settings.py` (`MODEL_PRICES`, checked 2026-10-27); a model not in the table is reported as "no price".

`--json` prints the same as one `AgentAnswer` object: `kept`, `dropped`, `not_shortlisted`, `notes`, `plan`, `plan_review`, `suggested_knowledge`, `tool_trace`, `planning_trace` and `usage` (per model, both phases: `role`, `model`, `requests`, `input_tokens`, `cached_input_tokens`, `output_tokens`, `web_searches`, `estimated_cost_usd`). With `--plan-only`, `--json` prints the `SearchPlan` alone.

| Setting (`.env` or environment) | Default | Meaning |
|---|---|---|
| `AGENT_PLANNER_MODEL` | `openai:gpt-5.4-mini` | The model that runs the agent loop. `--model` overrides it. |
| `AGENT_WORKER_MODEL` | `openai:gpt-5.4-nano` | The model behind the judge tools and screening's price lookup. `--worker-model` overrides it for `ask`. |
| `AGENT_MODEL` | none | Deprecated: sets both of the above when they are not set. |
| `AGENT_MAX_TOOL_CALLS` | 40 | Tool calls per run, plus each Carousell request a local tool makes (details, photos). |
| `AGENT_MAX_PHOTOS` | 15 | Photos looked at or measured. `--max-photos` overrides it. |
| `AGENT_MAX_LISTINGS` | 200 | Listings asked for across all search pages. A page that would pass it is cut short. |
| `AGENT_MAX_WEB_SEARCHES` | 3 | Hosted web searches in the execution phase; 0 turns web search off. |
| `AGENT_PLAN_MAX_TOOL_CALLS` | 6 | Tool calls in the planning phase (which also has no photos, at most 96 listings and 2 web searches). |

The budgets are enforced, not advised. A call over a budget is refused before it reaches Carousell, and the agent is told to finish with what it has; a judge tool that runs out part-way judges the rest on their titles and says so. The notes then say which budget was reached. A model that keeps calling tools anyway is stopped after its tool-call budget plus 5 more model requests. Carousell requests still go through the usual rate limiter (1.0 s ± 0.3 s apart). Details come from the 12-hour cache shared with the other commands.

### Knowledge notes

`knowledge/` at the repo root holds optional notes of your own, one topic per short markdown file, with front matter naming the words that select it (`applies_to: standing desk, desk frame`). The planner is shown, clearly labelled as the owner's notes, every note with a keyword in your request (and, for the execution, in the plan's item class). carousearch ships none; the folder is gitignored except [knowledge/README.md](knowledge/README.md), which has the format. Notes come from you, or from the web app's **Save to knowledge/** button under an answer's suggested note.

### Evaluating the planner

`evals/prompts.json` holds seven requests about unrelated kinds of items (phones by storage tier, an office chair under a budget, neglected camera listings, console variants, the owner's two motorcycle prompts, and one vague request), each with the generic plan properties a good plan has: a benchmark grouped by an attribute, a text extraction attribute, an exclusion with a reason, stated assumptions, and so on. The tests check them offline against scripted plans.

```bash
.venv/bin/carousearch eval                       # list the prompts and their checks; no request
.venv/bin/carousearch eval --live                # run each once: planner, Carousell and worker requests
.venv/bin/carousearch eval --live --only vague_request
```

`--live` runs each prompt once through `run_prompt` and prints, per prompt, the item class, the attributes by source, the benchmark, whether the planner sampled listings, the checks passed, the plan review statuses, the tool counts of both phases and the cost; then a table and the total cost. It writes everything to `data/evals/<UTC time>.json`, and stops early if Carousell blocks a request.

Status (2026-10-27): run live with the default models: `carousearch eval --live` once over the seven prompts (about US$0.40; every plan sampled the listings and 25 of 26 plan checks passed after one fix and a re-run), and the owner's R15 prompt through `--plan-only` and `--plan-file`. Planning is consistent; execution quality still depends on the planner model (gpt-5.4-mini once spent its whole listing budget on search pages, and its plan review said so). docs/NEXT_STEPS.md, section 4.7, has the table.

## Outreach: offer messages to sellers

After an Ask run, outreach drafts one short offer per kept listing (a percentage below the asking price, specific to that listing), lets you review the batch once, and sends it from your own Carousell account through a browser profile you logged into by hand: one message at a time, 30 to 60 seconds apart, at most 10 new conversations a day, and never twice to the same listing or seller. The full guide, the safety policy and how to check the page selectors after your first login are in [docs/OUTREACH.md](docs/OUTREACH.md).

```bash
uv pip install --python .venv/bin/python -e ".[dev,llm,outreach]" && .venv/bin/playwright install chromium
.venv/bin/carousearch account login                       # log in by hand in the window; once
.venv/bin/carousearch outreach draft <run id> --below 25  # run id = the folder name under data/runs/
.venv/bin/carousearch outreach send <run id> --all --dry-run
.venv/bin/carousearch outreach send <run id> --ids 12 14
.venv/bin/carousearch outreach list
```

In the web app, a finished Ask answer has a **Message sellers** panel (draft, review, **Send N messages**), and **/outreach** lists every message.

## Running the Telegram bot

The bot answers one Telegram user (you). Send it keywords and it asks for the filters one at a time with buttons, shows a summary, and on **Search** replies with listing cards (photo, price, condition, likes, title, seller, link), 5 at a time.

### 1. Create the bot in BotFather

In Telegram, open a chat with [@BotFather](https://t.me/BotFather) and send `/newbot`. Pick a display name, then a username that ends in `bot` (for example `moses_carousearch_bot`). BotFather replies with a token like `1234567890:AAH...`. Keep it secret: anyone with the token can control the bot.

Optional: send BotFather `/setcommands`, pick the bot, and paste:

```
search - start a search (optionally with keywords)
status - where the current search is
end - finish the current search
saved - list saved searches
save - save the current search under a name
help - how to use the bot
```

### 2. Create `.env`

```bash
cd ~/carousearch
cp .env.example .env
```

Put the token into `.env`. Then find your numeric Telegram user id, either by messaging [@userinfobot](https://t.me/userinfobot), or by sending `/start` to your new bot and running:

```
$ .venv/bin/carousearch whoami
User id     Name (people who messaged the bot in the last 24 hours)
123456789  Moses (@moses)
```

`.env` then looks like this (`.env` is in `.gitignore`; environment variables of the same name override it):

```
TELEGRAM_BOT_TOKEN=1234567890:AAH...
TELEGRAM_OWNER_ID=123456789
```

### 3. Run it

```bash
.venv/bin/carousearch bot
```

It logs to the console: start-up, every Carousell search and detail call, refused users (with their user id, which helps if you mistyped yours) and any handler error. Ctrl+C stops it. Without a token or owner id it exits with code 2 and says which setting is missing.

In Telegram:

- send keywords (or `/search <keywords>`) to start a search. `/search` on its own first asks "What are you looking for?".
- the bot then asks one thing per step, editing the same message as you tap: **Step 2/6 Price** (`< 100`, `100–300`, `300–800`, `800+`, Any, or **Type a range** such as `150-400`, `<250`, `>1000`), **3/6 Condition** and **4/6 Delivery** (tick any number, then Next), **5/6 Sort**, **6/6 Category** (common ones, Any, or **Other id…** to type a collection id such as `5707`). The header recaps what you have picked so far. Every step has **Back** and **Cancel**.
- the **Summary** lists every filter, with **Search** and an **Edit** button per step; an edited step returns straight to the summary.
- **Search** sends 5 cards, then a message with **Next 5**, **Refine** (back to the summary with the same filters) and **End**. Listings you have been shown before (in the bot or by `carousearch search`) get a `[seen]` tag. **Details** on a card shows the seller, rating, description and photo links (cached for 12 hours).
- one search at a time: while one is in progress, `/search` and new keywords get a reminder to `/end` it (or use Back / Refine). `/status` shows where it is; a search idle for 30 minutes expires and the next one starts fresh. Only the buttons on the newest message of the search work.
- `/save <name>` saves the current filters; `/saved` lists them with buttons to load one (at the summary step, when no search is in progress).

Only one copy of the bot can run at a time (Telegram answers a second poller with "Conflict").

### 4. Keep it running on WSL2

The simplest way is `nohup` from a WSL terminal:

```bash
nohup /home/moses/carousearch/.venv/bin/carousearch bot >> /home/moses/carousearch/data/bot.log 2>&1 &
tail -f /home/moses/carousearch/data/bot.log      # watch it
pkill -f "carousearch bot"                        # stop it
```

This stops when WSL shuts down (a Windows restart, `wsl --shutdown`, or WSL stopping the distro when no terminal has been open for a while). For something that survives that, replace it later with a systemd user service (with systemd enabled in `/etc/wsl.conf`, as in the cron section above) or a Windows Task Scheduler task that runs `wsl.exe -d <your distro> -u moses -- /home/moses/carousearch/.venv/bin/carousearch bot` at logon.

## When Carousell changes

Carousell's `/ds/` endpoints are undocumented and can change without notice. When `carousearch canary` fails, its one-line reason tells you which case you are in:

- **`CarousellBlockedError`** (Cloudflare challenge, captcha, HTTP 429, or 401/403 after a fresh session): Carousell is screening the client. Check that https://www.carousell.sg loads in a normal browser from the same network, lower the request rate, and wait. Updating `BROWSER_USER_AGENT` in `carousell/client.py` to a current Chrome version can help. The HTML and Playwright fallbacks are not built yet (see the TODO in `CarousellClient.search` and the roadmap).
- **`CarousellSchemaError: missing key '...'`**: the response shape changed. From the project root run `carousearch capture-fixtures`, then `pytest`. The failing parser tests and the error message point at the key that moved. Compare the new fixture with the shapes in `docs/CAROUSELL_ACCESS_NOTES.md` section 4, update `carousell/parse.py`, and record the change in the notes.
- **`CarousellHTTPError`** (404, 5xx, network error): a 404 usually means an endpoint path moved. Open carousell.sg in a browser with the developer tools Network tab, run a search, and look for the new `/ds/...` request; update the paths in `carousell/client.py`.
