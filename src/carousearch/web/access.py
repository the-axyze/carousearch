"""The web app's password gate: HTTP Basic auth over every request (pages, HTMX partials and static files) when
WEB_PASSWORD is set. Without a password the app is open, as it is for local use.

- credentials_match: does an Authorization header carry the expected username and password (pure, constant time).
- BasicAuthGate: the pure-ASGI middleware that answers 401 unless the credentials match.

The password is never logged.
"""

import base64
import binascii
import secrets

from starlette.types import ASGIApp, Receive, Scope, Send

REALM = "carousearch"
UNAUTHORIZED_BODY = b"Password required."


def credentials_match(authorization: str | None, username: str, password: str) -> bool:
    """True when `authorization` is "Basic <base64 user:password>" with exactly these credentials."""
    if not authorization:
        return False
    scheme, _, encoded = authorization.partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        decoded = base64.b64decode(encoded.strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return False
    given_username, separator, given_password = decoded.partition(":")
    if not separator:
        return False
    # Both are compared every time, so the time taken does not say which one was wrong.
    username_ok = secrets.compare_digest(given_username.encode(), username.encode())
    password_ok = secrets.compare_digest(given_password.encode(), password.encode())
    return username_ok and password_ok


class BasicAuthGate:
    """Lets a request through only with the right Basic credentials; lifespan events always pass."""

    def __init__(self, app: ASGIApp, username: str, password: str) -> None:
        self.app = app
        self.username = username
        self.password = password

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        authorization = header_value(scope, b"authorization")
        if credentials_match(authorization, self.username, self.password):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":  # the app has none; refuse any attempt
            await send({"type": "websocket.close", "code": 1008})
            return
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"www-authenticate", f'Basic realm="{REALM}", charset="UTF-8"'.encode()),
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(UNAUTHORIZED_BODY)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": UNAUTHORIZED_BODY})


def header_value(scope: Scope, name: bytes) -> str | None:
    for header_name, value in scope.get("headers", []):
        if header_name.lower() == name:
            return value.decode("latin-1")
    return None
