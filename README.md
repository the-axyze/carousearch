# carousearch

Carousearch is a planned personal Telegram bot for searching [Carousell Singapore](https://www.carousell.sg) in plain English. A cheap LLM turns a prompt like "lightly used Aeron chair under $600, meet-up near Tampines, newest first" into Carousell's own search filters (Level 1). It then fills the search keywords and category-specific fields such as phone model, storage or shoe size (Level 2). Finally it screens the listings it finds: plain rules for seller rating, account age, price outliers and keyword blocklists, plus a vision model for subjective calls like low-effort photos or reseller-template descriptions (Level 3). It ranks results and shows them in Telegram with editable filter buttons. **Phase 1 is built** (2026-10-24): a tested Python client and command line for structured Carousell searches, listing details and a breakage canary. **Phase 2 task 1** adds a single-user Telegram bot with manual filter buttons (see [Running the Telegram bot](#running-the-telegram-bot)). There is no LLM yet.

- [docs/FEASIBILITY.md](docs/FEASIBILITY.md): full feasibility study: verdict, recommended stack, costs, risks, architecture
- [docs/CAROUSELL_ACCESS_NOTES.md](docs/CAROUSELL_ACCESS_NOTES.md): raw technical findings on Carousell's endpoints, filters, response shapes and anti-bot behaviour
- [docs/ROADMAP.md](docs/ROADMAP.md): Levels 1 to 3 as milestones with tasks, acceptance criteria and effort estimates (and the phase 1 status)
- [docs/ANALYSIS_AND_SCREENING_PLAN.md](docs/ANALYSIS_AND_SCREENING_PLAN.md): implementation spec for market analysis (fresh pulls, upsert-by-content-hash storage, plan → scrape → compute → summarise), read-only SQL questions, and the staged screening pipeline with auditable vision verdicts; supersedes the roadmap's Level 3

## Layout

```
src/carousearch/
  filters/schema.py      SearchRequest, Listing, ListingDetail, SearchResult (the shared data model)
  filters/payload.py     SearchRequest -> exact JSON body for Carousell's search endpoint (all Carousell codes live here)
  carousell/client.py    CarousellClient: session bootstrap, CSRF, re-bootstrap, rate limiting (all network I/O)
  carousell/parse.py     pure functions: response JSON / HTML -> models, block detection
  carousell/rate_limit.py, carousell/errors.py
  storage.py             SQLite: seen_listings, listing_detail_cache, saved_searches, listings (upsert by content hash),
                         listing_changes, harvest_runs
  market/hashing.py      content hash of a listing, hash of a search request (pure)
  market/limits.py       HarvestLimits (pages, age cutoff, page size)
  market/harvest.py      walks a search's pages into `listings` and records the run; reuses a recent identical run
  market/analysis.py     AnalysisSpec and the price statistics (pure)
  market/render.py       analysis result as text / JSON (pure)
  config.py              data folder and the Telegram settings (environment or .env)
  bot/state.py           ChatDraft, button actions and their callback_data encoding (pure)
  bot/keyboard.py        filter panel and other inline keyboards, filter summary text (pure)
  bot/messages.py        listing captions, help, prompt and error texts (pure)
  bot/draft_store.py     the active draft for each chat (in memory)
  bot/handlers.py        python-telegram-bot handlers, BotServices, owner check (Telegram and Carousell I/O)
  bot/app.py             builds the Application, runs polling, and the `whoami` helper
  canary.py              the health check behind `carousearch canary`
  fixture_capture.py     the dev command behind `carousearch capture-fixtures`
  text_render.py, cli.py terminal output and the `carousearch` command
tests/                   pytest; fixtures/ holds trimmed real responses captured 2026-10-24
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
uv pip install --python .venv/bin/python pip -e ".[dev]"
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

Example with a category facet:

```bash
carousearch search "iphone 15 pro" --category 5707 --field mobile_storage=MOBILE_STORAGE_256_GB --deal-method meetup --sort recent
```

Listings shown are recorded in the `seen_listings` table.

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

A listing already in the table is overwritten only when its title, price or condition changed (a content hash decides); that change is also appended to `listing_changes` with the old and new price. Likes and `last_seen_at` are refreshed on every pull. An identical search run in the last 30 minutes is reused instead of scraped again (`reused run 11: …`); pass `--no-reuse` to force a fresh pull. `--max-age-days N` stops once listings get older than N days and needs `--sort recent`.

### analyze

Runs the searches in an analysis spec, then prints price statistics for the union of their listings: count, min, p25, median, p75, max and mean, overall and per group, with IQR outliers listed separately and excluded by default. The spec is JSON ([docs/examples/analysis_spec.example.json](docs/examples/analysis_spec.example.json)):

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

`group_by` is `none`, `condition`, `week_posted` or `seller`; `title_filter` holds case-insensitive regexes (every `include` must match, no `exclude` may). `--json` prints the full result; `--spec -` reads the spec from stdin. The statistics are asking prices, not sold prices.

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

## Running the Telegram bot

The bot answers one Telegram user (you). Send it keywords, set filters with the buttons, tap **Search**, and it replies with listing cards (photo, price, condition, likes, title, seller, link), 5 at a time.

### 1. Create the bot in BotFather

In Telegram, open a chat with [@BotFather](https://t.me/BotFather) and send `/newbot`. Pick a display name, then a username that ends in `bot` (for example `moses_carousearch_bot`). BotFather replies with a token like `1234567890:AAH...`. Keep it secret: anyone with the token can control the bot.

Optional: send BotFather `/setcommands`, pick the bot, and paste:

```
search - search Carousell for keywords
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

- send keywords (or `/search <keywords>`): a filter panel appears. Tap price, condition, delivery, sort and category; the panel updates in place. "Custom…" and "Other id…" ask you to type a price range (`100-500`, `<300`, `800+`) or a Carousell collection id (`5707`).
- **Search** sends 5 cards and a message with **Next 5** and **Back to filters**. Listings already shown in this chat get a `[seen]` tag. **Details** on a card shows the seller, rating, description and photo links (cached for 12 hours).
- `/save <name>` saves the current filters; `/saved` lists them with buttons to load one.

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
