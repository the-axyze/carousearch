"""The browser side of outreach: a chat transport that sends a message from the owner's logged-in Carousell
account, and a dry-run transport that sends nothing.

- ChatTransport (Protocol): status() and send(listing_id, body). The sender only knows this.
- PlaywrightChatTransport: Playwright's sync API over a persistent Chromium profile (data/browser-profile/) that
  the owner logged into by hand (`carousearch account login`). It never types credentials, never logs in, and
  never works around a challenge: a challenge, a block or a missing element ends the call with a failure that
  names the selector (outreach/selectors.py), and the sender stops the batch. Playwright objects belong to the
  thread that created them, so one transport is used from one thread.
- DryRunChatTransport: records calls and answers success without any network; tests and --dry-run use it.

Pure: classify_session, username_from_link, confirmation_snippet. I/O: PlaywrightChatTransport.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from carousearch.outreach import selectors
from carousearch.outreach.models import SendOutcome, SessionStatus
from carousearch.outreach.selectors import Target

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Locator, Page, Playwright

DEFAULT_TIMEOUT_MS = 15_000
PAGE_SETTLE_MS = 3_000  # Carousell renders its header and buttons after the first paint

NOT_LOGGED_IN_DETAIL = "Not logged in. Run `carousearch account login` and log in by hand in the window it opens."
BLOCKED_DETAIL = (
    "Carousell showed a challenge or block page (e.g. Cloudflare's \"Just a moment...\"); nothing was done. Open the "
    "profile by hand with `carousearch account login` and let the page finish; if it stays, stop for the day."
)
INSTALL_HINT = (
    "Playwright is not installed. Run: uv pip install --python .venv/bin/python -e \".[outreach]\" and then "
    ".venv/bin/playwright install chromium"
)
UNKNOWN_SESSION_DETAIL = (
    "Neither the Login link nor the logged-in indicator was found; the selectors in outreach/selectors.py may need "
    "updating (docs/OUTREACH.md)."
)


class BrowserError(RuntimeError):
    """The browser could not start, or its window went away."""


class ChatTransport(Protocol):
    def status(self) -> SessionStatus: ...

    def send(self, listing_id: str, body: str) -> SendOutcome: ...

    def close(self) -> None: ...


# --- reading a page (pure) ----------------------------------------------------------------------------------------


def classify_session(
    title: str, challenge_found: bool, logged_out_found: bool, logged_in_found: bool, username: str | None
) -> SessionStatus:
    """The session state from what the home page shows. Unsure counts as not logged in."""
    if challenge_found or title.strip().startswith(selectors.CHALLENGE_TITLE_PREFIXES):
        return SessionStatus(logged_in=False, blocked=True, detail=BLOCKED_DETAIL)
    if logged_out_found:
        return SessionStatus(logged_in=False, detail=NOT_LOGGED_IN_DETAIL)
    if logged_in_found:
        detail = f"Logged in as {username}." if username else "Logged in (username not found on the page)."
        return SessionStatus(logged_in=True, username=username, detail=detail)
    return SessionStatus(logged_in=False, detail=UNKNOWN_SESSION_DETAIL)


def username_from_link(href: str | None) -> str | None:
    """'/u/some_name/' -> 'some_name'."""
    if not href or not href.startswith(selectors.USERNAME_LINK_PREFIX):
        return None
    name = href.removeprefix(selectors.USERNAME_LINK_PREFIX).strip("/").split("/")[0]
    return name or None


def confirmation_snippet(body: str) -> str:
    """The start of the message, cut at a word boundary, to look for in the chat thread."""
    first_line = body.strip().splitlines()[0] if body.strip() else ""
    if len(first_line) <= selectors.CONFIRMATION_SNIPPET_CHARACTERS:
        return first_line
    return first_line[: selectors.CONFIRMATION_SNIPPET_CHARACTERS].rsplit(" ", 1)[0]


def browser_errors() -> tuple[type[Exception], ...]:
    """The exceptions a browser call can raise: BrowserError, and Playwright's own when it is installed."""
    try:
        from playwright.sync_api import Error as PlaywrightError
    except ImportError:
        return (BrowserError,)
    return (BrowserError, PlaywrightError)


# --- the dry run (no I/O) -----------------------------------------------------------------------------------------


@dataclass
class DryRunChatTransport:
    """Sends nothing: answers `session` to status() and success to send(), and remembers each call."""

    session: SessionStatus = field(
        default_factory=lambda: SessionStatus(logged_in=True, username="dry-run", detail="Dry run: nothing is sent.")
    )
    fail_on_listing_ids: frozenset[str] = frozenset()  # tests: these sends fail
    status_checks: int = 0
    sends: list[tuple[str, str]] = field(default_factory=list)  # (listing id, body)

    def status(self) -> SessionStatus:
        self.status_checks += 1
        return self.session

    def send(self, listing_id: str, body: str) -> SendOutcome:
        self.sends.append((listing_id, body))
        if listing_id in self.fail_on_listing_ids:
            return SendOutcome("failed", error="dry-run failure", failed_selector=selectors.CHAT_BUTTON.name)
        return SendOutcome("sent", chat_url=f"dry-run://chat/{listing_id}")

    def close(self) -> None:
        pass


# --- the real browser (I/O) ---------------------------------------------------------------------------------------


class PlaywrightChatTransport:
    """A persistent Chromium profile on carousell.sg. Started on first use; close() ends it."""

    def __init__(self, profile_dir: Path, headed: bool, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> None:
        self._profile_dir = profile_dir
        self._headed = headed
        self._timeout_ms = timeout_ms
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None

    # --- session ---

    def status(self) -> SessionStatus:
        """Open the home page and read the session state from it. A browser that cannot start is reported as
        not logged in, with the reason."""
        try:
            page = self._page()
            page.goto(selectors.HOME_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(PAGE_SETTLE_MS)
            return self.current_status()
        except browser_errors() as error:
            return SessionStatus(logged_in=False, detail=f"The browser could not check: {first_line(str(error))}")

    def current_status(self) -> SessionStatus:
        """The session state of the page as it is now, without navigating (the login poll uses this). Raises
        BrowserError when the window was closed."""
        try:
            page = self._page()
            username_link = self._find(page, selectors.USERNAME_LINK)
            username_href = username_link.first.get_attribute("href") if username_link.count() else None
            return classify_session(
                title=page.title(),
                challenge_found=self._visible(page, selectors.CHALLENGE_INDICATOR),
                logged_out_found=self._visible(page, selectors.LOGGED_OUT_INDICATOR),
                logged_in_found=self._visible(page, selectors.LOGGED_IN_INDICATOR),
                username=username_from_link(username_href),
            )
        except browser_errors() as error:
            raise BrowserError(first_line(str(error))) from error

    def open_login_page(self) -> None:
        """Show Carousell's login page; the owner logs in by hand. Raises BrowserError if the browser fails."""
        try:
            self._page().goto(selectors.LOGIN_URL, wait_until="domcontentloaded")
        except browser_errors() as error:
            raise BrowserError(first_line(str(error))) from error

    # --- sending ---

    def send(self, listing_id: str, body: str) -> SendOutcome:
        """Open the listing, press Chat, type the message, press Send, and look for it in the thread."""
        try:
            page = self._page()
            page.goto(selectors.LISTING_PAGE_URL_TEMPLATE.format(listing_id=listing_id), wait_until="domcontentloaded")
            page.wait_for_timeout(PAGE_SETTLE_MS)
            if self._blocked(page):
                return SendOutcome("failed", error=BLOCKED_DETAIL)
            chat_button = self._wait_for(page, selectors.CHAT_BUTTON)
            if chat_button is None:
                return missing(selectors.CHAT_BUTTON)
            chat_button.click()
            page.wait_for_timeout(PAGE_SETTLE_MS)
            page = self._page()  # the newest tab, in case Chat opened the conversation in a new one
            composer = self._wait_for(page, selectors.COMPOSER)
            if composer is None:
                if self._visible(page, selectors.LOGIN_DIALOG):
                    return SendOutcome("failed", error=NOT_LOGGED_IN_DETAIL, failed_selector=selectors.LOGIN_DIALOG.name)
                return missing(selectors.COMPOSER)
            composer.fill(body)
            send_button = self._wait_for(page, selectors.SEND_BUTTON)
            if send_button is None:
                return missing(selectors.SEND_BUTTON)
            send_button.click()
        except browser_errors() as error:
            return SendOutcome("failed", error=f"browser error before sending: {first_line(str(error))}")
        return self._confirm(page, body)

    def _confirm(self, page: "Page", body: str) -> SendOutcome:
        """Send was pressed: from here on the message may have gone out, so a failure is "unconfirmed"."""
        try:
            page.get_by_text(confirmation_snippet(body), exact=False).last.wait_for(timeout=self._timeout_ms)
        except browser_errors() as error:
            return SendOutcome(
                "unconfirmed", chat_url=page.url, error=f"message not seen in the thread: {first_line(str(error))}"
            )
        return SendOutcome("sent", chat_url=page.url)

    # --- plumbing ---

    def close(self) -> None:
        """End the browser; quietly, since the owner may have closed its window already."""
        context, playwright = self._context, self._playwright
        self._context, self._playwright = None, None
        try:
            if context is not None:
                context.close()
        except browser_errors():
            pass
        finally:
            if playwright is not None:
                playwright.stop()

    def _page(self) -> "Page":
        if self._context is None:
            self._context = self._start()
        pages = self._context.pages
        return pages[-1] if pages else self._context.new_page()

    def _start(self) -> "BrowserContext":
        """Launch Chromium on the persistent profile; BrowserError when Playwright or Chromium is missing."""
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import sync_playwright
        except ImportError as error:
            raise BrowserError(INSTALL_HINT) from error
        self._profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        try:
            context = self._playwright.chromium.launch_persistent_context(
                str(self._profile_dir), headless=not self._headed, locale="en-SG"
            )
        except PlaywrightError as error:
            self.close()
            raise BrowserError(f"Chromium could not start: {first_line(str(error))}") from error
        context.set_default_timeout(self._timeout_ms)
        return context

    def _blocked(self, page: "Page") -> bool:
        title = page.title().strip()
        return title.startswith(selectors.CHALLENGE_TITLE_PREFIXES) or self._visible(page, selectors.CHALLENGE_INDICATOR)

    def _find(self, page: "Page", target: Target) -> "Locator":
        if target.role is not None:
            return page.get_by_role(target.role, name=target.accessible_name, exact=True)  # type: ignore[arg-type]
        assert target.css is not None, f"{target.name} has neither a role nor a CSS selector"
        return page.locator(target.css)

    def _visible(self, page: "Page", target: Target) -> bool:
        locator = self._find(page, target)
        return any(locator.nth(index).is_visible() for index in range(locator.count()))

    def _wait_for(self, page: "Page", target: Target) -> "Locator | None":
        """The first visible match, waiting up to the timeout; None when there is none."""
        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        locator = self._find(page, target).first
        try:
            locator.wait_for(state="visible", timeout=self._timeout_ms)
        except PlaywrightTimeout:
            return None
        return locator


def missing(target: Target) -> SendOutcome:
    return SendOutcome(
        "failed", error=f"{target.name} not found on the page (outreach/selectors.py)", failed_selector=target.name
    )


def first_line(text: str) -> str:
    return text.strip().splitlines()[0] if text.strip() else text
