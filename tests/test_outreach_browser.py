"""The browser module's pure page reading and the dry-run transport. No browser is started."""

import pytest

from carousearch.outreach import selectors
from carousearch.outreach.browser import (
    BLOCKED_DETAIL,
    NOT_LOGGED_IN_DETAIL,
    UNKNOWN_SESSION_DETAIL,
    DryRunChatTransport,
    PlaywrightChatTransport,
    classify_session,
    confirmation_snippet,
    username_from_link,
)
from carousearch.outreach.models import SessionStatus


def test_a_challenge_page_is_a_block_whatever_else_shows() -> None:
    by_title = classify_session("Just a moment...", False, False, True, "me")
    by_frame = classify_session("Carousell Singapore", True, False, True, "me")

    assert by_title == by_frame == SessionStatus(logged_in=False, blocked=True, detail=BLOCKED_DETAIL)


def test_the_login_link_means_logged_out() -> None:
    status = classify_session("Carousell Singapore", False, True, False, None)

    assert status == SessionStatus(logged_in=False, detail=NOT_LOGGED_IN_DETAIL)


def test_the_logged_in_indicator_means_logged_in() -> None:
    assert classify_session("Carousell", False, False, True, "tan_ah_kow") == SessionStatus(
        logged_in=True, username="tan_ah_kow", detail="Logged in as tan_ah_kow."
    )
    assert classify_session("Carousell", False, False, True, None).logged_in


def test_neither_indicator_counts_as_not_logged_in_and_points_at_the_selectors() -> None:
    status = classify_session("Carousell", False, False, False, None)

    assert not status.logged_in and not status.blocked and status.detail == UNKNOWN_SESSION_DETAIL


@pytest.mark.parametrize(
    ("href", "username"),
    [("/u/tan_ah_kow/", "tan_ah_kow"), ("/u/someone/reviews/", "someone"), ("/inbox/", None), (None, None),
     ("/u/", None)],
)
def test_username_from_the_profile_link(href: str | None, username: str | None) -> None:
    assert username_from_link(href) == username


def test_the_confirmation_snippet_is_the_start_of_the_first_line_cut_at_a_word() -> None:
    assert confirmation_snippet("Hi there, $90 ok?") == "Hi there, $90 ok?"
    long_body = "Hi! Saw your table with the small dent on the top, would you take $90?"
    snippet = confirmation_snippet(long_body)
    assert long_body.startswith(snippet) and len(snippet) <= 40 and not snippet.endswith(" ")
    assert confirmation_snippet("First line\nSecond line") == "First line"


def test_the_dry_run_transport_sends_nothing_and_remembers_calls() -> None:
    transport = DryRunChatTransport(fail_on_listing_ids=frozenset({"2"}))

    assert transport.status().logged_in and transport.status_checks == 1
    assert transport.send("1", "hello").chat_url == "dry-run://chat/1"
    failed = transport.send("2", "hello")
    assert failed.result == "failed" and failed.failed_selector == selectors.CHAT_BUTTON.name
    assert transport.sends == [("1", "hello"), ("2", "hello")]


def test_the_real_transport_starts_no_browser_until_used(tmp_path) -> None:
    transport = PlaywrightChatTransport(tmp_path / "profile", headed=False)

    transport.close()  # nothing was started, so nothing to stop

    assert not (tmp_path / "profile").exists()


def test_every_target_has_a_way_to_be_found() -> None:
    targets = [value for value in vars(selectors).values() if isinstance(value, selectors.Target)]

    assert len(targets) >= 8
    for target in targets:
        assert target.css or (target.role and target.accessible_name), target.name


def test_the_listing_page_address_is_the_clients() -> None:
    assert selectors.LISTING_PAGE_URL_TEMPLATE.format(listing_id="123") == "https://www.carousell.sg/p/123/"
