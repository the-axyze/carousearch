# Market analysis and staged screening: implementation plan

Written 2026-10-25. This document is the specification for the next body of work on carousearch. It is
written so that an implementing agent can build each phase without further design decisions. Where
this document and [ROADMAP.md](ROADMAP.md) differ, this document wins; the roadmap's Level 3 section is
superseded by phases D and E below.

Read first: [README.md](../README.md) (layout, commands), [ROADMAP.md](ROADMAP.md) (phase 1 and 2 status),
and the module docstrings of `filters/schema.py`, `storage.py`, `carousell/client.py`, `bot/state.py`
and `bot/handlers.py`. The code in those modules is the ground truth for the interfaces this plan builds on.

## 0. What is being built, in one paragraph

Three things, layered. **Market analysis:** a question such as "what do used iPhone 15 Pro 256GB go for"
becomes a plan (which searches to run, how many pages, how to group), the plan is executed against
Carousell by scraping fresh pages into SQLite, the numbers are computed by plain code, and an LLM writes
the summary. **Open-ended querying:** once listings are on disk, the LLM may write read-only SQL over them.
**Staged screening:** a search's result set is narrowed by a per-chat stack of filter stages (rules, an LLM
over the description, a vision model over the photos). Every model verdict is cached, each stage can be
undone or loosened, and every vision verdict carries the observations and evidence behind it so the user
can audit it.

## 1. Decisions already made (do not reopen)

These were settled in the design discussion. Implement them as written.

1. **Fresh pull on every analysis question.** The `listings` table is a by-product of pulls, never the
   source of truth for "now". An analysis reads only the listings that its own pull returned.
2. **Reuse window.** If the identical `SearchRequest` (same hash, section 3.2) was harvested successfully
   within the last 30 minutes (configurable), reuse that run instead of scraping again. This is what makes
   follow-up questions instant.
3. **One row per listing, overwritten in place.** No append-only snapshot table. A row is keyed by
   Carousell's listing id (no synthetic identity hash). On every pull, `like_count` and `last_seen_at` are
   always written. A **content hash over `(title, price, condition)`** decides whether the listing
   "changed": same hash, nothing else is written; different hash, the whole row is overwritten and one
   row is appended to `listing_changes` with the old and new price, condition and title. Likes are
   deliberately **not** in the hash: they are demand signal (used for ranking), not a market move.
4. **Likes matter.** They are stored on every pull and used by rule stages and ranking. No separate
   likes history table; likes-per-day is derived from `like_count / days since posted_or_bumped_at`.
5. **Statistics are computed by code, never by the LLM.** The LLM produces a plan (structured output,
   validated by pydantic) and a summary (text). Plain Python (`statistics` module); no pandas or numpy.
6. **Vision: extract once, judge many.** Per listing, one vision call produces **observations** (a fixed
   schema plus free-text notes, section 8.2), cached by photo hash. A **judgement** for a specific
   criterion is a text-only call over the cached observations, cached by criterion. A second criterion on
   the same shortlist never re-uploads photos.
7. **Scores, not booleans.** Text and vision stages return a 0 to 1 match score plus reasons; the stage
   holds a threshold. "Loosen" and "Tighten" move the threshold without any model call.
8. **Verdicts are auditable.** A vision judgement always carries: the observations it was based on, an
   evidence list (each item pointing at an observation and a photo index, for or against), a confidence,
   and what could not be determined. The one-line card text is derived from that, not the other way round.
9. **Cost ceilings.** A per-stage cap on model calls (default 60 listings) and a daily spend guard. Before
   a vision stage runs in Telegram, the user sees the count, time and cost estimate and confirms.
10. **Stage order is free.** Rule and semantic stages before vision will almost always be cheaper, and
    the planner is told so, but the pipeline is order-agnostic.
11. **Feedback becomes the labelled set.** 👍/👎 on a verdict is stored and is the calibration data the
    roadmap's Level 3 task 7 asked for; no separate tagging session.

## 2. Conventions that every phase must follow

These restate the rules in ROADMAP.md and make them concrete for the new code.

- **Pure vs I/O.** Pure modules (`market/analysis.py`, `market/hashing.py`, `screening/pipeline.py`,
  `screening/rules.py`, `bot/state.py` additions, all `render_*` functions) import nothing that does
  network or filesystem I/O and take every input as an argument. I/O modules (`market/harvest.py`,
  `screening/runner.py`, `llm/anthropic_adapter.py`, `screening/images.py`, handlers) are thin and call
  pure functions. A function that needs the time takes a `clock: Callable[[], datetime]` or a `now:
  datetime`; nothing calls `datetime.now()` except `handlers.utc_now` and the CLI entry points.
- **Models.** LLM-facing and persisted structures are pydantic `BaseModel` with
  `model_config = ConfigDict(extra="forbid")`. In-process results are `@dataclass(frozen=True)`.
  Enums are `StrEnum` with semantic values (`like_new`, not Carousell codes).
- **Timestamps** are timezone-aware UTC in Python and ISO 8601 text in SQLite, written and read only
  through `storage._to_text` / `storage._from_text` (promote them to public names `to_text` /
  `from_text` in the same module when phase A touches storage; keep the old names as aliases).
- **Database access** follows `storage.py`: every function takes an open `sqlite3.Connection`; writes
  are wrapped in `with connection:`; tables are created by `open_database` through `CREATE TABLE IF NOT
  EXISTS`. Add the new tables to `storage.SCHEMA`. There is no migration framework; new tables and
  `ALTER TABLE ... ADD COLUMN` guarded by a `PRAGMA table_info` check are the only schema changes allowed.
  Existing tables (`seen_listings`, `listing_detail_cache`, `saved_searches`) are not changed and keep
  working for the bot's `[seen]` tag, Details cache and `/saved`.
- **Carousell I/O** goes only through `CarousellClient` and therefore the one `RateLimiter`. Photo
  downloads (media.karousell.com) use a separate `httpx.Client` with its own `RateLimiter(0.3, 0.1)`.
- **Dependencies.** The core `dependencies` list in `pyproject.toml` does not grow. New capabilities are
  optional extras: `llm = ["anthropic>=0.40"]`, `vision = ["pillow>=10"]`. Modules that need an extra
  import it lazily inside the function or adapter class and raise a clear `RuntimeError("install
  carousearch[llm]")` if missing, so `pytest` and the existing CLI commands run without them.
- **Tests** are offline. Carousell is `tests/fake_carousell.FakeCarousell`; the LLM is
  `tests/fake_llm.FakeLLM` (phase B); photos are bytes from `tests/fixtures/photos/`. Tests marked `live`
  or `llm` are deselected by default (`addopts` in `pyproject.toml` gains `-m 'not live and not llm'`).
  Every pure function gets direct unit tests; every handler gets a fake-bot test. The existing 180 tests
  must keep passing unchanged.
- **Style.** `ruff check src tests` clean, line length 120, module docstring on every file saying what is
  pure and what does I/O, docstrings on public functions that explain a non-obvious choice (not what the
  signature already says). Names are full words (`listing_id`, not `lid`). No `print` outside `cli.py`.
- **Logging** uses `logging.getLogger(__name__)`. Never log API keys, bot tokens, or full photo bytes.
  Log every Carousell call, every model call (purpose, model, tokens, cost) and every cache hit at INFO.
- **Telegram** `callback_data` stays under 64 bytes (`state.encode_action` enforces it); message text
  under 4096 characters (`messages.truncate`). The synchronous Carousell client and model adapters run
  through `asyncio.to_thread`, as `handlers.search_carousell` does.
- **Secrets** come from the environment or `.env` through `config.py`, never from code or the database.
- **Do not**: add pandas/numpy, store `Listing.raw` in the new tables, change `parse.py`, `payload.py`
  or `schema.py` semantics, change any existing public function signature, make network calls in unit
  tests, or let a handler block the event loop.

## 3. Phase A: storage, harvest and analysis (no LLM)

Branch `feature/market-analysis-a`. Everything in this phase works from a hand-written JSON spec, so the
numbers are proven on real data before any model is involved.

### 3.1 New tables (`storage.SCHEMA`)

```sql
CREATE TABLE IF NOT EXISTS listings (
    listing_id            TEXT PRIMARY KEY,
    title                 TEXT NOT NULL,
    price                 REAL,                 -- NULL when the display price is not a number
    currency              TEXT,
    price_display         TEXT NOT NULL,
    condition             TEXT,                 -- Condition value, NULL for legacy "Used"
    condition_label       TEXT,
    like_count            INTEGER NOT NULL,
    seller_username       TEXT,
    seller_id             TEXT,
    thumbnail_url         TEXT,
    listing_url           TEXT NOT NULL,
    posted_or_bumped_at   TEXT,
    posted_or_bumped_kind TEXT,
    content_hash          TEXT NOT NULL,        -- market.hashing.listing_content_hash
    first_seen_at         TEXT NOT NULL,
    last_seen_at          TEXT NOT NULL,
    last_changed_at       TEXT NOT NULL         -- when content_hash last changed; = first_seen_at at insert
);
CREATE TABLE IF NOT EXISTS listing_changes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id    TEXT NOT NULL,
    changed_at    TEXT NOT NULL,
    old_price     REAL,
    new_price     REAL,
    old_condition TEXT,
    new_condition TEXT,
    old_title     TEXT NOT NULL,
    new_title     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS listing_changes_listing ON listing_changes (listing_id, changed_at);
CREATE TABLE IF NOT EXISTS harvest_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    request_hash  TEXT NOT NULL,                -- market.hashing.search_request_hash
    request_json  TEXT NOT NULL,                -- SearchRequest.model_dump_json(), page_cursor None
    started_at    TEXT NOT NULL,
    finished_at   TEXT,                         -- NULL while running
    pages_fetched INTEGER NOT NULL DEFAULT 0,
    listing_count INTEGER NOT NULL DEFAULT 0,
    stop_reason   TEXT                          -- market.harvest.StopReason value, NULL while running
);
CREATE INDEX IF NOT EXISTS harvest_runs_lookup ON harvest_runs (request_hash, started_at);
CREATE TABLE IF NOT EXISTS harvest_run_listings (
    run_id     INTEGER NOT NULL,
    listing_id TEXT NOT NULL,
    position   INTEGER NOT NULL,                -- 0-based, Carousell's order across pages
    PRIMARY KEY (run_id, listing_id)
);
```

`seen_listings` is left exactly as it is. `listings` is the new, richer record; the bot keeps using
`seen_listings` for its `[seen]` tag until a later clean-up.

### 3.2 `market/hashing.py` (pure)

```python
def listing_content_hash(listing: Listing) -> str
```
`sha256` of `json.dumps([listing.title, listing.price, listing.condition.value if listing.condition else None],
separators=(",", ":"), ensure_ascii=False)`, hex, first 16 characters. Floats go through `repr`-stable JSON
encoding (`json.dumps` does this); `750.0` and `750` must hash the same, so normalise `price` with
`float(price)` first.

```python
def search_request_hash(request: SearchRequest) -> str
```
`sha256` of `request.model_dump_json(exclude={"page_cursor", "page_size"})` after
`model_copy(update={"page_cursor": None})`, hex, first 16 characters. Pydantic serialises lists in the
order given, so callers must not rely on `conditions=[a, b]` and `[b, a]` hashing the same; the LLM-facing
code in phase B sorts enum lists before building the request (`state._toggled` already keeps canonical
order for the bot).

### 3.3 `storage.py` additions

```python
@dataclass(frozen=True)
class ListingRow:            # one row of `listings`; the input type for all analysis functions
    listing_id: str
    title: str
    price: float | None
    currency: str | None
    price_display: str
    condition: Condition | None
    condition_label: str | None
    like_count: int
    seller_username: str | None
    seller_id: str | None
    thumbnail_url: str | None
    listing_url: str
    posted_or_bumped_at: datetime | None
    posted_or_bumped_kind: PostedOrBumpedKind | None
    content_hash: str
    first_seen_at: datetime
    last_seen_at: datetime
    last_changed_at: datetime

@dataclass(frozen=True)
class UpsertSummary:
    new_ids: list[str]
    changed_ids: list[str]
    unchanged_ids: list[str]

def upsert_listings(connection, listings: Iterable[Listing], seen_at: datetime) -> UpsertSummary
```
Behaviour, inside one `with connection:` block, per listing in order:
1. `SELECT content_hash, price, condition, title FROM listings WHERE listing_id = ?`.
2. No row: `INSERT` all columns, `first_seen_at = last_seen_at = last_changed_at = seen_at`; id goes to `new_ids`.
3. Row with the same hash: `UPDATE listings SET like_count = ?, last_seen_at = ? WHERE listing_id = ?`; `unchanged_ids`.
4. Row with a different hash: `UPDATE` every column except `first_seen_at`, with `last_changed_at = seen_at`;
   `INSERT INTO listing_changes (...)` with the old and new price/condition/title; `changed_ids`.
Duplicate ids within one call (same listing on two pages) are processed once: keep the first occurrence.

```python
def load_listing_rows(connection, listing_ids: Sequence[str]) -> list[ListingRow]     # in the order given; missing ids skipped
def load_listing_changes(connection, listing_id: str) -> list[ListingChange]           # oldest first
def create_harvest_run(connection, request: SearchRequest, request_hash: str, started_at: datetime) -> int
def add_run_listings(connection, run_id: int, listing_ids: Sequence[str], first_position: int) -> None
def finish_harvest_run(connection, run_id: int, finished_at: datetime, pages_fetched: int, listing_count: int, stop_reason: str) -> None
def find_reusable_run(connection, request_hash: str, now: datetime, max_age: timedelta) -> HarvestRun | None
def load_run_listing_ids(connection, run_id: int) -> list[str]                           # ordered by position
```
`HarvestRun` is a frozen dataclass mirroring the table. `find_reusable_run` returns the newest run with
that hash whose `finished_at` is not NULL, `stop_reason != "error"`, and `now - finished_at <= max_age`.
`add_run_listings` uses `INSERT OR IGNORE` so a listing that appears on two pages keeps its first position.

### 3.4 `market/harvest.py` (I/O: Carousell + SQLite)

```python
class StopReason(StrEnum):
    NO_MORE_PAGES = "no_more_pages"
    MAX_PAGES = "max_pages"
    MAX_AGE = "max_age"
    ERROR = "error"

class HarvestLimits(BaseModel):          # extra="forbid"
    max_pages: int = Field(default=10, ge=1, le=50)
    max_age_days: int | None = Field(default=None, ge=1)   # only meaningful with sort=RECENT (validated in harvest())
    page_size: int = Field(default=MAX_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)

@dataclass(frozen=True)
class HarvestOutcome:
    run_id: int
    listing_ids: list[str]        # Carousell's order, de-duplicated
    pages_fetched: int
    stop_reason: StopReason
    reused: bool                  # True when an earlier run was reused and no request was made
    summary: UpsertSummary | None # None when reused

def harvest(
    client: CarousellClient,
    connection: sqlite3.Connection,
    request: SearchRequest,
    limits: HarvestLimits,
    clock: Callable[[], datetime],
    reuse_within: timedelta | None = timedelta(minutes=30),
    progress: Callable[[int, int], None] | None = None,     # (pages_fetched, listings_so_far)
) -> HarvestOutcome
```
Algorithm:
1. `request = request.model_copy(update={"page_cursor": None, "page_size": limits.page_size})`. If
   `limits.max_age_days` is set and `request.sort != SortOrder.RECENT`, raise `ValueError` (the cutoff is
   only sound when pages arrive newest first).
2. `request_hash = search_request_hash(request)`. If `reuse_within` is not None and
   `find_reusable_run(...)` returns a run, return `HarvestOutcome(run.id, load_run_listing_ids(...), run.pages_fetched, StopReason(run.stop_reason), reused=True, summary=None)` and log "reused run N".
3. `run_id = create_harvest_run(...)`. Loop over pages with `cursor = None`:
   - `result = client.search(request.model_copy(update={"page_cursor": cursor}))`.
   - `upsert_listings`, `add_run_listings` (skipping ids already in this run), update counters, call `progress`.
   - Stop reason, checked in this order by the pure function below: `NO_MORE_PAGES` if
     `result.next_page is None` or the page added no new ids; `MAX_AGE` if the cutoff applies and the
     last listing on the page with a timestamp is older than `now - max_age_days`; `MAX_PAGES` if
     `pages_fetched == limits.max_pages`.
   - On `CarousellError`: `finish_harvest_run(..., stop_reason="error")` and re-raise. The listings
     already written stay (they are real data); the run is simply not reusable.
4. `finish_harvest_run(...)`, return the outcome with the merged `UpsertSummary`.

Pure helper, unit-tested on its own:
```python
def decide_stop(page_index: int, page_listings: list[Listing], new_ids_on_page: int, next_page: str | None,
                limits: HarvestLimits, now: datetime) -> StopReason | None
```

### 3.5 `market/analysis.py` (pure)

```python
class GroupBy(StrEnum):
    NONE = "none"
    CONDITION = "condition"
    WEEK_POSTED = "week_posted"      # ISO week of posted_or_bumped_at, key "2026-W43"; None -> "unknown"
    SELLER = "seller"                # seller_username or seller_id; None -> "unknown"

class TitleFilter(BaseModel):        # extra="forbid"
    include: list[str] = []          # regexes; every one must match the title (re.IGNORECASE)
    exclude: list[str] = []          # regexes; none may match
    # validator: each pattern must compile; reject patterns longer than 200 characters

class AnalysisSpec(BaseModel):       # extra="forbid"
    question: str                                        # the user's own wording
    searches: list[SearchRequest] = Field(min_length=1, max_length=3)
    limits: HarvestLimits = HarvestLimits()
    max_age_days: int | None = Field(default=None, ge=1) # client-side cutoff on posted_or_bumped_at, independent of limits.max_age_days
    title_filter: TitleFilter = TitleFilter()
    group_by: GroupBy = GroupBy.NONE
    exclude_outliers: bool = True
    # validator: every search has page_cursor None; searches are de-duplicated by search_request_hash
```
Result types (frozen dataclasses):
```python
PriceStats(count: int, minimum: float, p25: float, median: float, p75: float, maximum: float, mean: float)
GroupResult(key: str, stats: PriceStats, listing_ids: list[str])                  # listing_ids sorted by price ascending
OutlierListing(listing_id: str, title: str, price: float, side: Literal["low", "high"], bound: float)
AnalysisResult(
    spec: AnalysisSpec,
    run_ids: list[int],
    fetched_count: int,               # unique listings across the runs
    analysed_count: int,              # after title/age filters, with a numeric price, outliers excluded if configured
    unpriced_count: int,
    dropped_by_filters_count: int,
    overall: PriceStats | None,       # None when analysed_count == 0
    groups: list[GroupResult],        # empty when group_by == NONE; sorted by key
    outliers: list[OutlierListing],   # sorted by price
    cheapest: list[ListingRow],       # up to 5 analysed listings, price ascending
    newest: list[ListingRow],         # up to 5 analysed listings, posted_or_bumped_at descending (None last)
    computed_at: datetime,
)

def analyse(spec: AnalysisSpec, rows: Sequence[ListingRow], run_ids: Sequence[int], now: datetime) -> AnalysisResult
def price_stats(prices: Sequence[float]) -> PriceStats        # len >= 1; uses statistics.median/mean and statistics.quantiles(n=4, method="inclusive") when len >= 2, else all equal
def iqr_bounds(prices: Sequence[float]) -> tuple[float, float]  # (p25 - 1.5*IQR, p75 + 1.5*IQR); with fewer than 4 prices returns (-inf, +inf)
def apply_title_filter(title_filter: TitleFilter, rows) -> list[ListingRow]
def group_key(group_by: GroupBy, row: ListingRow) -> str
```
Outliers are computed on the overall price list (not per group), then excluded from both the overall
stats and the groups when `exclude_outliers` is true. `apply_title_filter` compiles each pattern once.

### 3.6 `market/render.py` (pure)

`render_analysis_text(result: AnalysisResult) -> str`: plain text, suitable for both the terminal and
Telegram, under 4096 characters (truncate the cheapest/newest sections first). Format:

```
Used iPhone 15 Pro 256GB prices          <- spec.question
Fetched 212 listings (3 runs); analysed 184 (14 without a price, 9 filtered out, 5 outliers excluded)

All        n=184  median S$1,050  p25–p75 S$980–S$1,150  min S$700  max S$1,400
like_new   n=61   median S$1,120  ...
lightly_used ...

Outliers excluded: 1455… "iPhone 15 Pro, faulty" S$300 (low), …
Cheapest: S$700  iPhone 15 Pro 256GB black  https://www.carousell.sg/p/…
```
Prices use `filters.payload.format_price`. Also `render_analysis_json(result) -> str` via a pydantic
`TypeAdapter` or `dataclasses.asdict` with datetimes as ISO text.

### 3.7 CLI (phase A)

Two subcommands in `cli.py`, following the existing pattern (argument parsing in `build_argument_parser`,
a `run_*_command(...) -> int` per command, data dir from `--data-dir`):

- `carousearch harvest "<query>" [search options] [--max-pages N] [--max-age-days N] [--no-reuse]`:
  reuses `search_request_from_arguments` for the filters, runs `harvest`, prints
  `run 12: 212 listings, 5 pages, stopped: max_pages (37 new, 4 changed)` or `reused run 11 (…)`.
- `carousearch analyze --spec FILE|- [--json] [--no-reuse]`: reads an `AnalysisSpec` from a JSON file
  (or stdin for `-`), harvests each search, loads the union of the run listing ids, calls `analyse`,
  prints `render_analysis_text` (or JSON). Exit 2 on a spec validation error, 1 on a Carousell error.

Add `docs/examples/analysis_spec.example.json` with a realistic spec (two searches, `group_by: condition`).

### 3.8 Tests (phase A)

- `tests/test_hashing.py`: content hash ignores likes and thumbnail changes, changes on price/title/
  condition, is stable across `750` vs `750.0`; request hash ignores `page_cursor` and `page_size`.
- `tests/test_storage_listings.py`: insert, unchanged (only likes and last_seen_at move), changed
  (row overwritten, `listing_changes` row written, `first_seen_at` kept), duplicates within one call,
  `find_reusable_run` respects age, NULL `finished_at` and `error`.
- `tests/test_harvest.py`: `FakeCarousell` gains `page_responses: dict[str | None, httpx.Response]`
  keyed by the `session`/cursor field the client sends (read it from `request.content` JSON) so a
  two-page scrape can be scripted; test each stop reason, reuse, de-duplication across pages, the error
  path (run finished with `error`, listings kept), and that `max_age_days` without `RECENT` raises.
  Build page 2 from the existing fixture by rewriting listing ids and timestamps in the test
  (a real second page should be captured later; add it to `fixture_capture.py` as
  `search_recent_page_1.json` / `search_recent_page_2.json` and prefer it when present).
- `tests/test_analysis.py`: `price_stats` against hand-computed values; `iqr_bounds` small-n behaviour;
  title include/exclude; every `GroupBy`; outlier exclusion; empty input; a listing with `price None`.
- `tests/test_cli.py` additions: `analyze --spec` end to end with `FakeCarousell`, including exit codes.

### 3.9 Acceptance (phase A)

- `carousearch analyze --spec docs/examples/analysis_spec.example.json` against the live site returns a
  table with a plausible median for the example query, and a second run within 30 minutes makes no
  Carousell request (log shows "reused run").
- Running `harvest` twice with a price edit simulated in the fake produces one `listing_changes` row.
- All new pure functions have tests; total Carousell requests for a 5-page analysis are 1 bootstrap + 5.

## 4. Phase B: the LLM layer

Branch `feature/llm-b`. Depends on phase A.

### 4.1 `llm/base.py`

```python
class LLMError(Exception): ...                   # transport, auth, rate limit
class LLMOutputError(LLMError):                  # the model's output did not validate
    def __init__(self, message: str, raw_output: str): ...

@dataclass(frozen=True)
class LLMUsage:
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float

class StructuredLLM(Protocol):
    def complete[T: BaseModel](self, *, system: str, user: str, output_model: type[T],
                               max_tokens: int = 1024, purpose: str) -> tuple[T, LLMUsage]: ...
```
`purpose` is a short label (`"plan_analysis"`, `"prompt_to_filters"`, `"summarize"`, `"judge"`) used for
logging and the usage ledger.

```python
def complete_with_repair[T: BaseModel](llm: StructuredLLM, *, system: str, user: str, output_model: type[T],
                                       purpose: str, max_tokens: int = 1024) -> tuple[T, list[LLMUsage]]
```
Calls `llm.complete`; on `LLMOutputError` retries **once** with the user message extended by
`"\n\nYour previous answer was rejected: <error>. Answer again, following the schema exactly."`.
A second failure propagates.

### 4.2 `llm/anthropic_adapter.py`

`AnthropicLLM(api_key: str, model: str, prices: ModelPrices)` implementing `StructuredLLM`. Structured
output is obtained with **forced tool use**: define one tool named `answer` whose `input_schema` is
`output_model.model_json_schema()`, call `messages.create` with `tool_choice={"type": "tool", "name":
"answer"}`, and validate the tool input with `output_model.model_validate`. Any validation error or
missing tool call raises `LLMOutputError` with the raw content. Network/API errors raise `LLMError`.
`ModelPrices(input_per_million_usd, output_per_million_usd)` lives in config (section 4.6). The
`anthropic` import is inside the class so the module imports without the extra installed.

### 4.3 `llm/usage.py` and the budget guard

Table (add to `storage.SCHEMA`):
```sql
CREATE TABLE IF NOT EXISTS llm_usage (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    at            TEXT NOT NULL,
    purpose       TEXT NOT NULL,
    model         TEXT NOT NULL,
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd      REAL NOT NULL
);
```
`record_usage(connection, usage: LLMUsage, purpose: str, at: datetime)` and
`spend_since(connection, since: datetime) -> float`. `BudgetExceededError(limit, spent)` is raised by
`check_daily_budget(connection, now, limit_usd)` (spend since local midnight in `Asia/Singapore`; the
timezone is a constant in `config.py`). Every caller of a model checks the budget **before** the call and
records usage **after**.

### 4.4 Prompt to filters (`llm/prompt_to_filters.py`)

LLM-facing intent model (semantic, never Carousell codes):
```python
class FilterIntent(BaseModel):                     # extra="forbid"
    keywords: str                                  # what goes in the search bar; required, non-empty
    exclude_keywords: list[str] = []               # client-side title filter in phase D; stored on the draft now
    category_name: str | None = None               # free text, e.g. "Mobile Phones"; resolved by code
    price_min: float | None = None
    price_max: float | None = None
    conditions: list[Condition] = []
    deal_methods: list[DealMethod] = []
    buyer_protection_only: bool = False
    free_delivery_only: bool = False
    sort: SortOrder = SortOrder.BEST_MATCH
    max_age_days: int | None = None
    unclear: str | None = None                     # a one-line question if the prompt cannot be mapped at all
```
`carousell/categories.py`: `COMMON_CATEGORIES: dict[str, str]` (name to collection id) built from the
list already in `bot/keyboard.py` plus CAROUSELL_ACCESS_NOTES section 4.8, and
`resolve_category_name(name: str | None) -> str | None` (case-insensitive exact match, then a
`difflib.get_close_matches` cutoff 0.8; otherwise None). Unknown names fall back to keywords only.

```python
def intent_to_search_request(intent: FilterIntent) -> SearchRequest        # pure; sorts enum lists canonically
def prompt_to_search_request(llm, prompt: str, now: datetime) -> tuple[SearchRequest, FilterIntent, list[LLMUsage]]
```
System prompt (`llm/prompts.py`, `PROMPT_TO_FILTERS_SYSTEM`, with `PROMPT_VERSION = "1"`): explains each
field, that prices are SGD, that `recent` is implied by "newest/latest", that `max_age_days` is implied by
"posted this week", that `exclude_keywords` holds things like "case", "box only", and to set `unclear`
only when no keywords can be extracted. Include three worked examples.

### 4.5 Plan and summarise an analysis (`llm/plan_analysis.py`, `llm/summarize.py`)

```python
class AnalysisIntent(BaseModel):                    # extra="forbid"
    searches: list[FilterIntent] = Field(min_length=1, max_length=3)
    max_pages: int = Field(default=10, ge=1, le=30)
    max_age_days: int | None = None
    title_include: list[str] = []                   # plain words, not regexes; code escapes them with re.escape
    title_exclude: list[str] = []
    group_by: GroupBy = GroupBy.NONE
    rationale: str                                  # one sentence shown to the user before the scrape

def intent_to_spec(question: str, intent: AnalysisIntent) -> AnalysisSpec    # pure; sets sort=RECENT when max_age_days is set
def plan_analysis(llm, question: str, now: datetime) -> tuple[AnalysisSpec, str, list[LLMUsage]]   # (spec, rationale, usage)

class AnalysisSummary(BaseModel):                   # extra="forbid"
    text: str                                       # 3 to 6 sentences, plain language, numbers quoted from the table
    caveats: list[str] = []                         # e.g. "asking prices, not sold prices"

def summarize_analysis(llm, result: AnalysisResult) -> tuple[AnalysisSummary, list[LLMUsage]]
```
The summariser receives `render_analysis_text(result)` and the question; its system prompt forbids
inventing numbers not present in the table and requires the "asking prices" caveat whenever prices are
discussed.

### 4.6 Config

`config.py` gains:
```python
ANTHROPIC_API_KEY_VARIABLE = "ANTHROPIC_API_KEY"
LLM_MODEL_VARIABLE = "CAROUSEARCH_LLM_MODEL"           # default "claude-haiku-5-5"
VISION_MODEL_VARIABLE = "CAROUSEARCH_VISION_MODEL"     # default "claude-haiku-5-5"
DAILY_BUDGET_VARIABLE = "CAROUSEARCH_DAILY_BUDGET_USD" # default "1.00"
REUSE_MINUTES_VARIABLE = "CAROUSEARCH_REUSE_MINUTES"   # default "30"
LOCAL_TIMEZONE = ZoneInfo("Asia/Singapore")

@dataclass(frozen=True)
class LLMSettings: api_key, model, vision_model, daily_budget_usd, prices: dict[str, ModelPrices]
def load_llm_settings(environment=None, env_file=DEFAULT_ENV_FILE) -> LLMSettings
```
`.env.example` documents the new variables. Model prices are a small dict in `config.py` with a comment
naming the date they were checked; unknown models get `ModelPrices(0, 0)` and a WARNING log line.

### 4.7 Surfaces (phase B)

- CLI: `carousearch analyze "<question>"` (positional question, mutually exclusive with `--spec`)
  plans, prints the rationale and the spec summary, harvests, prints the table, then the summary and
  caveats. `--plan-only` prints the spec JSON and exits (useful for building eval cases).
- CLI: `carousearch ask "<prompt>"` prints the `SearchRequest` the prompt maps to, then runs it like
  `search` (so the mapping can be checked without Telegram).
- Bot: typed text that is not a reply to a prompt goes through `prompt_to_search_request` **when an LLM
  is configured**, else behaves as today (keywords). The result lands in
  `show_filter_panel_for_request`, exactly as the roadmap said. `/analyze <question>`: sends
  "Planning…", then the rationale with **Run** / **Cancel** buttons (`ActionKind.RUN_ANALYSIS = "ra"`,
  `CANCEL_ANALYSIS = "rc"`; the pending spec lives on the `ChatDraft` as `pending_analysis:
  AnalysisSpec | None`), edits a progress message every page ("Fetching page 3 of up to 10…"), then
  sends the table and the summary. `BotServices` gains `llm: StructuredLLM | None` and `llm_settings`.

### 4.8 Eval sets

`tests/eval/prompts_level1.jsonl`: 50 cases `{"prompt": ..., "expected": {<SearchRequest fields>}}`.
`tests/eval/analysis_questions.jsonl`: 20 cases with expected `group_by`, `max_pages` range and the
keywords that must appear in `searches[0].query`. `scripts/run_eval.py --set level1` runs them with the
real adapter (marked `llm`, never in CI) and prints field-level accuracy. Phase B is accepted at 45/50
and 16/20.

### 4.9 Tests (phase B)

`tests/fake_llm.py`: `FakeLLM(answers: list[BaseModel | Exception])` returning answers in order, recording
`(system, user, output_model, purpose)` per call, raising when the list is exhausted. Tests cover:
repair-retry (first answer `LLMOutputError`, second valid), `intent_to_search_request` for every field,
category resolution (exact, fuzzy, unknown), `intent_to_spec` sort forcing, budget guard (spend over
limit blocks before the call and records nothing), bot `/analyze` flow with the fake bot (plan, run,
cancel, budget exceeded message), and that the bot still works with `llm=None`.

## 5. Phase C: read-only SQL questions

Branch `feature/sql-c`. Depends on B.

- `market/sql.py`: `LISTINGS_SCHEMA_DOC` (a string describing the `listings`, `listing_changes` and
  `harvest_runs` tables and a `listings_current` **view**: `listings` joined to the latest run per
  `listing_id`, with `days_since_posted` and `likes_per_day` computed columns; create the view in
  `storage.SCHEMA` with `CREATE VIEW IF NOT EXISTS`).
- `validate_sql(sql: str) -> str` (pure): strips, requires the statement to start with `SELECT` or
  `WITH` (case-insensitive), rejects any `;` except one trailing, rejects the tokens `ATTACH`, `PRAGMA`,
  `INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `CREATE`, `REPLACE`, `VACUUM` as whole words, appends
  ` LIMIT 200` when no `LIMIT` is present. Raises `ValueError` with a message the LLM can act on.
- `run_readonly_query(database_path: Path, sql: str, max_rows: int = 200, timeout_seconds: float = 5.0) -> QueryResult(columns, rows, truncated)`:
  opens a **separate** connection with `sqlite3.connect(f"file:{path}?mode=ro", uri=True)`, sets
  `PRAGMA query_only = 1`, installs `set_progress_handler` that raises after `timeout_seconds` (monotonic
  clock), fetches `max_rows + 1` to detect truncation, closes the connection in `finally`.
- `llm/sql_question.py`: `SqlPlan(sql: str, explanation: str)`; `answer_with_sql(llm, question, database_path, now)`:
  plan, validate, run; on `ValueError`/`sqlite3.Error` retry once with the error text; then summarise the
  rows with the same `AnalysisSummary` model. Returns the plan, the rows and the summary.
- CLI `carousearch query "<question>" [--sql-only]`; bot `/query <question>` replying with the SQL (in a
  code block), up to 20 rows rendered as text, and the summary.
- Tests: `validate_sql` table of accepted/rejected statements; the read-only connection refuses a write
  even if validation were bypassed (test by calling `run_readonly_query` with an `INSERT` directly and
  asserting `sqlite3.OperationalError`); timeout on a deliberately slow recursive CTE; the retry path.

## 6. Phase D: the screening pipeline (rules and text stages)

Branch `feature/screening-d`. Depends on B. Vision is phase E; this phase builds everything the vision
stage plugs into.

### 6.1 `screening/pipeline.py` (pure)

```python
class StageKind(StrEnum):
    RULE = "rule"
    TEXT = "text"
    VISION = "vision"

class RuleSpec(BaseModel):                       # extra="forbid"; all optional, all AND-ed
    title_include: list[str] = []               # regexes, as TitleFilter
    title_exclude: list[str] = []
    price_min: float | None = None
    price_max: float | None = None
    conditions: list[Condition] = []
    max_age_days: int | None = None
    min_like_count: int | None = None
    exclude_sellers: list[str] = []             # usernames or ids
    min_seller_rating: float | None = None      # needs ListingDetail; listings without a detail pass
    min_seller_reviews: int | None = None

class ModelStageSpec(BaseModel):                # extra="forbid"
    criterion: str = Field(min_length=3, max_length=300)
    threshold: float = Field(default=0.6, ge=0.0, le=1.0)

@dataclass(frozen=True)
class Stage:
    stage_id: str                               # 6 hex chars, from secrets.token_hex(3) at creation (passed in, pure)
    kind: StageKind
    spec: RuleSpec | ModelStageSpec

@dataclass(frozen=True)
class Verdict:                                  # one listing's result from a model stage
    score: float                                # 0..1
    summary: str                                # one line for the card
    detail_json: str                            # the full Judgement (phase E) or TextJudgement, serialised

@dataclass(frozen=True)
class StageOutcome:
    stage: Stage
    input_ids: tuple[str, ...]
    verdicts: dict[str, Verdict] | None         # None for RULE stages
    passed_ids: tuple[str, ...]                 # order preserved from input_ids

@dataclass(frozen=True)
class Pipeline:
    candidate_ids: tuple[str, ...]              # the harvest result the stages narrow
    stages: tuple[Stage, ...] = ()

def push_stage(pipeline, stage) -> Pipeline
def pop_stage(pipeline) -> Pipeline                       # ValueError when empty
def set_threshold(pipeline, stage_id, threshold) -> Pipeline   # ValueError for RULE stages or unknown id
def passed_after(outcomes: Sequence[StageOutcome]) -> tuple[str, ...]   # ids surviving the last outcome, or candidate_ids when none
def apply_threshold(verdicts: dict[str, Verdict], input_ids, threshold) -> tuple[str, ...]
```

### 6.2 `screening/rules.py` (pure)

`run_rule_stage(spec: RuleSpec, rows: Sequence[ListingRow], details: Mapping[str, ListingDetail], now: datetime) -> tuple[str, ...]`.
Each rule is its own small predicate function with a test. `likes_per_day(row, now) -> float | None`
lives here (None without a timestamp; days floored at 1).

### 6.3 `screening/text_judge.py` (LLM, text only)

```python
class TextJudgement(BaseModel):             # extra="forbid"
    score: float = Field(ge=0, le=1)
    verdict: Literal["match", "partial", "no_match", "cannot_tell"]
    reason: str                             # one sentence
    evidence: list[str] = []                # phrases quoted from the title/description
    limits: str | None = None               # what the text does not say
```
`judge_text(llm, criterion: str, listing: ListingRow, detail: ListingDetail | None) -> tuple[TextJudgement, LLMUsage]`.
The prompt gets the title, price, condition label, seller stats and the description (truncated to 1500
characters). `cannot_tell` is scored 0.5 so a threshold of 0.6 drops it and "Loosen" (0.4) keeps it.

### 6.4 Verdict cache (`storage.py`)

```sql
CREATE TABLE IF NOT EXISTS stage_verdicts (
    listing_id     TEXT NOT NULL,
    kind           TEXT NOT NULL,              -- 'text' | 'vision'
    criterion_key  TEXT NOT NULL,              -- screening.pipeline.criterion_key(criterion)
    content_key    TEXT NOT NULL,              -- text: listings.content_hash; vision: photos_hash
    model          TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    score          REAL NOT NULL,
    summary        TEXT NOT NULL,
    detail_json    TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (listing_id, kind, criterion_key, content_key, model, prompt_version)
);
CREATE TABLE IF NOT EXISTS verdict_feedback (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id     TEXT NOT NULL,
    kind           TEXT NOT NULL,
    criterion_key  TEXT NOT NULL,
    criterion      TEXT NOT NULL,
    score          REAL NOT NULL,
    detail_json    TEXT NOT NULL,
    thumbs_up      INTEGER NOT NULL,           -- 1 agree, 0 disagree
    at             TEXT NOT NULL
);
```
`criterion_key(criterion: str) -> str` (pure): lowercase, collapse whitespace, strip surrounding
punctuation, `sha256` hex first 16. `load_verdicts(connection, kind, criterion_key, model, prompt_version,
listing_ids) -> dict[str, Verdict]` only returns rows whose `content_key` matches the listing's current
`content_hash` (text) or photos hash (vision), so a changed listing is re-judged. `save_verdict(...)`,
`record_feedback(...)`, `load_feedback(connection, criterion_key=None) -> list[Feedback]`.

### 6.5 `screening/runner.py` (I/O)

```python
@dataclass(frozen=True)
class RunnerLimits:
    max_model_calls_per_stage: int = 60

class StageTooLargeError(Exception): ...        # carries input count and the cap

def run_pipeline(pipeline, services: ScreeningServices, now, progress=None) -> list[StageOutcome]
def run_stage(stage, input_ids, services, now, progress=None) -> StageOutcome
def estimate_stage(stage, input_ids, services) -> StageEstimate(listings: int, cached: int, calls: int, seconds: float, cost_usd: float)
```
`ScreeningServices` bundles the connection, `CarousellClient`, `StructuredLLM`, the `ListingJudge`
(phase E, may be None), `RunnerLimits`, model names, prompt versions and the clock. `run_stage` for a
model stage: load cached verdicts, compute the uncached ids, raise `StageTooLargeError` if they exceed the
cap, check the budget, fetch `ListingDetail` for uncached ids through the 12-hour detail cache (this is
one Carousell request each, so it is part of the estimate), judge, save, apply the threshold. Judging
runs sequentially (the rate limiter and the budget guard are simpler that way; concurrency is a later
optimisation). `run_pipeline` replays stages in order, feeding `passed_ids` forward.

### 6.6 Bot surface (phase D)

- `ChatDraft` gains `pipeline: Pipeline | None = None` and `pending_stage: Stage | None = None`.
- `/screen [max_pages]`: harvests the current draft's request (`sort` as set; `max_pages` default 3, so at
  most ~144 candidates), stores `Pipeline(candidate_ids=...)` on the draft, replies with the count and the
  stage list (empty) and a **Filters** message with buttons: `Undo last`, `Loosen`, `Tighten`, `Show 5`.
  Button kinds: `POP_STAGE = "sp"`, `LOOSEN = "sl"` (threshold − 0.15, floor 0.1), `TIGHTEN = "st"` (+0.15,
  ceiling 0.95), `SHOW_SURVIVORS = "ss"`, `CONFIRM_STAGE = "sc"`, `CANCEL_STAGE = "sx"`, `WHY = "wy"` (value:
  listing id), `FEEDBACK_UP = "fu"` / `FEEDBACK_DOWN = "fd"` (value: listing id).
- `/filter <criterion>`: the LLM classifies the criterion into a stage (`StagePlan(kind, rule: RuleSpec |
  None, criterion: str | None, clarifying_question: str | None, options: list[str])`); if it returns a
  clarifying question the bot asks it with up to 4 option buttons (`STAGE_OPTION = "so"`, value: index)
  and only then builds the stage. For TEXT/VISION stages the bot shows the estimate and **Run** /
  **Cancel**; RULE stages run immediately. After a stage runs, the Filters message is edited in place to
  list the stages (`1. rule: price ≤ 900, like new · 144 → 61`, `2. text: "no cracks" ≥ 0.6 · 61 → 23`).
- `Show 5` sends the first five survivors as cards; each card's keyboard gains `Why?` (when the listing
  has verdicts) and 👍 👎. `Why?` sends the stored summaries and, for a vision verdict, the evidence
  and observations (phase E), under 4096 characters.
- All pipeline state is in memory on the draft (as drafts are today). Verdicts and feedback are in SQLite.

### 6.7 Tests (phase D)

Pure: push/pop/threshold, every rule predicate, `apply_threshold`, `criterion_key` normalisation,
`passed_after`. Storage: verdict cache hit/miss and content-key invalidation, feedback round trip.
Runner (fake LLM, fake Carousell): cache reuse means zero model calls on a replay; cap raises
`StageTooLargeError` before any call; budget exceeded raises before any call; estimate counts cached
listings as free. Bot (fake bot): `/screen`, `/filter` with a rule, `/filter` with a text criterion
(estimate → run), clarifying question flow, undo, loosen/tighten re-applies the threshold without
calling the LLM, `Why?`, feedback stored.

## 7. Phase E: vision observations and judgements

Branch `feature/vision-e`. Depends on D.

### 7.1 Photos (`screening/images.py`, I/O, extra `vision`)

```python
@dataclass(frozen=True)
class PreparedPhoto:
    index: int                 # position in ListingDetail.image_urls
    source_url: str
    jpeg_bytes: bytes          # long edge <= 768 px, JPEG quality 80
    dhash: str                 # 16 hex chars, difference hash on an 9x8 greyscale (implemented here with Pillow, no imagehash dependency)

def fetch_photos(http_client: httpx.Client, rate_limiter: RateLimiter, urls: Sequence[str], max_photos: int = 2) -> list[PreparedPhoto]
def photos_hash(photos: Sequence[PreparedPhoto]) -> str       # sha256 over the dhashes in order, first 16 hex
def prepare_photo(index, source_url, raw_bytes) -> PreparedPhoto    # pure apart from Pillow decoding
```
Photo bytes are never stored in SQLite; only `photos_hash` and the dhashes (for duplicate detection
across listings, `storage.find_listings_with_dhash`).

### 7.2 Observations (`screening/vision.py`)

Fixed fields are nullable; `"unknown"` or `null` is always allowed. `prompt_version` for observations is
`OBSERVE_PROMPT_VERSION = "1"`.

```python
class Severity(StrEnum): NONE = "none"; MINOR = "minor"; MAJOR = "major"
class Background(StrEnum): HOME = "home"; RETAIL = "retail"; WAREHOUSE = "warehouse"; STUDIO = "studio"; OUTDOOR = "outdoor"; UNKNOWN = "unknown"
class Angle(StrEnum): FRONT = "front"; BACK = "back"; SIDE = "side"; TOP = "top"; PORTS = "ports"; SCREEN_ON = "screen_on"; CLOSE_UP = "close_up"; PACKAGING = "packaging"; OTHER = "other"

class Defect(BaseModel):                        # extra="forbid"
    kind: str                                   # scratch, dent, crack, stain, yellowing, sole_wear, missing_part, other
    severity: Severity
    location: str                               # "left corner", "screen", ...
    photo_index: int

class PhotoObservation(BaseModel):              # extra="forbid"
    photo_index: int
    angles: list[Angle]
    quality: float = Field(ge=0, le=1)          # focus, lighting, resolution, framing
    description: str                            # 1 to 3 sentences of what is visible

class Observations(BaseModel):                  # extra="forbid"
    item_seen: str | None                       # what the photos show, e.g. "iPhone 15 Pro, natural titanium"
    item_matches_title: Literal["yes", "no", "unsure"]
    colour: str | None
    finish: str | None
    variant_visible: str | None                 # storage, size, model number if legible
    is_stock_photo: Literal["yes", "no", "unsure"]
    background: Background
    overlaid_text: str | None                   # watermarks, phone numbers, prices in the image
    multiple_units_visible: bool | None
    authenticity_cues: list[str] = []           # "box label", "receipt", "serial visible"
    completeness: list[str] = []                # "box", "charger", "cable", "manual"
    powered_on_shown: bool | None
    wear_level: float | None = Field(default=None, ge=0, le=1)
    defects: list[Defect] = []
    text_in_image: list[str] = []
    photos: list[PhotoObservation]
    notes: str                                  # free text: anything relevant the fields do not capture
    could_not_determine: list[str] = []         # "screen condition", "underside"
```
`condition_consistency(observations, listing_condition: Condition | None) -> float | None` (pure) maps
`wear_level` against the stated condition band (brand_new 0–0.05, like_new 0–0.2, lightly_used 0.1–0.4,
well_used 0.3–0.7, heavily_used 0.6–1.0) to a 0–1 consistency score (1 inside the band, falling linearly
to 0 at 0.4 outside it).

### 7.3 Judgement

`JUDGE_PROMPT_VERSION = "1"`.
```python
class Evidence(BaseModel):                      # extra="forbid"
    observation: str                            # the observation text or field this rests on
    photo_index: int | None
    supports: bool                              # True = for the criterion, False = against

class Judgement(BaseModel):                     # extra="forbid"
    score: float = Field(ge=0, le=1)
    verdict: Literal["match", "partial", "no_match", "cannot_tell"]
    summary: str                                # one line, derived from the evidence, shown on the card
    evidence: list[Evidence]
    confidence: float = Field(ge=0, le=1)
    limits: list[str] = []                      # what the photos cannot show for this criterion
```
`cannot_tell` is scored 0.5 (same convention as text).

### 7.4 `ListingJudge` protocol and adapters

```python
class ListingJudge(Protocol):
    def observe(self, listing: ListingDetail, photos: Sequence[PreparedPhoto]) -> tuple[Observations, LLMUsage]: ...
    def judge(self, criterion: str, listing: ListingDetail, observations: Observations) -> tuple[Judgement, LLMUsage]: ...
```
`AnthropicListingJudge(api_key, vision_model, text_model, prices)` in `llm/anthropic_adapter.py` sends
the JPEG bytes as base64 image blocks for `observe` and reuses the text `StructuredLLM` for `judge`.
`FakeListingJudge` in `tests/fake_llm.py`. The adapter interface is deliberately the only place that knows
about images, so a local model served with vLLM (OpenAI-compatible endpoint) can be added as
`llm/openai_compatible_adapter.py` later without touching the pipeline.

### 7.5 Caches

```sql
CREATE TABLE IF NOT EXISTS listing_observations (
    listing_id     TEXT NOT NULL,
    photos_hash    TEXT NOT NULL,
    model          TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    dhashes        TEXT NOT NULL,              -- JSON list, for duplicate detection
    json           TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (listing_id, photos_hash, model, prompt_version)
);
```
Judgements go to `stage_verdicts` with `kind = 'vision'` and `content_key = photos_hash`. The runner's
vision path per uncached listing: detail (cached 12 h) → `fetch_photos` → `photos_hash` → observations
(cached) → judgement (cached) → save. The estimate counts photo fetches, observation calls and judgement
calls separately, and prices image input tokens at a fixed `TOKENS_PER_PHOTO = 1100` (comment: checked
against the Anthropic docs 2026-10-25; a 768 px image is about 1,000 to 1,200 tokens).

### 7.6 Bot surface (phase E)

Same buttons as phase D. `Why?` for a vision verdict renders: the summary, then `Evidence:` lines
prefixed `+`/`−` with the photo index, then `Limits:`, then `Seen:` with the key observation fields
(item, colour, wear, defects, stock photo, background, completeness) and the notes. `verdict_feedback`
stores the full `Judgement` JSON so the labelled set carries the evidence too.

### 7.7 Tests (phase E)

`prepare_photo` on two small fixture JPEGs (resize, dhash stability, near-duplicate dhash distance);
`photos_hash` ordering; `condition_consistency` table; runner vision path with `FakeListingJudge`
(observations cached across two criteria: second criterion makes judge calls only; photo change
invalidates both); `Why?` rendering under 4096 characters with a long observation; feedback stored with
the judgement JSON.

### 7.8 Acceptance (phases D and E together)

- `/screen` on a real search, then `/filter under 900 like new`, `/filter no visible scratches`: the
  second stage shows an estimate, runs on at most 60 listings, and `Why?` on a surviving card explains the
  verdict with evidence tied to photo indices.
- Re-running the same criterion (undo, then re-add) makes zero model calls.
- On a set of 30 listings you tag with 👍/👎, at least 80% of the 👎 verdicts have a score within 0.2 of
  the threshold (i.e. the model is uncertain where you disagree, not confidently wrong). If not, the
  threshold default and prompts are tuned before phase F.

## 8. Phase F: scheduled surveys

Branch `feature/surveys-f`. Depends on B.

- Table `saved_analyses (id, name UNIQUE, spec_json, interval_minutes, last_run_at, created_at)` and
  `storage` functions mirroring `saved_searches`.
- `/survey save <name>` stores the last `AnalysisSpec` run in this chat; `/survey list`, `/survey run
  <name>`, `/survey delete <name>`; `/survey every <name> <minutes>` sets the interval (minimum 60).
- `bot/jobs.py`: on start-up, `Application.job_queue.run_repeating(check_surveys, interval=60)`; the
  callback runs every saved analysis whose `last_run_at + interval` has passed, with `reuse_within=None`,
  and sends one message per survey with the table and the deltas against its previous run (median
  change, new listings, listings gone, price drops from `listing_changes` since `last_run_at`). Runs are
  sequential and share the one rate limiter; the daily Carousell request budget (count rows in
  `harvest_runs` × pages since midnight, cap from `CAROUSEARCH_DAILY_REQUESTS`, default 1000) is checked
  before each survey.
- Tests: due-ness logic (pure), the job with a fake bot and fake Carousell, the delta rendering.

## 9. Build order, branches and pull requests

One pull request per phase, in the order A, B, C, D, E, F (C and D are independent and may run in
parallel on separate branches after B). Each PR:

1. Starts from the latest `main`, touches only the modules listed for its phase plus `storage.py`,
   `cli.py`, `config.py`, `pyproject.toml`, `README.md` and the tests.
2. Ends with `.venv/bin/pytest` green (all existing tests plus the new ones), `.venv/bin/ruff check src
   tests` clean, and a README section for every new command or bot feature, in the same style as the
   existing sections (what it does, one example invocation, one example output).
3. Updates the status block at the end of `ROADMAP.md` with a dated paragraph: what was built, what was
   deferred, and any difference from this plan with the reason.
4. Has a description listing the acceptance checks from this document and whether each was run against
   the live site or only against fakes.

Within a phase, build pure modules first with their tests, then storage, then I/O, then the CLI, then the
bot. Do not start the bot surface before the CLI surface of the same phase works end to end.

## 10. Open items (decide when reached, not before)

- Facet codes for category-specific fields (roadmap Level 2 task 1) are still unresolved; until then the
  planner relies on keywords, `title_include` and the common-category list.
- Carousell may return the legacy "Used" condition on old listings (`condition None`); analyses group it
  under `unknown` and the summariser is told so.
- Whether the bot's `[seen]` tag should move from `seen_listings` to `listings.first_seen_at` (it can,
  trivially, but it is not needed for any phase here).
- A local VLM adapter (vLLM / OpenAI-compatible) for `ListingJudge`: the protocol is ready for it; add it
  when the hosted model's cost on real usage is known.
