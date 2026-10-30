"""Every carousell.sg address and page selector outreach uses, in one place.

When Carousell changes its pages, these are the only lines expected to change. Carousell's CSS class names are
generated (e.g. "D_ql M_pR"), so targets use roles, visible names and link addresses instead.

Checked 2026-10-30 on the public pages while logged out (docs/OUTREACH.md, "Verifying the selectors"):
- the header shows "Login" (a[href="/login/"]) and "Register" links;
- a listing page has a button named "Chat" beside "Make Offer"; logged out, clicking it opens a dialog titled
  "Login" (QR code, Facebook, email/username/mobile);
- a headless browser got Cloudflare's "Just a moment..." page instead of the site; a headed one did not.

NOT verified, because they appear only when logged in: LOGGED_IN_INDICATOR, USERNAME_LINK, COMPOSER, SEND_BUTTON
and the inbox address. `carousearch account status` and one `outreach send --dry-run` after the owner's first
login check the first two; the first real send checks the rest.
"""

from dataclasses import dataclass

from carousearch.carousell.parse import LISTING_URL_TEMPLATE

HOME_URL = "https://www.carousell.sg/"
LOGIN_URL = "https://www.carousell.sg/login/"
# A listing's page; Carousell redirects it to the slugged address, e.g. /p/<title-slug>-<id>/.
LISTING_PAGE_URL_TEMPLATE = LISTING_URL_TEMPLATE


@dataclass(frozen=True)
class Target:
    """An element to find: by ARIA role and accessible name, or by CSS selector."""

    name: str  # for error messages: which line of this file to fix
    css: str | None = None
    role: str | None = None
    accessible_name: str | None = None


# --- session state ------------------------------------------------------------------------------------------------

# Verified logged out: the header's Login link.
LOGGED_OUT_INDICATOR = Target("LOGGED_OUT_INDICATOR", css='a[href="/login/"]')
# Unverified guess: a logged-in header links to the owner's profile (/u/<username>/) and to the inbox.
LOGGED_IN_INDICATOR = Target("LOGGED_IN_INDICATOR", css='header a[href^="/u/"], a[href^="/inbox"]')
# Unverified guess: the profile link, whose address ends in the username.
USERNAME_LINK = Target("USERNAME_LINK", css='header a[href^="/u/"]')
USERNAME_LINK_PREFIX = "/u/"

# A challenge or block: Cloudflare's interstitial (seen headless), or a captcha frame.
CHALLENGE_TITLE_PREFIXES = ("Just a moment", "Attention Required", "Access denied")
CHALLENGE_INDICATOR = Target(
    "CHALLENGE_INDICATOR",
    css='iframe[src*="challenges.cloudflare.com"], #challenge-form, iframe[title*="captcha" i], iframe[src*="captcha"]',
)

# --- sending a chat -----------------------------------------------------------------------------------------------

# Verified logged out: the listing page's Chat button.
CHAT_BUTTON = Target("CHAT_BUTTON", role="button", accessible_name="Chat")
# Verified logged out: what the Chat button opens instead of a chat.
LOGIN_DIALOG = Target("LOGIN_DIALOG", css='[role="dialog"]:has-text("Login")')
# Unverified guess: the chat's message box and its send button.
COMPOSER = Target("COMPOSER", css='textarea, [contenteditable="true"][role="textbox"]')
SEND_BUTTON = Target("SEND_BUTTON", role="button", accessible_name="Send")

# The start of the sent message, looked for in the thread to confirm it went out.
CONFIRMATION_SNIPPET_CHARACTERS = 40
