# MCP server

`carousearch mcp` is a small [Model Context Protocol](https://modelcontextprotocol.io) server. It lets an agent host (Claude Code, Codex CLI, the Claude desktop app, or ChatGPT through a tunnel) search Carousell Singapore through carousearch's own client, with its session handling, rate limiter and detail cache.

The server is a connector and nothing more. The host does the reasoning: it writes regexes over the returned JSON, looks at photos, ranks, and plans its own calls. By design the server has no filtering, scoring, LLM calls or planning tools.

The same server also powers `carousearch ask`, carousearch's own agent, which runs it inside its process and adds per-run budgets ([README, "Ask the agent"](../README.md#ask-the-agent)).

| Tool | What it does | Carousell requests |
|---|---|---|
| `search_listings(request, limit=20, page_token=None)` | One page of a search with Carousell's own filters (keywords, price range, condition, meet-up / mailing, Buyer Protection, free delivery, sort, category, category facets). Returns `{listings, total, next_page}`. | 1 per page |
| `listing_details(listing_id)` | Description, all photo URLs, seller rating, reviews, join date, response rate, meet-up points. | 1, or 0 if fetched in the last 12 hours |
| `listing_photo(url, max_width=800)` | One photo from `media.karousell.com` (other domains are refused), scaled down to `max_width` pixels wide. | 1 per photo |
| `screening_tags(listing_id, query=None)` | Optional: carousearch's rule-based tags (multi-item title, price list in the description, floor price). | shares the `listing_details` cache |
| `usage_notes()` | The recommended order, the request budget and the JSON shapes. The same text is sent to the host as the server's instructions when it connects. | 0 |

The usage notes live in [`src/carousearch/mcp_instructions.md`](../src/carousearch/mcp_instructions.md).

## Running it

```bash
.venv/bin/carousearch mcp                                # stdio: what local hosts start themselves
.venv/bin/carousearch mcp --transport http --port 8766   # streamable HTTP at http://127.0.0.1:8766/mcp
```

You rarely start the stdio server by hand: the host launches it as a subprocess and stops it when the session ends. The HTTP server listens on 127.0.0.1 only and runs until Ctrl+C.

- Logs (every Carousell request, tool errors) go to stderr. Hosts keep them in their MCP logs.
- No request is made until a tool is called. The first search or details call also loads the Carousell home page once, for cookies.
- The server has its own Carousell session and rate limiter (1.0 s ± 0.3 s between requests; `--min-interval` and `--jitter` change it, as for every command). It can run next to the bot and the web app.
- `listing_details` uses the 12-hour `listing_detail_cache` in `~/carousearch/data/carousearch.db` (`--data-dir` to change), shared with the CLI, the bot and the web app. Photos are not cached. The server does not record `seen_listings`, so the bot's `[seen]` tags are unaffected.

## Registering it with a host

Use absolute paths: the host starts the server from its own working directory.

### Claude Code

```bash
claude mcp add --scope user carousearch -- /home/moses/carousearch/.venv/bin/carousearch mcp
```

`--scope user` makes it available in every project; leave it out to add it to the current project only. `claude mcp list` shows whether it connects, and `/mcp` inside a session shows its tools. Remove it with `claude mcp remove carousearch --scope user`.

### Codex CLI

Add this to `~/.codex/config.toml`:

```toml
[mcp_servers.carousearch]
command = "/home/moses/carousearch/.venv/bin/carousearch"
args = ["mcp"]
```

or run `codex mcp add carousearch -- /home/moses/carousearch/.venv/bin/carousearch mcp`, which writes the same entry. `codex mcp list` shows it.

### Claude desktop app

In the app, Settings, Developer, Edit Config opens `claude_desktop_config.json` (on Windows `%APPDATA%\Claude\claude_desktop_config.json`; on macOS `~/Library/Application Support/Claude/claude_desktop_config.json`). The app runs on Windows while carousearch lives in WSL, so the command goes through `wsl.exe`:

```json
{
  "mcpServers": {
    "carousearch": {
      "command": "wsl.exe",
      "args": ["-d", "<your distro>", "-u", "moses", "--", "/home/moses/carousearch/.venv/bin/carousearch", "mcp"]
    }
  }
}
```

Replace `<your distro>` with the name `wsl -l` prints (for example `Ubuntu`). On a Mac or Linux machine with carousearch installed locally, use `"command": "/path/to/carousearch/.venv/bin/carousearch"` and `"args": ["mcp"]`. Restart the app after editing the file.

### ChatGPT (HTTP behind a tunnel)

ChatGPT cannot start a local process; it connects to a public HTTPS URL. The shape (not set up yet):

1. Run the server over HTTP: `carousearch mcp --transport http --port 8766`.
2. Expose it with a tunnel, for example a Cloudflare quick tunnel:

   ```bash
   cloudflared tunnel --url http://localhost:8766 --http-host-header localhost:8766
   ```

   It prints a URL such as `https://<random-words>.trycloudflare.com`. `--http-host-header` matters: the MCP SDK protects a server bound to 127.0.0.1 against DNS rebinding by accepting only `localhost` / `127.0.0.1` Host headers, so requests that arrive with the tunnel's hostname would be refused.
3. In ChatGPT, turn on developer mode in the connector settings and add a custom connector with the URL `https://<random-words>.trycloudflare.com/mcp` and no authentication. All tools are marked read-only (`readOnlyHint`), which ChatGPT uses to decide whether a call needs your confirmation.

The server has no authentication: anyone who learns the tunnel URL can run searches through your machine and spend your Carousell budget. Keep the URL private and stop the tunnel (Ctrl+C) when you are done. A permanent setup would want a named Cloudflare Tunnel behind Cloudflare Access, or OAuth on the server, neither of which is built.

## Example prompts

Paste one into a host session with the server registered.

1. > Find road bikes under S$800 with meet-up. Drop any listing whose title lists more than one brand separated by | or /. For the remaining ones, look at the main photo and flag listings whose photo quality is so poor a buyer would skip them. Return a table with price, title, seller, photo verdict and link.

2. > Look for a used iPhone 15 Pro 256GB under S$1,100, sorted by recent, two pages. Leave out catalogue posts (titles naming several models or storage sizes; check doubtful ones with screening_tags) and listings priced under S$500. Fetch details for the 6 cheapest that remain and rank them by seller rating, review count and how long the seller has been on Carousell. Give me the top 3 with links.

3. > What do Nintendo Switch OLED consoles sell for on Carousell right now? Search "nintendo switch oled", sort by recent, three pages of 48. Keep only titles about the console itself (drop games, cases, docks, controllers and "looking for" posts), then give the median, lowest and highest asking price and how many listings you counted. Do not fetch details or photos.

## Etiquette

- Keep one task under about 40 Carousell requests: search pages, details and photos together, plus one home-page load per session.
- The server enforces about one request per second (1.0 s ± 0.3 s), so a long task is slow rather than heavy.
- Search first and filter the JSON yourself, for free; fetch details for a shortlist only; look at photos last.
- If a tool says Carousell is blocking or rate-limiting, stop instead of retrying, and run `carousearch canary` later ([When Carousell changes](../README.md#when-carousell-changes)).
