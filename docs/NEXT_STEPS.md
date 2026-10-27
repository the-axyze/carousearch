# Carousearch: next steps

Written 2026-10-26 against commit `26f68c9` on `main` (identical to `origin/main`); updated the same day for phase C, on branch `feature/phase-c-prompt-mode`, on 2026-10-27 for the tiered agent (section 4.6), on branch `feature/tiered-agent-tools`, and again on 2026-10-27 for the generic planning layer (section 4.7), on branch `feature/generic-planning`. This is the live plan. [ROADMAP.md](ROADMAP.md) keeps the original milestones and the dated status history; [CLAUDE.md](../CLAUDE.md) has the working rules.

Labels used below: **verified** means checked against the code, or run, while this file was written on 2026-10-26. **Unverified** means taken from the docs or the owner's account and not re-checked. **Estimate** means a number nobody has measured.

Phase letters: phases C and D below continue the MCP and agent track in ROADMAP.md (phase A = MCP server, phase B = `carousearch ask`). They are not the phases A to F of [ANALYSIS_AND_SCREENING_PLAN.md](ANALYSIS_AND_SCREENING_PLAN.md), which use the same letters for different work.

---

## 1. Current state (2026-10-26)

- Head: phase C (PR #4) and the tiered agent (PR #5) are merged into `main`. The generic planning layer (section 4.7) is on `feature/generic-planning`, branched from `main` on 2026-10-27, not pushed.
- Tests: `.venv/bin/pytest` gives 1061 passed, 1 deselected (the live test), in about 15 s on the owner's machine (verified 2026-10-27 on `feature/generic-planning`; 896 before it). `ruff check src tests` is clean.
- No type checker runs (see H1).

### 1.1 Components

| Component | Code | What exists | Live status |
|---|---|---|---|
| Carousell client | `carousell/`, `filters/` | Home-page bootstrap (cookies + CSRF), search, listing detail, photo download (https on `media.karousell.com` only), one re-bootstrap on 401/403/422, rate limiter 1.0 s ± 0.3 s, typed errors | Used live by every front end. Canary passed 2026-10-24 |
| CLI | `cli.py`, `text_render.py` | 11 commands: `search`, `listing`, `harvest`, `analyze`, `canary`, `capture-fixtures`, `bot`, `whoami`, `web`, `mcp`, `ask` | `search`, `listing`, `canary` run live in phase 1 |
| Screening | `screening/` | Title rule, description price-list and floor-price rules, below-market rule, a 10-request detail budget per page, a price resolver (the worker model through Pydantic AI, or Noop without its key), 12-hour detail cache, price cache | Rules checked live 2026-10-25. **The price resolver has never called a real model** (it now would, with `OPENAI_API_KEY` set: see section 4.6) |
| Market analysis | `market/`, `harvest`, `analyze` | Phase A of the analysis plan (section 1.4) | Offline tests only. The live acceptance run of the example spec has not been done (ROADMAP, phase A status) |
| Telegram bot | `bot/` | Step-by-step search wizard, one locked session per chat (30-minute idle expiry, in memory), Next 5 / Refine / End, Details, `/save` and `/saved`, `[seen]` tags, multi-item caption line (title rule only) | **Used live by the owner** (`data/bot.log`). Running now |
| Web app | `web/` | One page: keywords, price, condition, delivery, sort, category, "Hide multi-item posts" with a collapsed Hidden (n) list of the hidden posts and their reasons; card grid with resolved-price row and badges; Details panel with every tag's reason; Load more; 2-minute repeat memory. **Ask mode** (Filters \| Ask switch): runs the `ask` agent in the background, polls its progress every second, shows kept cards from Carousell's data with the agent's reason, Dropped (n), notes and the tool trace; one run at a time; filter searches wait while it runs | Filters mode: **used live by the owner** (`data/web.log`), running on 127.0.0.1:8765 (the running process predates phase C). Ask mode and the Hidden section: **built, scripted models only, not run in a browser** |
| MCP server | `mcp_server.py`, `mcp_instructions.md` | Seven read-only tools (`category_fields` and `fields` on `search_listings` added 2026-10-27, `sample_descriptions` the same day; search cards carry `description_snippet`, details `attributes`) over stdio or streamable HTTP; no auth on HTTP | **Verified over stdio with a script** driving the SDK client, 2026-10-26 (3 Carousell requests). **Not registered in any host yet** (verified: `~/.claude.json` has no `carousearch` entry) |
| In-app agent | `agent/`, `ask`, `eval` | Two phases: a planning phase (discovery tools on a 6-call budget, returns a `SearchPlan`) and an execution phase over the MCP tools plus five local tools (`run_python` sandbox over the run's `listings.db`, `extract_fields`, `judge_listings`, `judge_photos`, `photo_metrics`); planner and worker models; no item knowledge anywhere (tested); method templates; optional owner's `knowledge/` notes; capped hosted web search; prompt caching; per-run budgets (40 tool calls, 15 photos, 200 listings, 3 web searches) enforced by one gate; kept / dropped / not shortlisted / plan review / notes / traces / cost estimate; `--json`, `--plan-only`, `--plan-file`; `carousearch eval` over seven cross-category prompts | **Run live 2026-10-27** with `openai:gpt-5.4-mini` and `openai:gpt-5.4-nano` on the two motorcycle prompts, twice each (section 4.6) |
| Canary | `canary.py` | One search plus one detail; exit code; one line in `data/canary.log` | One logged run (2026-10-24, OK). Not scheduled: `crontab -l` says no crontab (verified) |

### 1.2 What has and has not been verified live

Verified live:
- The client, search filters, paging and listing detail (phase 1, 2026-10-24) and the canary.
- Screening rules on a real search (2026-10-25, ROADMAP phase 3a): no false multi-item tags on 20 single-variant titles; one placeholder price tagged below market.
- The web app (curl check 2026-10-25, then the owner's own searches, logged in `data/web.log`) and the wizard bot (the owner's own use, logged in `data/bot.log`).
- The MCP server over stdio: tool list, one search, one photo (2026-10-26).
- `carousearch ask` with OpenAI models (2026-10-27): four runs, US$0.026 to US$0.039 each, 5 to 14 Carousell requests each (home-page load included); the sandbox, `judge_listings`, `photo_metrics`, `category_fields`, listings.db and the cost line all worked live (section 4.6). `judge_photos` ran live once (prompt B, first run).
- The planning layer (2026-10-27): `carousearch eval --live` over seven cross-category prompts, and the R15 prompt through `ask --plan-only` and `--plan-file`; `sample_descriptions`, `extract_fields` (regex and worker model), the plan checks, the sampling reminder and the plan review all worked live (section 4.7).

Never run live:
- `carousearch ask` with an Anthropic model (no key), and the hosted web search (no live run asked for one).
- The web app's Ask mode, with its plan step (Execute, Edit, Cancel, Save to knowledge/), and Hidden (n) section: no real model, no live Carousell, and no real browser. The mode switch, CSS (dark theme, phone width), HTMX polling and out-of-band swaps were checked only through `TestClient` tests and, for the Hidden swaps, a jsdom replay the C4 implementer reported (unverified; not kept in the repo).
- The price resolver against any real model. It now runs on the worker model (`openai:gpt-5.4-nano` by default) and will run on the next `search --screen` or web search with multi-item posts, now that `OPENAI_API_KEY` is set.
- The MCP server inside a real host (Claude Code, Codex CLI, Claude desktop, ChatGPT), and the HTTP transport through a tunnel.
- The `analyze` example spec against the live site, and a real two-page `sort=recent` fixture pair.

Why: until 2026-10-27 `.env` held no model key. It now has `OPENAI_API_KEY` and `AGENT_MODEL=openai:gpt-5.4-nano` (the deprecated alias, which sets both tiers to nano; the live runs overrode it in the environment). `ANTHROPIC_API_KEY` is empty (verified by key name and length on 2026-10-27).

### 1.3 Running on the owner's machine

- `carousearch web --port 8765` and `carousearch bot`, started with `nohup` from the repo root, writing to `data/web.log` and `data/bot.log` (verified from the processes' open files). A process keeps the code it started with, so restart both after pulling changes (section 3.4).
- Both die when WSL shuts down (a Windows restart, `wsl --shutdown`, or WSL stopping an idle distro).
- systemd is already enabled in this distro (`/etc/wsl.conf` has `systemd=true`, PID 1 is systemd; verified), but no user services exist yet (H3).

### 1.4 The market-analysis track (PR #3)

Another session wrote [ANALYSIS_AND_SCREENING_PLAN.md](ANALYSIS_AND_SCREENING_PLAN.md) (merged as PR #1) and built its phase A on `feature/market-analysis-a` (merged as PR #3, 2026-10-25):

- Tables `listings` (one row per listing, overwritten when a content hash over title, price and condition changes), `listing_changes`, `harvest_runs`, `harvest_run_listings`.
- `market/hashing.py`, `market/limits.py`, `market/harvest.py` (walks `next_page`, reuses an identical run from the last 30 minutes when it covers the request), `market/analysis.py` and `market/render.py` (price statistics with the `statistics` module, IQR outliers, grouping by condition, ISO week or seller).
- Commands `carousearch harvest` and `carousearch analyze --spec FILE|-`, and `docs/examples/analysis_spec.example.json`.

Deferred, per ROADMAP: the live acceptance run and a real two-page fixture. Phases B to F of that plan (an LLM layer for prompt-to-filters and analysis planning, read-only SQL questions, the staged screening pipeline, vision verdicts, scheduled surveys) have not been started; there is no `llm/` package. Screening phase 3a is a narrower first slice of that plan's phase D. Whether and how to resume the plan is open question Q7.

### 1.5 Known gaps

| Gap | Where | Tracked as |
|---|---|---|
| Only ruff runs; no type checker. The Protocols (`CarousellSource` and `ListingDetailCache` in `mcp_server.py`, `CarousellSource` in `web/services.py`, `ScreeningStore`, `PriceResolver`, `ListingDetailSource`) are not checked at runtime or statically | whole repo | H1 |
| ~~`(serve or mcp_server.serve)(...)` in `run_mcp_command` is hard to read~~ Done 2026-10-27: two plain lines | `cli.py` | Done (H2) |
| Bot and web app run under `nohup` and die on WSL restart | operations | H3 |
| The MCP HTTP transport has no authentication; do not tunnel it publicly yet | `mcp_server.py` | H4 |
| Photos are not cached; every `listing_photo` call is a Carousell request | `mcp_server.py`, `carousell/client.py` | H5 |
| Bot sessions live in memory; a restart forgets them and old buttons stop working | `bot/session_store.py` | H6 |
| The 40-request budget per task is advice to external hosts; only `ask` enforces budgets | `mcp_instructions.md`, `agent/budget.py` | H7 |
| The title rule misses brand-only lists. Verified: "Trek \| Giant \| Specialized road bike" and "Shimano / SRAM groupset" get no tag, while "Honda \| ADV150 \| R15 \| MT15" does | `screening/rules.py` | H8 |
| ~~`ask` does not report token usage~~ Done 2026-10-27: `AgentAnswer.usage` per model and a cost-estimate line in `ask` and in Ask mode's notes | `agent/` | Done (H9) |
| The canary is not scheduled and nothing alerts on failure | operations | H10 |
| ~~The price resolver calls the `anthropic` SDK directly~~ Done 2026-10-27: it runs on the worker model through Pydantic AI (verified: no `anthropic` import left in `screening/`) | `screening/price_resolver.py` | Done (H11, Q6) |
| The planner (gpt-5.4-mini) follows multi-step prompts only partly. Since the planning layer (section 4.7) it states a grouped benchmark and reviews every plan item, but its execution still varies run to run (it skipped photo checks and once extracted fields on one page only before the prompt was tightened) | `agent/prompt.py`, model choice | T1 |
| Run folders `data/runs/*` and `judgement_cache` rows are never cleaned up | `agent/run_directory.py`, `storage.py` | T2 |
| `run_python` is confined by Landlock (Linux 5.13+); elsewhere only the working directory, an empty environment and resource limits apply | `agent/sandbox.py` | T3 (accepted) |
| The category tree is a snapshot (2026-10-27); new Carousell categories are missing until it is recaptured | `carousell/category_tree.json` | T4 |
| ~~"Hide multi-item posts" in the web app only counts the hidden posts; their reasons are not shown anywhere~~ Done in C4: a collapsed Hidden (n) section lists each hidden post with its tag reasons (verified: `hidden_rows` in `web/view_models.py`, tests in `tests/test_web_routes.py`) | `web/` | Done (C4) |
| ~~No photo-quality flag in the web app~~ Available in the web app's Ask mode, where the agent can look at photos with `listing_photo` (scripted-model tests only). Filters mode and the bot still have no photo flag | `web/`, `bot/` | Done for the web app (Ask mode); bot: none |
| `python-telegram-bot` is installed without the `job-queue` extra (APScheduler is absent; verified), which the alert job needs | `pyproject.toml` | Phase D, task D1 |
| No HTML or Playwright fallback if Carousell blocks the JSON API (TODO in `CarousellClient.search`) | `carousell/client.py` | Build only if the canary starts failing |
| Commit trailers: `a39b9f8`, `9e92d10` and `307dbe0` carry `Co-Authored-By` and `Claude-Session` lines. So do the older `609f1f2`, `27d921a`, `4b97362`, `096ccd6` and `420d384` (verified). History is not rewritten, by decision | git history | none |

---

## 2. Requirements as currently understood

Owner and use:
- One user, in Singapore, for personal use. Runs on the owner's home WSL2 machine.
- Carousell's Terms of Service ban automated access. The owner accepts the risk, mitigated by: home IP only, at most 1 request per second, request budgets, caches, and the canary to notice breakage early.

Interfaces:
- The **local web app** is the main interface.
- The **Telegram bot** stays for quick lookups from the phone and for future alerts.
- The **MCP server** lets external agent hosts (Claude Code, Codex CLI, Claude desktop, ChatGPT through a tunnel) drive searches.
- **`carousearch ask`** is the in-app agent over the same MCP tools, through Pydantic AI. The model is chosen by `AGENT_MODEL` (`provider:model`); staying vendor-neutral is a requirement.

What to flag:
- Catalogue or multi-item posts whose displayed price does not apply to the searched variant.
- Poor-quality photos.
- Titles listing several brands or models separated by `|` or `/`.
- Business sellers are fine and must not be filtered out.
- Every drop must come with a visible reason.

How to build:
- Reuse over rewrite: no bespoke agent loops, tool registries or vision pipelines where the SDK or host already provides them. Tools are defined once (the MCP server) and every agent path uses them.
- Code, test and git rules: see CLAUDE.md. Build work is delegated to Opus subagents with explicit briefs.
- Secrets live only in `.env` (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_OWNER_ID`, optional `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` and the `AGENT_*` settings). `.env` and `data/` are never committed.

Non-goals:
- Multi-user support, accounts or sharing.
- Public hosting. Everything binds to 127.0.0.1; the only planned outside access is an authenticated tunnel for ChatGPT (H4), and only if the owner wants it.
- Filtering by seller type.
- Logging in to Carousell, solving Cloudflare challenges, or redistributing listing data.

How the requirements map to the code today:

| Requirement | Covered by | Gap |
|---|---|---|
| Flag catalogue posts whose price is not for your variant | `screening/` rules and price resolver; web badges and price row; bot caption line; `screening_tags` tool | Resolver never run live (phase L) |
| Flag poor-quality photos | `ask`, the web app's Ask mode and external hosts via `listing_photo` | Not in Filters mode or the bot; never run against a real model (phase L) |
| Flag multi-brand or multi-model titles split by `\|` or `/` | Title rule (models, storage sizes, catalogue phrases) | Brand-only lists missed (H8) |
| A reason for every drop | Tag reasons; `ask` and Ask mode dropped lists; the web app's Hidden (n) section | None known |
| Vendor-neutral models | `AGENT_PLANNER_MODEL` and `AGENT_WORKER_MODEL` through Pydantic AI, also for the price resolver | None known |
| Tools defined once | `mcp_server.py` used in-process by `ask` and the web app's Ask mode | Keep it that way in phases D and M |

---

## 3. Environment and operations

### 3.1 Files

| Path | What | In git |
|---|---|---|
| `.env` | Secrets and settings (see `.env.example`) | Never |
| `data/carousearch.db` | SQLite: `seen_listings`, `listing_detail_cache`, `saved_searches`, `price_resolution_cache`, `listings`, `listing_changes`, `harvest_runs`, `harvest_run_listings` | Never |
| `data/bot.log`, `data/web.log` | Output of the `nohup` processes | Never |
| `data/canary.log` | One line per canary run | Never |
| `tests/fixtures/` | Trimmed real Carousell responses (2026-10-24) | Yes |
| `docs/examples/` | Example analysis spec, web screenshots | Yes |

`data/` defaults to `~/carousearch/data/` (`CAROUSEARCH_DATA_DIR` or `--data-dir` to change).

### 3.2 Ports and processes

| Process | How it runs | Port | Notes |
|---|---|---|---|
| Web app | `nohup`, long-running | 8765 on 127.0.0.1 | WSL2 forwards it to the Windows browser's `localhost` |
| Telegram bot | `nohup`, long-running | none (long polling) | Only one poller per bot token; a second one gets "Conflict" |
| MCP server, stdio | Started and stopped by the host | none | Logs to stderr |
| MCP server, HTTP | Manual, `--transport http` | 8766 on 127.0.0.1 | No auth; not running now |
| `ask`, CLI commands | One-shot | none | Each opens its own Carousell session and rate limiter |

Each process has its own Carousell session and rate limiter, and they all share the SQLite file.

### 3.3 Settings in `.env`

| Variable | Used by | Default |
|---|---|---|
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_OWNER_ID` | `bot`, `whoami` | none; the bot exits with code 2 without them |
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` | whichever provider the planner and worker models name | unset: the price resolver is off, `ask` exits 2, Ask mode's Run is disabled |
| `AGENT_PLANNER_MODEL` | `ask`, the web app's Ask mode (the agent loop) | `openai:gpt-5.4-mini` |
| `AGENT_WORKER_MODEL` | the judge tools and screening's price resolver | `openai:gpt-5.4-nano` |
| `AGENT_MODEL` | deprecated alias: fills whichever of the two above is unset | unset |
| `AGENT_MAX_TOOL_CALLS`, `AGENT_MAX_PHOTOS`, `AGENT_MAX_LISTINGS`, `AGENT_MAX_WEB_SEARCHES` | `ask`, the web app's Ask mode | 40, 15, 200, 3 |
| `CAROUSEARCH_DATA_DIR` | everything | `~/carousearch/data` |

The web app reads its settings once at start-up, so it needs a restart after `.env` changes.

### 3.4 Restarting the bot and web app

```bash
cd ~/carousearch
pkill -f "carousearch bot"; pkill -f "carousearch web"
nohup .venv/bin/carousearch bot >> data/bot.log 2>&1 &
nohup .venv/bin/carousearch web --port 8765 >> data/web.log 2>&1 &
tail -n 20 data/bot.log data/web.log
```

README.md documents the bot's `nohup` line; the web line above matches the running process. Only restart them when the owner asks, or as part of a task the owner approved.

### 3.5 The canary

`.venv/bin/carousearch canary` runs a fixed "iphone" search (20 per page), needs at least 10 parsed listings, then fetches the first listing's detail. It prints one line, appends it to `data/canary.log`, and exits 0 or 1. The failure reason names the case (`CarousellBlockedError`, `CarousellSchemaError`, `CarousellHTTPError`); README.md, "When Carousell changes", says what to do for each. It is not scheduled yet (H10).

### 3.6 A fresh machine

1. Python 3.12+ and [uv](https://docs.astral.sh/uv/). Clone `git@github.com:the-axyze/carousearch.git`.
2. `uv venv --python 3.14 .venv` and `uv pip install --python .venv/bin/python pip -e ".[dev,llm]"`.
3. `.venv/bin/pytest` and `.venv/bin/ruff check src tests` must pass offline.
4. `cp .env.example .env`; the owner fills it in.
5. Live use only from a Singapore residential connection. Cloudflare challenges plain clients and is reported to challenge datacenter IPs (FEASIBILITY.md, section 1.4). From any other machine, do not run live commands.

### 3.7 Only the owner can do these

- Create the bot in BotFather and put `TELEGRAM_BOT_TOKEN` in `.env`; find `TELEGRAM_OWNER_ID` (`carousearch whoami` or @userinfobot).
- Add `ANTHROPIC_API_KEY` and/or `OPENAI_API_KEY`, and set a spending limit in the provider's console.
- Register the MCP server with a host: `claude mcp add --scope user carousearch -- /home/moses/carousearch/.venv/bin/carousearch mcp` (docs/MCP.md has Codex CLI, Claude desktop and ChatGPT).
- Anything needing `sudo` or Windows: `loginctl enable-linger`, a Windows Task Scheduler task that starts WSL at logon, `wsl --shutdown`.
- Approve live runs (`pytest -m live`, `capture-fixtures`, the phase L checklist), any tunnel, and pushes.

---

## 4. Remaining work

Effort is in solo-developer days, as in ROADMAP.md, and is a rough estimate.

| Phase | Goal | Effort | Depends on | Needs the owner |
|---|---|---|---|---|
| H1 | pyright (H2, the `cli.py` readability fix, is done) | 0.5 day | nothing | no |
| H9 | Token usage in `AgentAnswer` | **Done** (2026-10-27, section 4.6) | | |
| L | First live validation with a real model | 1 hour left: price resolver, web app Ask mode in a browser, budget refusal (section 4.4) | OPENAI_API_KEY (now set) | yes: present for the run |
| T | Tiered agent: planner and worker models, sandbox, judge tools, photo metrics, category fields, web search, cost | **Done** (2026-10-27, section 4.6; branch `feature/tiered-agent-tools`, not pushed) | C | review, merge |
| G | Generic planning layer: plan first, for any kind of item, shown before it is spent; extraction, templates, knowledge notes, evals | **Done** (2026-10-27, section 4.7; branch `feature/generic-planning`, not pushed) | T | review, merge; open the plan step in a browser |
| T1 to T4 | Planner quality on multi-step prompts, cleanup of run folders and judgements, category tree refresh | 0.5 to 1 day | T, G | T1: choose the planner model |
| C | Prompt mode in the web app | **Done** (C1 to C5, 2026-10-26; branch `feature/phase-c-prompt-mode`, PR pending) | nothing (L before release) | screenshots after L |
| H8 | Title rule for brand-only lists | 0.5 day | nothing | no |
| D | Saved searches and prompts, alert job | 2 to 3 days | C for "save a prompt" (done) | to enable alerts |
| H3, H10 | systemd user services; scheduled canary with a Telegram alert | 1 day | nothing | yes: linger, Task Scheduler |
| H4 to H7 | MCP auth, photo cache, bot sessions in SQLite, external request budget (H11, the vendor-neutral resolver, is done) | 2 to 3 days | as listed | H4 only if a tunnel is wanted |
| M | Saved searches over MCP (optional) | 0.5 day | D | decide Q9 |

Suggested order: review and merge G, then T1 (with `carousearch eval --live` as the yardstick), L's remaining browser check, H1, H8, D (saved prompts can now save the agreed plan), H3 and H10, then the rest as needed.

### 4.1 Phase C: prompt mode in the web app (done 2026-10-26, except the after-L acceptance bullet)

**Status.** C1 to C5 are done on branch `feature/phase-c-prompt-mode` (PR pending). [ROADMAP.md](ROADMAP.md), "Web app: phase C status, prompt mode", has what was built and the deviations from the design below (among them: the callback is `observer`; waiting searches are a partial that asks again every 2 s; `POST /ask` also refuses non-HTMX requests). Every acceptance bullet but the last is covered by offline tests (verified); nothing has run against a real model or in a real browser. The design below is kept as it was proposed.

**Goal.** In the web app, type a plain-English request, watch the agent work, then read the kept listings as cards with the agent's reason, every dropped listing with its reason, the notes and the tool trace. Built on `agent.run_prompt.run_prompt`; no new tools and no second agent loop.

**Design (proposed; the implementer flags any deviation).**
- One page with a mode switch, "Filters | Ask", above the search box. Ask shows a textarea and a Run button. A prompt never runs from a URL on page load, so a bookmark or reload cannot spend money.
- A run is a background asyncio task in the web process. `POST /ask` checks the input and starts the task, then returns a progress partial. That partial polls `GET /ask/{run_id}` every second (HTMX `hx-trigger="every 1s"`) until the run ends, then shows the answer. No server-sent events, websockets or new JavaScript dependency.
- New module `web/prompt_runs.py`: `PromptRun` (id, prompt, started_at, status, trace so far, answer, error text) and an in-memory registry that keeps the last 10 runs.
- Progress: `run_prompt` gains an optional injected callback (for example `on_tool_call`) that `ToolCallGate` calls when a call starts and ends, carrying the budget counters (calls used of 40, photos of 15). `ask` passes nothing and is unchanged.
- Card data comes from Carousell, not from the model. The gate, or a pure function over the tool results, records the listings `search_listings` returned, by id. A kept card shows that `Listing` (thumbnail, `price_display`, condition, seller) plus the model's reason. A kept id that appeared in no search result is shown with a warning.
- Dropped: a collapsed "Dropped (n)" section listing title, link and reason for every dropped listing. Notes go above the cards; the trace is a collapsed list, one line per call, with status (`ok`, `failed`, `refused`, `not run`) and duration.
- Concurrency: reuse the web app's client and detail cache, `McpServices(carousell=services.carousell, detail_cache=services.screening.store)`. Hold `WebServices.turn` for the whole run so a filter search never uses the client at the same time as an agent tool; filter searches wait and the page says an agent run is in progress. `McpServices.lock` stays the inner lock. One run at a time; a second Run is refused with a message.
- No provider key: Ask mode names the missing variable (`AgentSettings.missing_key_variable()`), says to add it to `.env` and restart, and disables Run.
- Errors (`AgentRunError`, `UserError`, Carousell errors) show a calm message; details go to the log only, as the existing routes do.

**Tasks.**
1. C1 (done): progress callback and search-result capture in `agent/run_prompt.py` (pure helpers in `agent/trace.py` or a new small module), with `FunctionModel` tests. Built as `agent/progress.py`.
2. C2 (done): `web/prompt_runs.py`; routes `POST /ask` and `GET /ask/{run_id}`; `AgentSettings` loaded by `carousearch web` and passed in through `WebSettings`.
3. C3 (done): pure view functions (kept card, dropped row, trace row, progress) in `web/view_models.py`, or in a new `web/prompt_view_models.py` if that file gets too long; templates and CSS. Built in `web/prompt_view_models.py`.
4. C4 (done): in Filters mode, "Hide multi-item posts" lists the hidden posts in a collapsed "Hidden (n)" section with their tag reasons.
5. C5 (done): docs: README "Web app" section, a ROADMAP status paragraph, this file.

**Acceptance** (all but the last bullet verified by tests on the branch).
- Through `TestClient` with a scripted `FunctionModel`: `POST /ask` returns the progress partial; polling shows each tool call as it finishes; the final partial shows kept cards with real thumbnail and price plus the model's reason, a Dropped section with every dropped listing and its reason, the notes, and the trace with statuses.
- A budget refusal appears as `refused` in the trace and in the notes.
- Without a provider key, Run is disabled, the message names the variable, and no model or Carousell call is made.
- A filter search during a run never overlaps an agent tool call (a fake client that fails on overlapping calls proves it).
- With "Hide multi-item posts" on, the hidden posts are listed with their reasons.
- No GET request starts a model run.
- `pytest` green (679 plus the new tests), ruff clean. Done: 765 passed.
- After phase L: one real prompt in the browser, with desktop and mobile screenshots in `docs/examples/`. **Open.**

**Verify.**
```bash
.venv/bin/pytest -q
.venv/bin/pytest tests/test_web_routes.py tests/test_agent_run.py -q
.venv/bin/ruff check src tests
```

### 4.2 Phase D: saved searches and prompts, and alerts (2 to 3 days)

**Goal.** Save a filter search or a prompt from the web app (and from the bot), and have the bot re-run saved items on a schedule and push only new matches to Telegram.

**Design (proposed).**
- Storage: a new table `alerts` (id, name unique, kind `search` or `prompt`, `request_json`, `prompt`, `interval_minutes`, `paused`, `last_run_at`, `created_at`) and `alert_seen` (alert_id, listing_id, first_seen_at; primary key on both ids). The existing `saved_searches`, `/save` and `/saved` keep working unchanged; the analysis plan's convention allows new tables and guarded `ADD COLUMN` only.
- For a prompt, the "plan" saved is the prompt plus the search requests the agent actually made. `ToolCallGate.records` holds the arguments each call was made with, so the `search_listings` requests can be recovered from a run. Since phase C, the web app keeps its recent runs in `PromptRunRegistry` (`web/prompt_runs.py`); a `PromptRun` has the prompt, its `ToolCallEvent`s (each with an arguments summary, not the full arguments) and the listings its searches returned. "Save a prompt" in the web app can start from the run in the registry; recovering the exact search requests needs the gate's records, which `run_prompt` does not return today.
- Each run starts cheap: the saved search sorted by recent, page 1 (one request), then new ids against `alert_seen`.
  - Search alerts: screen the new listings with `screen_listings` (rules, budgeted details, the resolver when a key exists), leave out multi-item posts if the alert says so, and push the rest.
  - Prompt alerts: only when there are new listings, run the agent with the saved prompt and the new listing ids, under a smaller budget (for example 10 tool calls, 3 photos). Push the kept listings with reasons and the number dropped.
- The job runs in the bot process on python-telegram-bot's JobQueue: add `python-telegram-bot[job-queue]` (APScheduler). A new `bot/jobs.py` checks every 60 s; which alerts are due is a pure function of the alerts and the time. Runs are sequential through the bot's client. A daily background cap (default 300 Carousell requests, from ROADMAP's optional alerts section) is checked before each run. On `CarousellBlockedError`, pause all alerts for an hour and send one message.
- A new alert's first run marks the current results as seen without pushing, so it never floods.
- Messages: at most one Telegram message per alert run (a list of price, title, link, reason), and nothing when there are no new matches.
- Surfaces: a "Save as alert" button under web results (both modes); bot `/alerts` with Pause, Resume and Delete buttons (callback_data at most 64 bytes, enforced by `state.encode_action`). Default interval 60 minutes, minimum 30 (Q4).

**Tasks.** D1 dependency and storage (tables, functions, tests). D2 pure due-ness and diff logic. D3 the run function for search alerts. D4 the run function for prompt alerts, reusing `run_prompt`. D5 the JobQueue wiring and `/alerts`. D6 the web "Save as alert" button. D7 docs.

**Acceptance.**
- A listing is never pushed twice for the same alert.
- At most one Telegram message per alert run; none when nothing is new.
- The daily cap is enforced; 5 alerts at the default interval stay under it over a simulated day (fake clock, counting fake client).
- A new alert's first run pushes nothing.
- Paused alerts do not run; deleting an alert removes its `alert_seen` rows.
- A prompt alert makes no model call when there is nothing new.
- Alerts survive a bot restart.
- `pytest` green, ruff clean. Live: the owner enables one search alert and gets a message for a new listing.

**Verify.** `.venv/bin/pytest -q`, `.venv/bin/ruff check src tests`; then, on the owner's machine with approval, restart the bot (section 3.4), create one alert at the minimum interval, and watch `data/bot.log`.

### 4.3 Hardening

| Id | Task | Acceptance | Effort |
|---|---|---|---|
| H1 | Add pyright to the `dev` extra with `[tool.pyright]` in `pyproject.toml` (basic mode, `src` first, then `tests`). Fix what it finds, starting with the Protocol implementations. Add the command to CLAUDE.md | `.venv/bin/pyright` reports 0 errors. Unverified: the PyPI `pyright` package downloads a Node runtime on first use, while `basedpyright` bundles one | 0.5 day |
| H2 | In `cli.py` `run_mcp_command`, replace `(serve or mcp_server.serve)(mcp_server.build_server(services), transport, port)` with two plain lines: pick the serve function, then call it | `tests/test_cli.py` passes unchanged | minutes |
| H3 | systemd user services `carousearch-bot.service` and `carousearch-web.service`, kept as templates in the repo (for example `deploy/systemd/`) with install steps in README. The owner runs `sudo loginctl enable-linger moses` and adds a Windows Task Scheduler task that starts WSL at logon. Stop the `nohup` processes first: only one bot may poll | After `wsl --shutdown` and a Windows logon, both run without anyone typing a command; `systemctl --user status` shows them; logs via `journalctl --user -u carousearch-bot` | 0.5 day plus owner steps |
| H4 | Auth for `mcp --transport http`. The installed mcp 2.3 `MCPServer` accepts `token_verifier` and `auth` settings (verified in the installed source). Use a static bearer token from `.env` (for example `MCP_HTTP_TOKEN`) and refuse to start HTTP without it. Whether ChatGPT's connector can send a static bearer token is unverified; the alternative is Cloudflare Access in front of a named tunnel | No token gives 401; the right token lists the tools; stdio is unaffected | 0.5 to 1 day |
| H5 | Cache downsized photos (a SQLite table keyed by URL and `max_width`, or files under `data/photos/`) with an expiry and a size cap | A second `listing_photo` for the same URL and width makes no Carousell request (counting fake) | 0.5 day |
| H6 | `SqliteSessionStore` with the same `get`, `remove_if_expired`, `set`, `clear` as `SessionStore`; pure functions to turn a `SearchSession` into JSON and back | A session survives a bot restart; idle expiry still works; the handler tests pass with either store | 0.5 to 1 day |
| H7 | A Carousell request budget for external hosts: a rolling window per server process (for example `MCP_MAX_REQUESTS_PER_HOUR`), counted in `McpServices`, with cache hits free. Over the cap, tools raise a `ToolError` telling the host to stop. Keep the counting pure and out of `agent/` (`agent/budget.py` imports `mcp_server`, not the other way round) | Calls past the cap never reach the fake client; cached details do not count; `ask` budgets unchanged | 0.5 day |
| H8 | Extend the title rule so titles listing several brands or products split by `\|` or `/`, without digits, are tagged (the two verified misses in section 1.5), with a table of ordinary titles that must stay untagged ("Black / White", "w/ box", single-brand titles) | Both misses tagged; at least 10 negatives untagged; all existing rule tests pass | 0.5 day |
| H9 | **Done 2026-10-27.** `AgentAnswer.usage` (per model: requests, input, cached input and output tokens, web searches, estimated US$), printed as a cost line by `ask` and in Ask mode's notes | Scripted-model tests show the counts; `--json` includes them (both verified) | done |
| H10 | Schedule the canary (a systemd timer at 08:00 alongside H3, or cron) and send the owner a Telegram message when it fails, using the bot token and owner id from `.env` | A forced failure (fake client) sends one message; a success sends none | 0.5 day |
| H11 | **Done 2026-10-27.** The price resolver uses Pydantic AI structured output on the worker model (`AGENT_WORKER_MODEL`) instead of a separate `PRICE_MODEL` | Resolver tests pass with a scripted model; no direct `anthropic` import left in `screening/` (both verified) | done |

### 4.4 Phase L: first live validation, once an API key exists (1 to 2 hours, owner present)

Before starting: the owner adds `ANTHROPIC_API_KEY` to `.env` and sets a spending limit in the Anthropic console. The `anthropic` package is already installed in `.venv` (verified). H9 should be done first; otherwise read the cost from the console.

Expected cost (estimate, never measured): a price lookup is about US$0.0001 to US$0.0002 (README, "Screening"); an `ask` run is "a few US cents" with Claude Haiku 5.5 (README, "Ask the agent"). The whole checklist should cost well under US$0.50 and stay under about 150 Carousell requests.

1. **Canary.** `.venv/bin/carousearch canary` must print `OK`. If it fails, stop.
2. **Price resolution, CLI.** `.venv/bin/carousearch search "nintendo switch" --limit 48 --screen`. Look at: the summary line ends with "your variant's price read from the description by Claude" instead of "price resolution off"; multi-item posts show a YOUR PRICE from the description, or "not stated"; open three of them on Carousell and check by eye that the price matches the description. Run the same command again: the number of rows in `price_resolution_cache` must not grow:
   `.venv/bin/python -c "import sqlite3, pathlib; db = sqlite3.connect(pathlib.Path.home() / 'carousearch/data/carousearch.db'); print(db.execute('select count(*) from price_resolution_cache').fetchone())"`
3. **Price resolution, web.** Restart the web app (section 3.4) so it reads the key, then run the same search in the browser. The results line must not say "price lookup off"; a catalogue post shows the resolved price with the listed one struck through.
4. **`ask`.** Run the first example in README, "Ask the agent", with `--json > data/ask-run-1.json`, then once without `--json` to read the text. Look at: tool calls at most 40 and photos at most 15; every listing seen is in kept or dropped, each with a reason; kept ids and URLs open on Carousell; the photo verdicts for the top 3 match what you see in the photos; the time taken.
5. **Budget refusal.** The same prompt with `--max-photos 1`. The trace must show `[refused]` for the second photo, and the notes must say the photo budget stopped the run.
6. **Optional:** if `OPENAI_API_KEY` exists, repeat step 4 with `--model openai:gpt-5` (the model named in README).
7. **Record.** Measured cost per lookup and per run, durations, and anything wrong: a dated paragraph in ROADMAP.md and updates to sections 1.1 and 1.2 here. Adjust the `AGENT_MAX_*` defaults if the numbers say so.

### 4.5 Phase M (optional): saved searches over MCP (0.5 day, after D)

Add one read-only tool, `saved_alerts`, that returns each saved alert's name, kind, request and prompt, so a host can re-run a search with `search_listings`. It makes no Carousell request. No write tools (creating, pausing or deleting alerts stays in the web app and bot) unless the owner answers Q9 otherwise. `ask` sees the new tool automatically, because it uses the same server, so update `agent/prompt.py` if the agent should use or ignore it.

Acceptance: the tool is listed with the read-only annotation; a test with an in-memory store returns the saved alerts; the agent tests still pass.

### 4.6 Tiered agent tools (done 2026-10-27, branch `feature/tiered-agent-tools`)

**Status.** Built and committed on `feature/tiered-agent-tools` (not pushed). 896 offline tests pass, ruff is clean. Run live four times through `carousearch ask` (2026-10-27).

**What changed.**
- Two model tiers: `AGENT_PLANNER_MODEL` (default `openai:gpt-5.4-mini`) runs the loop, `AGENT_WORKER_MODEL` (default `openai:gpt-5.4-nano`) answers the judge tools and screening's price lookup. `AGENT_MODEL` is a deprecated alias for both. Model building and provider keys live in `language_models.py`, shared by the agent and screening (H11 done).
- Local tools (`agent/local_tools.py`), Pydantic AI function tools that pass through the same `ToolCallGate` as the MCP tools (`GatedToolset`): `run_python` (a subprocess `python -I -S` in the run folder, empty environment, Landlock confinement of files and TCP, `unshare -rn` where available, 20 s / 512 MB / 64 MB file limits, output cut to 4,000 characters), `judge_listings` and `judge_photos` (worker model, 12 listings or 6 photos per structured call, cached in `judgement_cache` by listing, normalised criterion and worker model; details and photos they fetch count against the budgets), and `photo_metrics` (size, Laplacian-variance sharpness, brightness; no model).
- Each run writes `data/runs/<UTC time>-<id>/` with `listings.db` and `listings.json` (every search page and detail the run saw, filled by the gate), `photos/` and `python/` (each snippet and its output).
- MCP server: `category_fields(category=None)` (tree snapshot `carousell/category_tree.json`, 725 categories, plus the live fieldset through the new `CarousellClient.get_category_filters`) and `fields` on `search_listings`. `ListingDetail` gained `category_id` and `category_name`. The agent hides `usage_notes`, whose text is in its instructions. The gate sends the planner only the asked-for fields and the first 300 characters of a description.
- Hosted web search through Pydantic AI's `WebSearch` capability with a dynamic `WebSearchTool` (capped by `AGENT_MAX_WEB_SEARCHES`, default 3, counted from the run's messages; `max_uses` where the provider honours it); prompt caching through the `Caching` capability.
- `AgentAnswer` gained `not_shortlisted` and `usage`; `dropped` is now only shortlisted listings that failed a stated criterion. `ask` prints a count per tool, "Not shortlisted: n" and a cost line (prices in `agent/settings.py`, checked 2026-10-27).

**Live runs** (`AGENT_PLANNER_MODEL=openai:gpt-5.4-mini AGENT_WORKER_MODEL=openai:gpt-5.4-nano`, outputs in `data/ask-live/`):

| Run | Tools | Result | Cost |
|---|---|---|---|
| A, first prompt version | category_fields 1, search 3, run_python 1 | benchmark S$5,766.96 (80% of S$7,208.70 over 27 R15s), 1 kept (S$2,000, 65.2% below); stopped before the COE and photo checks; 19 filtered posts listed as dropped | US$0.038 |
| B, first prompt version | search 1, run_python 2, judge_photos 1 (and usage_notes) | one median (S$15,800) across all bike models; 6 kept, 41 dropped, one contradiction in sign | US$0.039 |
| A, after the prompt fix | search 2, run_python 2, judge_listings 1 (5 details fetched) | benchmark S$5,820.86 (80% of S$7,276.08 over 37 R15s); 5 bikes under S$2,910.43; COE checked on all 5, none about 2 years; nearest kept (S$800, COE Oct 2029) | US$0.026 |
| B, after the prompt fix | category_fields 2, search 2, run_python 2, photo_metrics 1 (10 photos) | still one median (S$9,749.50) across models; kept 5 including one above benchmark and one S$191 placeholder; days listed not computed | US$0.028 |

**Deviations from the brief.** `category_fields` is an MCP tool, not an agent-only tool (it needs no run state, external hosts benefit, and CLAUDE.md keeps tools in `mcp_server.py` where possible). The tiny `--json` live runs were run twice each, once before and once after the prompt fix they prompted. `usage_notes` is filtered out of the agent's tools. The `llm` extra is now empty. Prices in the cost table for Claude Haiku 5.5 come from launch coverage, not Anthropic's page.

**Next (T1 to T4).** T1: planner quality. Since the planning layer (section 4.7) the planner must state a grouped benchmark and the executor must review every plan item, so the remaining question is execution quality; measure it with `carousearch eval --live` (about US$0.4 a pass) and try `AGENT_PLANNER_MODEL=openai:gpt-5.4` (about three times the price) for the execution phase. No item-specific worked examples: the method must stay generic. T2: delete run folders older than N days and expire judgements; T3: accepted, documented; T4: a dev command to recapture the category tree.

### 4.7 Generic planning layer (done 2026-10-27, branch `feature/generic-planning`)

**Status.** Built and committed on `feature/generic-planning` (from `main`, not pushed). 1061 offline tests pass (896 before), ruff is clean. Run live with `openai:gpt-5.4-mini` and `openai:gpt-5.4-nano` (see below). The web app's plan step is tested with scripted models only; the running web app predates it and needs a restart to show it.

**The requirement.** The agent must search any kind of item smartly, with no domain knowledge in code or in the prompts: it works out what drives value, what the listings contain, which attributes the goal needs and where each comes from, how to compare fairly, what to exclude and why, and how to rank. Domain knowledge may only come from the model's general knowledge, web search, the listings it samples, or the owner's `knowledge/` notes.

**What changed.**
- `agent/plan.py`: `SearchPlan` (goal, item class, value drivers, attributes with a source of `structured_filter`, `card_field`, `text_extraction`, `derived` or `external_fact`, benchmark, exclusions with reasons, ranking, template, budget estimate, assumptions), `PlanItemStatus`, `plan_problems` (a grouping naming no attribute, an unknown template, text extraction with no worker calls budgeted) and `complete_plan_review` (every plan item gets a status; one the executor skipped is `not_done`).
- Two phases. `agent/planning.py` `plan_prompt`: the planner with `category_fields`, `search_listings` and the new `sample_descriptions` on `AgentSettings.planning_budget` (`AGENT_PLAN_MAX_TOOL_CALLS`, default 6; no photos; 96 listings; 2 web searches), strict structured output, plan checks as an output validator, and one reminder to sample when a first plan read no listings. `agent/run_prompt.py` `execute_plan`: the plan, its review items and the owner's matching notes in the user message; `run_prompt` does both. The tool gate moved to `agent/toolsets.py`.
- Prompts rewritten as method only (`agent/prompt.py`, both under 70 source lines); worker instructions and tool descriptions lost their item examples. `tests/test_agent_prompt.py` checks every model-facing text (prompts, tool descriptions, worker instructions, templates, MCP descriptions and usage notes) against a list of item and domain words.
- `sample_descriptions` (MCP tool): one search page as description excerpts (cached detail, else the card's ~300-character snippet, now parsed as `Listing.description_snippet`) and structured fields (`ListingDetail.attributes`, the seller-entered category fields, now parsed). Budgeted like a search page; its detail fetches count as tool calls.
- `extract_fields` (local tool): planner-written field specs, regex first, then the worker model in batches of 12 with a strict schema; typed values (unknown stays null), `x_<name>` columns and an `extracted` JSON column in `listings.db`; cached in `judgement_cache` (kind `extraction`) by listing, spec hash, text read and worker model.
- `agent/templates.py`: `under_benchmark`, `best_under_budget`, `neglected_listings`, `compare_variants`, `watch_for` (planning only).
- `agent/knowledge.py` and `knowledge/` (gitignored except its README): notes selected by `applies_to` keywords in the request or the plan's item class, shown labelled as the owner's. `AgentAnswer.suggested_knowledge`; the web app saves it on request, never on its own.
- CLI: `ask --plan-only` (prints the plan, saves `plan.json` in the run folder) and `ask --plan-file`; `carousearch eval [--live] [--only NAME]` over `evals/prompts.json` (seven prompts) with generic plan checks; H2 done.
- Web app: Run plans first and shows the plan with Execute, Edit (one revision round) and Cancel; nothing beyond planning is spent before Execute. The answer shows the plan review and a suggested note with Save to knowledge/. Method templates prefill the prompt box.

**Live runs.**

All with `openai:gpt-5.4-mini` (planner, both phases) and `openai:gpt-5.4-nano` (worker); outputs in `data/generic-planning/` and `data/evals/`. Costs are list-price estimates.

`carousearch eval --live`, one pass (`data/evals/20261027-092942.json`), plus one re-run of the prompt that failed, after the fix it prompted (`data/evals/20261027-094750.json`):

| Prompt | Item class (trimmed) | Attributes by source | Benchmark grouping | Review | Cost |
|---|---|---|---|---|---|
| phones_under_benchmark | used iPhone 15 Pro by storage | card 5, text 1, derived 1 | storage_size | done 10, partial 3 | US$0.061 (re-run; the pass failed: a plan without ranking and budget estimate, three times) |
| office_chair_budget | ergonomic office chair | filter 1, card 6, text 2 | none (best under budget) | done 7, partial 5, not done 3 | US$0.055 |
| neglected_cameras | mirrorless bodies, no kits | card 6, text 1 | condition | done 10, partial 3 | US$0.048 (failed check: listing age filed as card_field, not derived) |
| console_variants | four console families | filter 1, card 3, text 2, derived 1 | variant, condition | done 12, partial 1 | US$0.044 |
| owner_r15_benchmark | used R15s, about 2 years of COE | filter 1, card 3, text 4, derived 1 | generation_variant | done 11, partial 3, not done 1 | US$0.095 (average S$7,205.30 over 27, benchmark S$5,764.24, 2 kept) |
| owner_motorcycles_below_median | used motorcycles by model | filter 1, card 3, text 4, derived 2 | model | done 6, partial 3, not done 7 | US$0.098 (execution spent its 200-listing budget on six search pages and never extracted; 0 kept, honestly reviewed) |
| vague_request | furniture and home items | filter 2, card 2, text 1, derived 1 | item_type | done 10, partial 2 | US$0.059 |

Every plan sampled listings before it was made. Checks: 25 of 26 passed (the re-run counted). Where the planner leaned on its own knowledge instead of the samples: the motorcycles prompt's mileage and registration-year regexes and its `condition_v2` filter, and the cameras prompt's negotiation phrases ("likely on Carousell"); the R15 and chair plans quoted phrasing from the samples.

The R15 prompt through the two-phase CLI (`ask --plan-only`, then `ask --plan-file`), before the date and strict-output fixes: the plan (2 planning calls, US$0.015) had attributes `model_match` (text), `condition` (filter well_used, heavily_used), `price` (card), `coe_remaining` (text), `image_quality_proxy` (card), `listing_effort_proxy` (text), an average benchmark grouped by `model_match`, and five exclusions with reasons. The first execution extracted fields on one mostly irrelevant page, fetched two more without extracting and gave up (review: benchmark `not_done`); the prompt fix that followed made the second execution (US$0.045) compute an average of S$6,424.53 over 15 R15s (benchmark S$5,139.63), find one listing in the 1.5 to 2.5 year COE window at S$7,500, and keep none; its review: 9 done, 3 partial, 1 not done (ranking, nothing to rank). It also showed the worker computing time left without knowing today's date, which led to extracting stated dates instead.

Total spend for the verification: about US$0.63 (four plan-only runs US$0.060, two executions US$0.095, the eval pass US$0.398 plus an unmetered failed planning attempt, the re-run US$0.061).

**Deviations from the brief.** `plan_prompt` returns a `PlanningOutcome` (the plan with its trace, cost, notes and the notes shown), not a bare `SearchPlan`, so the answer can report both phases; `execute_plan` takes that outcome (`PlanningOutcome.from_plan` wraps a saved plan). `sample_descriptions` takes a full `SearchRequest` (query, category and filters) rather than "category or query", and is an MCP tool, so external hosts get it too; the execution phase does not see it. `suggested_knowledge` is plain text whose first line may be `applies_to: ...`; without it the plan's item class becomes the keyword. Existing tests that drove `run_prompt` with execution-only scripts now answer the planning phase with a fixed plan (`tests/scripted_model.PlanFirst`); tests that counted the MCP or local tools, or quoted the old prompt, were updated. Five changes came out of the live runs rather than the brief: the planner is reminded once to sample before planning (its first live plans named regexes from its own knowledge without reading a listing); the executor is told to extract on every page a figure needs and to look into a failing step before stopping short; extraction asks for what the text states (dates) and leaves durations to `run_python`, since the worker does not know today's date; both structured outputs are strict (a live plan arrived without its ranking and budget estimate); and each model request has a 180-second deadline (one planner request went unanswered for minutes). `ask --plan-only` also saves `plan.json` in its run folder. Prompt B's exact wording was not recorded anywhere; `evals/prompts.json` uses a reconstruction from section 4.6.

**Not done or deferred.** Executing alerts (`watch_for` only shapes a plan; phase D). A browser check of the plan step and screenshots. Saving plans with saved prompts (phase D). Cleaning up `data/runs/*` and old extraction answers in `judgement_cache` (T2).

---

## 5. Open questions for the owner

Each has the default that will be assumed if there is no answer.

| # | Question | Default if unanswered |
|---|---|---|
| Q1 | Prompt mode: same page with a Filters / Ask switch, or a separate page? | Same page, with a switch |
| Q2 | Should agent answers in the web app also run carousearch's own screening (tags, price lookup) on the kept listings? | No; show the agent's reasons, plus `screening_tags` results only when the agent called that tool |
| Q3 | Alerts: filter searches only, or prompts re-judged by the agent too? | Both; prompt alerts call the model only when there are new listings, under a smaller budget |
| Q4 | Alert interval and quiet hours? | 60 minutes by default, 30 minimum, no quiet hours, 300 background Carousell requests a day at most |
| Q5 | Which provider key comes first, and the monthly spend ceiling? | Anthropic, model `anthropic:claude-haiku-5-5`; US$5 a month, set as a hard limit in the provider console |
| Q6 | Move the price resolver to Pydantic AI so it is vendor-neutral (H11)? | Done 2026-10-27, on the worker model, as the tiered-agent brief asked |
| Q7 | Resume the analysis plan's phases B to F? If so, on Pydantic AI and the MCP tools instead of the plan's own `llm/` adapter layer? | Paused. When resumed, treat the plan as a spec of behaviour, build the model calls on Pydantic AI, and let the code and CLAUDE.md win where the plan's conventions now differ (the core dependency list has grown; Pillow is a core dependency, not a `vision` extra) |
| Q8 | Is ChatGPT access through a tunnel wanted at all? | No tunnel until H4 is done; stdio hosts only |
| Q9 | Should external hosts be able to create or delete alerts over MCP? | No; phase M is read-only |
| Q10 | Should the web app and agent mark listings as seen (which changes the bot's `[seen]` tags)? | No, as today |
| Q11 | systemd user services or a Windows Task Scheduler task per process? | systemd user services (systemd is already on), plus one Task Scheduler task to start WSL at logon |
| Q12 | Should Run be disabled while an Ask run is in progress, instead of a second Run being refused with a message? | No; keep the refusal (the message shows, and the first run's progress keeps showing under it) |
| Q13 | Should Ask mode show the last finished run's answer after a page reload? | No, until asked (the registry keeps the last 10 runs; the page shows a run in flight, or a plan waiting for a decision) |
| Q14 | Should `carousearch ask` stop for approval of the plan, as the web app does? | No: `ask` plans and executes in one go; use `--plan-only` and `--plan-file` to look first |
| Q15 | Photo checks have no attribute source of their own (the brief's five sources); planners file them under `card_field`. Add a `photo_judgement` source? | No, until the eval shows plans mishandling photos |
| Q16 | Should a saved prompt (phase D) save the agreed plan too, so an alert re-runs the same method without planning again? | Yes, when D is built: store the `SearchPlan` JSON with the prompt |
| Q17 | Which planner model for the execution phase, once `eval --live` gives a baseline? | `openai:gpt-5.4-mini` for both phases until T1 compares `openai:gpt-5.4` on the eval set |

---

## 6. How to work in this repo as a future Claude session

### 6.1 Read in this order

1. `CLAUDE.md` (rules), then this file.
2. `README.md` for the commands and features you will touch.
3. The ROADMAP.md status paragraph for that area (each lists deviations from its brief and what was deferred).
4. `docs/MCP.md` for MCP or agent work; `docs/ANALYSIS_AND_SCREENING_PLAN.md` for market analysis only; `docs/CAROUSELL_ACCESS_NOTES.md` and `docs/FEASIBILITY.md` for Carousell internals and the risk background.
5. The module docstrings of the modules to change, then their tests.

Then check the baseline: `git status`, `git log --oneline -5`, `.venv/bin/pytest -q` (765 passed, 1 deselected, or more if work has landed since), `.venv/bin/ruff check src tests`.

If you are working from GitHub without the owner's machine: there is no `.env`, no `data/` and no running process; do not make live requests; list every owner-only step (section 3.7) in your report instead of attempting it.

### 6.2 Brief a subagent

Implementation goes to an Opus subagent (Agent tool, `model: "opus"`). A brief has these parts:

```
Goal: <one paragraph; what the owner will be able to do>
Read first: CLAUDE.md, docs/NEXT_STEPS.md section <n>, <modules and tests>
Module layout: <new and changed files, one line each>
Data models: <new or changed models, the module that owns each, who consumes them>
Pure vs I/O: <which functions are pure; where I/O happens; what is injected>
Tests: <fakes to use (FakeCarousell, fake_mcp_services, FunctionModel, TestClient, fake bot);
        the cases that must be covered; no network, no real model>
Verify: .venv/bin/pytest -q  -> all pass, count = <before> + new
        .venv/bin/ruff check src tests  -> All checks passed!
Docs: README section <name>; dated ROADMAP status paragraph; NEXT_STEPS updates
Flag, do not work around: <known risks, e.g. an SDK API that may differ from the docs>
Git: small grouped commits, imperative subject under 72 chars, no trailers,
     git -c user.name=moses1998lee -c user.email=moses1998lee@gmail.com commit; do not push
Report back: what was built, files, test count before and after, ruff, deviations,
             anything unverified, open questions
```

### 6.3 Verify the work

- Rerun `pytest` and `ruff` yourself; do not rely on the subagent's report.
- Read the whole diff: names, function size, the pure and I/O split, and no secrets, `data/` or `.env` in it.
- Check that the docs changed with the code.
- Live checks only on the owner's machine, with the owner's approval, and with as few requests as possible.
- Send corrections to the same subagent (SendMessage); do not redo its work.

### 6.4 Commit and push

- Small grouped commits, no trailers, authored as `moses1998lee <moses1998lee@gmail.com>` (CLAUDE.md, "Git rules").
- Push to `origin main` after review, or when the owner asks.

### 6.5 Report back to the owner

Keep it short: what changed and where; the test count before and after; ruff (and pyright once H1 lands); which live checks ran and which did not; deviations from the brief and why; owner-only steps still needed; the next step from section 4.

---

## 7. Glossary

| Term | Meaning |
|---|---|
| Multi-item post | A "catalogue" listing that advertises several variants ("iPhone 15 / 15 Pro / 15 Pro Max all storage") and shows one price, usually the cheapest. `ScreenedListing.is_multi_item` is true when any tag has severity `warn` |
| Resolved price | `ResolvedPrice`: the price for the searched variant. `source` is `listed` (card price kept), `description` (found by the resolver) or `not_stated` (description read, no price for your variant) |
| Screening tags | `Tag` values (`code`, `label`, `reason`, `severity`) from the rules: `MULTI_ITEM_TITLE`, `PRICE_LIST_IN_DESCRIPTION`, `LISTED_PRICE_IS_FLOOR`, `PRICE_BELOW_MARKET`, `PRICE_NOT_FOR_YOUR_ITEM`. Each reason states its evidence |
| Detail budget | At most 10 listing-detail requests per screened page (`--max-detail-fetches`) |
| Budget (agent) | `AgentBudget`: 40 tool calls, 15 photos, 200 listings per `ask` run, enforced by `ToolCallGate`; calls over a cap are refused and never reach Carousell |
| Budget (MCP) | The "about 40 Carousell requests per task" in the usage notes: advice to external hosts, not enforced (H7) |
| Canary | `carousearch canary`: a fixed search plus one detail that proves Carousell access and parsing still work |
| Bootstrap | Loading the Carousell home page once per session for cookies and the CSRF token |
| Session lock (bot) | Each chat has at most one `SearchSession`; while it is active, new searches get a reminder to `/end` it, and only the buttons on its newest message work. Idle sessions expire after 30 minutes |
| `turn` and `McpServices.lock` | The web app's asyncio lock and the MCP server's thread lock; each keeps its process to one Carousell or database call at a time |
| Kept / dropped | The two lists in an `AgentAnswer`; every listing the agent saw is in one of them, with a reason |
| Tool trace | One `ToolCall` per tool call of a run: name, argument summary, duration, status |
| Host | An external agent application that starts or connects to the MCP server: Claude Code, Codex CLI, Claude desktop, ChatGPT |
| Harvest run | One multi-page pull of a search into `listings`, recorded in `harvest_runs`; an identical recent run can be reused |
