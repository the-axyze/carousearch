"""The web app's password gate: credentials_match directly, and BasicAuthGate through create_app and TestClient
with fake services. No network."""

import base64
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from carousearch.config import ScreeningSettings, WebAccessSettings
from carousearch.screening.pipeline import ScreeningServices
from carousearch.web.access import credentials_match
from carousearch.web.app import WebSettings, create_app
from carousearch.web.services import WebServices


def basic(username: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()


class NoCarousell:
    def search(self, request):
        raise AssertionError("the gate tests never search")

    def get_listing(self, listing_id):
        raise AssertionError("the gate tests never fetch details")


def open_client(access: WebAccessSettings) -> TestClient:
    services = WebServices(
        carousell=NoCarousell(),
        screening=ScreeningServices(client=NoCarousell(), store=None, resolver=None, clock=lambda: None),
        price_lookup_enabled=False,
    )
    settings = WebSettings(data_dir=Path("unused"), screening=ScreeningSettings(), access=access)
    return TestClient(create_app(settings, services_factory=lambda: services))


# --- credentials_match --------------------------------------------------------------------------------------------


def test_the_right_credentials_match() -> None:
    assert credentials_match(basic("owner", "pw:with:colons"), "owner", "pw:with:colons")


@pytest.mark.parametrize(
    "authorization",
    [
        None,
        "",
        basic("owner", "wrong"),
        basic("someone", "secret"),
        "Bearer " + base64.b64encode(b"owner:secret").decode(),
        "Basic not-base64!!",
        "Basic " + base64.b64encode(b"no-colon").decode(),
        "Basic " + base64.b64encode(b"\xff\xfe:secret").decode(),
    ],
)
def test_anything_else_does_not_match(authorization: str | None) -> None:
    assert not credentials_match(authorization, "owner", "secret")


def test_the_scheme_is_case_insensitive() -> None:
    assert credentials_match(basic("owner", "secret").replace("Basic", "basic"), "owner", "secret")


# --- the gate in the app ------------------------------------------------------------------------------------------


def test_without_a_password_the_app_is_open() -> None:
    with open_client(WebAccessSettings()) as client:
        assert client.get("/").status_code == 200
        assert client.get("/static/app.css").status_code == 200


@pytest.mark.parametrize("path", ["/", "/outreach", "/static/app.css", "/static/htmx.min.js", "/no-such-page"])
def test_with_a_password_every_path_needs_it(path: str) -> None:
    with open_client(WebAccessSettings(password="secret")) as client:
        missing = client.get(path)
        wrong = client.get(path, headers={"Authorization": basic("owner", "nope")})

    for response in (missing, wrong):
        assert response.status_code == 401
        assert response.headers["www-authenticate"].startswith('Basic realm="carousearch"')
        assert "secret" not in response.text


def test_with_the_right_password_pages_and_static_files_load() -> None:
    with open_client(WebAccessSettings(password="secret")) as client:
        headers = {"Authorization": basic("owner", "secret")}
        assert client.get("/", headers=headers).status_code == 200
        assert client.get("/static/app.css", headers=headers).status_code == 200


def test_the_username_comes_from_the_settings() -> None:
    with open_client(WebAccessSettings(password="secret", username="friend")) as client:
        assert client.get("/", headers={"Authorization": basic("owner", "secret")}).status_code == 401
        assert client.get("/", headers={"Authorization": basic("friend", "secret")}).status_code == 200
