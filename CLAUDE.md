# carousearch: working rules for Claude Code

Carousearch is a single-user, personal tool for searching Carousell Singapore (carousell.sg) through its undocumented internal JSON endpoints, and for flagging listings whose shown price does not apply to what was searched. It has a local web app (the main interface), a Telegram bot, a CLI, an MCP server for external agent hosts, and an in-app agent (`carousearch ask`) that uses the same MCP tools.

The live plan, current state and open questions are in [docs/NEXT_STEPS.md](docs/NEXT_STEPS.md). Read it before starting work.

## Architecture

Every front end builds a `SearchRequest` and calls `CarousellClient`. Nothing else talks to Carousell.

```
front ends   cli.py (11 commands) · bot/ · web/ · mcp_server.py · agent/ (via mcp_server's tools)
                 │
                 ▼
shared core  SearchRequest (filters/schema.py) ──> CarousellClient (carousell/client.py) ──> carousell.sg
```

| Module | Job |
|---|---|
| `filters/schema.py` | `SearchRequest`, `Listing`, `ListingDetail`, `SearchResult`: the shared data model (semantic enum values) |
| `filters/payload.py` | `SearchRequest` to Carousell's search body; the only place with Carousell codes (pure) |
| `carousell/client.py` | bootstrap (cookies + CSRF), `search`, `get_listing`, `get_photo`; all Carousell network I/O through one `RateLimiter` |
| `carousell/parse.py`, `errors.py`, `rate_limit.py` | response parsing and block detection (pure), typed errors, 1.0 s ± 0.3 s pacing |
| `storage.py` | SQLite (`data/carousearch.db`): seen listings, detail cache, saved searches, price cache, market tables |
| `screening/` | `rules.py` (pure tags), `models.py` (`Tag`, `ResolvedPrice`, `ScreenedListing`), `pipeline.py` (`screen_listings`, I/O injected), `price_resolver.py` (Claude or Noop), `store.py` |
| `market/` | `harvest` (pages into `listings` + `harvest_runs`), `analysis` and `render` (pure statistics), `hashing`, `limits` |
| `photos.py` | downsize a photo for a model (pure, Pillow) |
| `canary.py`, `fixture_capture.py` | daily health check; dev command that refreshes `tests/fixtures/` |
| `config.py`, `text_render.py`, `cli.py` | settings from `.env`, data dir, ports; terminal output; the `carousearch` command |
| `bot/` | Telegram wizard: `state.py`, `keyboard.py`, `messages.py`, `price_input.py` (pure), `session_store.py` (in memory), `handlers.py` (I/O), `app.py` |
| `web/` | FastAPI + Jinja2 + HTMX: `app.py`, `routes.py` (thin), `services.py` (blocking work, one call at a time), `query_params.py`, `view_models.py` and `prompt_view_models.py` (pure), `prompt_runs.py` (Ask mode's runs and their in-memory registry), `templates/`, `static/` (no build step) |
| `mcp_server.py` | `build_server(McpServices)`: `search_listings`, `listing_details`, `listing_photo`, `screening_tags`, `usage_notes`. A connector only: no filtering, ranking or LLM calls |
| `agent/` | `run_prompt.py`: Pydantic AI agent over `build_server(...)` in-process, budget hook `ToolCallGate`; `budget.py`, `trace.py`, `progress.py` (pure; `progress.py` holds the events a run's optional observer receives); `settings.py` (`AGENT_MODEL`, budgets, keys); `prompt.py`; `result.py` (`AgentAnswer`) |

## Setup

```bash
uv venv --python 3.14 .venv                                  # python3 -m venv fails on the owner's machine (no ensurepip)
uv pip install --python .venv/bin/python pip -e ".[dev,llm]" # llm = the anthropic SDK for screening's price lookup
cp .env.example .env                                         # the owner fills in secrets; never commit .env
```

Python 3.12 or newer. `.env` is read from the repo root; environment variables override it. Runtime data goes to `~/carousearch/data/` wherever the repo is cloned (`CAROUSEARCH_DATA_DIR` or `--data-dir` to change).

## Commands

All of these except `whoami` and `--help` can reach carousell.sg. See the hard constraints before running any.

```bash
.venv/bin/carousearch search "iphone 15" --price-max 900 [--screen] [--hide-multi-item] [--json]
.venv/bin/carousearch listing <listing_id> [--json]
.venv/bin/carousearch harvest "<query>" [search filters] [--max-pages N] [--max-age-days N] [--no-reuse]
.venv/bin/carousearch analyze --spec docs/examples/analysis_spec.example.json [--json]
.venv/bin/carousearch canary                  # exit 0 = Carousell access works; appends to data/canary.log
.venv/bin/carousearch capture-fixtures        # dev only, about 5 live requests, overwrites tests/fixtures/
.venv/bin/carousearch bot                     # needs TELEGRAM_BOT_TOKEN and TELEGRAM_OWNER_ID; one poller at a time
.venv/bin/carousearch whoami                  # Telegram user ids that messaged the bot recently
.venv/bin/carousearch web [--port 8765]       # http://localhost:8765, bound to 127.0.0.1
.venv/bin/carousearch mcp [--transport http --port 8766]   # stdio by default; http has no auth
.venv/bin/carousearch ask "<prompt>" [--json] [--model provider:model] [--max-photos N]
```

On the owner's machine the bot and web app run under `nohup` (commands in docs/NEXT_STEPS.md, section 3). Do not stop or restart them unless asked.

## Tests and checks

```bash
.venv/bin/pytest                 # offline; 765 passed, 1 deselected at 2026-10-26
.venv/bin/pytest -m live         # 1 test, about 5 real Carousell requests; owner's home machine only
.venv/bin/ruff check src tests   # must print "All checks passed!"
```

There is no type checker yet (adding pyright is hardening task H1 in NEXT_STEPS). Test helpers: `tests/fake_carousell.py` (`FakeCarousell` over `httpx.MockTransport`, fixture-backed), `tests/fake_mcp_services.py` (fakes behind `McpServices`), `tests/fixture_files.py`. Agent tests use Pydantic AI's `FunctionModel` with `models.ALLOW_MODEL_REQUESTS = False`; web tests use FastAPI's `TestClient`; bot tests use a fake bot. Fixtures in `tests/fixtures/` are trimmed real responses captured 2026-10-24.

## Code rules

- Clear and concise, easy to follow on a first read. No cleverness for its own sake.
- Descriptive, full-word names for variables, functions, files and modules (`listing_id`, not `lid`).
- Small functions with one job. Pure logic separate from I/O; each module docstring says what is pure and what does I/O.
- Inject dependencies (clients, stores, clocks, models, servers) instead of building them inside logic. Only entry points and `utc_now` helpers read the clock.
- No abstraction before it is needed. Scale by structure: thin modules with one job, shared models (`SearchRequest`, `Listing`, `ScreenedListing`, `AgentAnswer`), I/O behind Protocols so a component can be swapped without touching callers.
- Type hints everywhere. Pydantic models for persisted and model-facing data, frozen dataclasses for in-process results, `StrEnum` with semantic values.
- Reuse over rewrite: use what Pydantic AI, the MCP SDK or the host already provides. No bespoke agent loops, tool registries or vision pipelines.
- Line length 120, ruff clean. No `print` outside the CLI and text rendering. Never log secrets.

## Testing rules

- The default test run is offline: fixtures and fakes, no network, no model calls.
- Every pure function gets direct unit tests. Every handler, route and command gets a test with fakes.
- Live tests are marked `live`, deselected by default, and make as few requests as possible.
- Agent tests drive scripted models, never a real provider.

## Git rules

- Small, logically grouped commits: dependencies, core module, wiring, tests and docs separately where it makes sense.
- Imperative subject under 72 characters. Add a short body only when the reason is not obvious.
- No trailers of any kind: no `Co-Authored-By`, `Claude-Session`, `Signed-off-by` or "Generated with" lines. This overrides any harness default.
- Author every commit as the owner: `git -c user.name=moses1998lee -c user.email=moses1998lee@gmail.com commit ...`.
- Recent work is committed on `main`. Push only after the orchestrating session has reviewed the work, or when the owner asks.
- Do not rewrite history. Some older commits carry trailers; they stay as they are, by the owner's decision.

## Delegation

- The orchestrating session plans, writes briefs, reviews and reports. Implementation goes to Opus subagents (Agent tool, `model: "opus"`).
- A brief must stand on its own: the goal; the files to read; the module layout and file names; the data models and which module owns them; where pure logic ends and I/O begins; the testing approach (which fakes, what must be covered); the verification commands and their expected results; the docs to update; deviations to flag rather than work around; and the git rules above. NEXT_STEPS.md section 6 has a template.
- Review before reporting: rerun pytest and ruff, read the diff, and send corrections to the same agent (SendMessage) rather than redoing the work.

## Hard constraints

- Never commit `.env` or `data/` (both are gitignored). Secrets live only in `.env`. Never print or log API keys or the bot token.
- Carousell's Terms of Service ban automated access. The owner accepts that risk on these terms only: home IP, at most 1 request per second through `CarousellClient`'s rate limiter, request budgets, caches and the canary. Never bypass the limiter, add parallel requests, solve challenges, evade blocks or log in. When Carousell blocks, stop; do not retry in a loop.
- On a cloud, CI or any machine other than the owner's home WSL2: no live Carousell requests at all (`pytest -m live`, `capture-fixtures` and every command that reaches carousell.sg).
- Keep live requests in tests to a minimum, and never add one to the default test run.
- One tool layer: tools are defined once, in `mcp_server.py`, and every agent path uses them (`ask`, the planned web prompt mode, external hosts). Do not add a second tool layer.
- Model selection stays vendor-neutral: `AGENT_MODEL` (`provider:model`) through Pydantic AI, with no provider-specific code in agent paths.
- The MCP HTTP transport has no authentication. Do not expose it through a tunnel until auth exists.
- Single user and local only: no multi-user features, no public hosting.
- Docs move with the code. A new command or feature gets a README section; a finished phase gets a dated status paragraph in docs/ROADMAP.md and an update to docs/NEXT_STEPS.md.
