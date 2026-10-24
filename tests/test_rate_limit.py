import pytest

from carousearch.carousell.rate_limit import RateLimiter, seconds_until_next_request


class FakeClock:
    def __init__(self) -> None:
        self.now = 50.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def make_limiter(clock: FakeClock, jitter: float = 0.0) -> RateLimiter:
    return RateLimiter(1.0, 0.3, clock=clock.time, sleep=clock.sleep, random_uniform=lambda low, high: jitter)


def test_first_request_does_not_wait() -> None:
    clock = FakeClock()

    make_limiter(clock).wait()

    assert clock.sleeps == []


def test_back_to_back_requests_wait_the_interval_plus_jitter() -> None:
    clock = FakeClock()
    limiter = make_limiter(clock, jitter=-0.25)

    limiter.wait()
    limiter.wait()
    limiter.wait()

    assert clock.sleeps == pytest.approx([0.75, 0.75])


def test_time_already_elapsed_counts_towards_the_interval() -> None:
    clock = FakeClock()
    limiter = make_limiter(clock)

    limiter.wait()
    clock.now += 0.6
    limiter.wait()
    clock.now += 5.0
    limiter.wait()

    assert clock.sleeps == pytest.approx([0.4])


def test_seconds_until_next_request() -> None:
    assert seconds_until_next_request(last_request_at=10.0, now=10.5, interval_seconds=1.0) == 0.5
    assert seconds_until_next_request(last_request_at=10.0, now=12.0, interval_seconds=1.0) == 0.0


def test_negative_settings_are_rejected() -> None:
    with pytest.raises(ValueError):
        RateLimiter(min_interval_seconds=-1)
