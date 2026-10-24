"""Polite pacing between Carousell requests: a minimum interval with random jitter."""

import random
import time
from collections.abc import Callable

DEFAULT_MIN_INTERVAL_SECONDS = 1.0
DEFAULT_JITTER_SECONDS = 0.3


class RateLimiter:
    """Blocks in `wait()` until at least `min_interval_seconds` (plus or minus jitter) has
    passed since the previous request. Clock, sleep and randomness are injected for tests."""

    def __init__(
        self,
        min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS,
        jitter_seconds: float = DEFAULT_JITTER_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        random_uniform: Callable[[float, float], float] = random.uniform,
    ) -> None:
        if jitter_seconds < 0 or min_interval_seconds < 0:
            raise ValueError("interval and jitter must not be negative")
        self._min_interval_seconds = min_interval_seconds
        self._jitter_seconds = jitter_seconds
        self._clock = clock
        self._sleep = sleep
        self._random_uniform = random_uniform
        self._last_request_at: float | None = None

    def wait(self) -> None:
        """Call right before sending a request."""
        if self._last_request_at is not None:
            interval = self._min_interval_seconds + self._random_uniform(-self._jitter_seconds, self._jitter_seconds)
            delay = seconds_until_next_request(self._last_request_at, self._clock(), interval)
            if delay > 0:
                self._sleep(delay)
        self._last_request_at = self._clock()


def seconds_until_next_request(last_request_at: float, now: float, interval_seconds: float) -> float:
    return max(0.0, last_request_at + interval_seconds - now)
