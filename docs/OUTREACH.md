# Outreach: offer messages to a run's sellers

Outreach turns an Ask run's shortlist into chat messages from your own Carousell account. For each listing the run kept, the worker model drafts one short offer (a set percentage below the asking price, rounded down) that names one detail of that listing. You review the whole batch once, untick or edit anything, and press one button; the messages then go out one at a time, spaced apart, under a daily cap, and no listing or seller is ever messaged twice across runs.

The aim is to send a low offer to many sellers and judge, from their replies, which ones will negotiate. Reading the replies is the next phase (see the end of this file); the data it needs is already kept.

## How it works

1. A finished Ask run saves its answer as `answer.json` in its run folder (`data/runs/<run id>/`). Its kept listings, with what `listings.db` holds about each (title, price, seller, description, seller-entered fields, extracted values), are the shortlist. A run made before answers were saved has no `answer.json`; outreach then uses every listing that run saw, those with details first, and says so.
2. **Draft.** One worker-model call per listing (`OUTREACH_DRAFT_MODEL`, by default the worker model, `openai:gpt-5.4-nano`). The drafter's instructions are generic (no item words; a test checks them with the agent's prompts) and ask for 2 to 4 sentences of casual Singapore English that mention one concrete detail, state the offer plainly and ask whether the seller is open to it. A draft without the offer price, or with an email address, contact number or link, is sent back to the model; one still wrong is passed over. Listings or sellers messaged before, and a second listing from the same seller, are passed over before any model call. Drafting again replaces the run's unsent drafts.
3. **Review.** The web app shows a table: a tick box (on by default), the listing link, asking price, offer and an editable message for each draft. A draft that a send would skip right now (the daily cap, or contacted since) starts unticked with the reason. The CLI prints the same as a table.
4. **Send.** One press (web: "Send N messages"; CLI: `outreach send`). The batch is checked against the policy, the browser profile is checked once for a logged-in session, and then each message is sent in turn: open the listing page, press **Chat**, type the message, press **Send**, look for the message in the thread. Between two sends it waits a random 30 to 60 seconds. The first failure stops the batch; what was not attempted stays approved for a later batch. A message whose send was pressed but which was not seen afterwards is recorded as sent ("unconfirmed"), so it is never sent twice.
5. Every message, drafted, skipped, sent or failed, stays in `data/outreach.db`, with the chat's address once sent.

The offer: asking price less the percentage, rounded down to a multiple of `--round-to` (default 50). When that step is more than a tenth of the asking price, the largest of 10, 5 or 1 that is not is used instead, so S$120 at 25 percent becomes S$90 rather than S$50. For example, S$830 at 25 percent is S$600; S$45 is S$33.

## Setup, once

```bash
uv pip install --python .venv/bin/python -e ".[dev,llm,outreach]"   # adds Playwright
.venv/bin/playwright install chromium                                # the browser (about 700 MB, in ~/.cache/ms-playwright)
.venv/bin/carousearch account login
```

`account login` opens a Chromium window on Carousell's login page, using its own profile in `data/browser-profile/` (gitignored, separate from everything else; searches stay anonymous as before). **Log in there by hand**: type your details, answer any code or check Carousell shows. The command only watches the page (every 3 seconds, without touching it) and stops when it sees you logged in, or after 5 minutes. The profile keeps the session, so this is needed again only when Carousell logs you out.

Carousearch never stores, reads or types your Carousell password, never logs in for you, and never answers a captcha or code. If Carousell shows a challenge or block page, outreach reports it and stops; it does not retry. A fresh profile's very first visit can meet Cloudflare's "Just a moment..." page: run `account login` and let it finish in the window by hand.

If Chromium will not start for lack of system libraries, run `sudo .venv/bin/playwright install-deps chromium` (it needs sudo, so only the owner can).

## Using it

Web app: run an Ask search to the end. Under the answer, **Message sellers** has the offer form (percent below, round to, an optional note for the drafter such as "I can collect this weekend"). **Draft messages** shows the drafts; tick, edit, then **Send N messages**. Tick **Dry run** first to see exactly what would happen without sending. Progress shows below and updates every few seconds; **Stop after the current message** ends a batch early. **/outreach** lists every message, newest first, with its status, listing link, chat link and run.

Public demo: when the web app runs with `--public` or `WEB_PUBLIC_DEMO=1` (for showing it through a tunnel; README, "Showing it on a phone"), every send is a dry run. The service enforces it whatever the form says, the Dry run toggle is replaced by a note, and every page says "Public demo: messages are not sent". Drafting still works, and the account login stays CLI-only.

CLI:

```bash
.venv/bin/carousearch outreach draft <run id> --below 25 [--round-to 50] [--limit N] [--note "..."]
.venv/bin/carousearch outreach send <run id> --all --dry-run       # the policy in action; nothing sent or recorded
.venv/bin/carousearch outreach send <run id> --ids 12 14           # send only these drafts
.venv/bin/carousearch outreach send <run id> --all                 # send every unsent draft of the run
.venv/bin/carousearch outreach list [--run <run id>] [--status sent]
.venv/bin/carousearch account status                               # exit 0 only when logged in
```

The run id is the run folder's name under `data/runs/`, e.g. `20261027-092958-f1ec53`.

## The policy, and why

| Setting | Default | What it does |
|---|---|---|
| `OUTREACH_DAILY_CAP` | 10 | New conversations in any 24 hours, across all runs |
| `OUTREACH_MIN_GAP_SECONDS`, `OUTREACH_MAX_GAP_SECONDS` | 30, 60 | The wait between two sends, drawn evenly between the two |
| `OUTREACH_DRAFT_MODEL` | blank: the worker model | The model that drafts |
| `OUTREACH_PROFILE_DIR` | blank: `data/browser-profile` | The logged-in browser profile |

- **The daily cap and the de-duplication protect your account.** Many new conversations a day, or a second message to a seller who did not answer the first, is what gets an account reported or restricted. A listing or seller (by seller id, else username) that has a sent message is never sent another, by any run.
- **The gap is politeness, not protection.** It keeps sends at a person's pace. It is not meant to disguise anything: nothing imitates typing or mouse movement, and nothing is spoofed.
- **Each message is drafted for its listing.** No fixed text goes out verbatim to many sellers, and messages carry no contact details and nothing about this tool.
- **You approve every batch.** Nothing is sent without the Send press (web) or `outreach send` (CLI). Development and tests use a dry-run transport that sends nothing.

Carousell's terms ban automated access; sending from your account through a browser is on the same risk terms as the rest of carousearch (home IP, low volume, stop on any block).

## Verifying the selectors after logging in

The page selectors live in `src/carousearch/outreach/selectors.py`, the only lines expected to change when Carousell changes its pages. Some were checked on the public pages (2026-10-30, logged out); the rest can only be checked logged in:

| Selector | Checked | What it finds |
|---|---|---|
| `LOGGED_OUT_INDICATOR` | yes | the header's Login link |
| `CHAT_BUTTON` | yes | the listing page's **Chat** button, beside **Make Offer**; logged out it opens a Login dialog (`LOGIN_DIALOG`, also checked) |
| `LOGGED_IN_INDICATOR`, `USERNAME_LINK` | no: guesses | a header link to your profile (`/u/<username>/`) or the inbox |
| `COMPOSER`, `SEND_BUTTON` | no: guesses | the chat's message box and its **Send** button |

In order, after `account login`:

1. `carousearch account status` should say "Logged in as <you>." If it says the indicators were not found, open the profile (`account login` again shows the window), find an element only a logged-in page has (your avatar or profile link), and put a selector for it in `LOGGED_IN_INDICATOR` (and `USERNAME_LINK` if it links to `/u/<username>/`).
2. `carousearch outreach send <run id> --all --dry-run` shows the policy on real drafts without touching the browser.
3. One real send to a single listing you choose: `carousearch outreach draft <run id> --below 25 --limit 1`, read the draft, then `carousearch outreach send <run id> --ids <id>`, and watch the window. If it stops with "COMPOSER not found" or "SEND_BUTTON not found", the message was not sent; find the element in the chat page (right-click, Inspect) and update that selector. Check the message in your Carousell inbox, then `carousearch outreach list` should show it as sent with its chat link.

## Next: reading replies (phase 2)

Not built yet. Each row of `outreach_messages` already has empty `reply_text`, `reply_at`, `reply_class` and `counter_price` columns. Phase 2 would open each sent conversation's `chat_url` in the same profile (read only, under the same pacing and block rules), store the seller's first reply, have the worker model classify it (accepts, counter-offer with a price, firm, no reply) and report negotiability per seller and per kind of listing, so the next round's percentage can follow the evidence.
