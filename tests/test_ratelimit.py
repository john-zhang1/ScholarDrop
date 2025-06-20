from __future__ import annotations

import pytest

from scholardrop.ratelimit import RateLimiter


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def limiter(clock: FakeClock) -> RateLimiter:
    return RateLimiter(attempts=3, window_seconds=60, clock=clock)


def test_allows_up_to_the_limit(limiter: RateLimiter) -> None:
    for _ in range(3):
        assert limiter.allowed("ip") is True
        limiter.record_failure("ip")

    assert limiter.allowed("ip") is False


def test_allowed_does_not_consume(limiter: RateLimiter) -> None:
    for _ in range(10):
        assert limiter.allowed("ip") is True

    assert len(limiter) == 0


def test_keys_are_independent(limiter: RateLimiter) -> None:
    for _ in range(3):
        limiter.record_failure("a")

    assert limiter.allowed("a") is False
    assert limiter.allowed("b") is True


def test_window_expires(limiter: RateLimiter, clock: FakeClock) -> None:
    for _ in range(3):
        limiter.record_failure("ip")
    assert limiter.allowed("ip") is False

    clock.advance(61)

    assert limiter.allowed("ip") is True


def test_window_is_rolling_not_fixed(limiter: RateLimiter, clock: FakeClock) -> None:
    limiter.record_failure("ip")
    clock.advance(30)
    limiter.record_failure("ip")
    limiter.record_failure("ip")
    assert limiter.allowed("ip") is False

    clock.advance(31)  # the first attempt ages out, the later two have not

    assert limiter.allowed("ip") is True
    limiter.record_failure("ip")
    assert limiter.allowed("ip") is False


def test_reset_clears_history(limiter: RateLimiter) -> None:
    for _ in range(3):
        limiter.record_failure("ip")

    limiter.reset("ip")

    assert limiter.allowed("ip") is True
    assert len(limiter) == 0


def test_retry_after_counts_down(limiter: RateLimiter, clock: FakeClock) -> None:
    for _ in range(3):
        limiter.record_failure("ip")
    first = limiter.retry_after("ip")

    clock.advance(30)
    later = limiter.retry_after("ip")

    assert 0 < later < first <= 61


def test_retry_after_is_zero_when_not_limited(limiter: RateLimiter) -> None:
    limiter.record_failure("ip")

    assert limiter.retry_after("ip") == 0


def test_sweep_drops_expired_keys(limiter: RateLimiter, clock: FakeClock) -> None:
    limiter.record_failure("stale")
    clock.advance(61)
    limiter.record_failure("fresh")

    assert limiter.sweep() == 1
    assert len(limiter) == 1
    assert limiter.allowed("stale") is True
