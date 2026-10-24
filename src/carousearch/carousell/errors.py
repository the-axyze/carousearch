"""Exceptions raised when talking to Carousell. All share CarousellError so callers can catch one type."""


class CarousellError(Exception):
    """Base class for every Carousell access failure."""


class CarousellBlockedError(CarousellError):
    """Carousell or Cloudflare refused us: a challenge page, a captcha, or 401/403 after re-bootstrapping."""


class CarousellSchemaError(CarousellError):
    """A response did not have the shape we expect. The message names the missing or invalid key."""


class CarousellHTTPError(CarousellError):
    """An unexpected HTTP status or a network failure."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
