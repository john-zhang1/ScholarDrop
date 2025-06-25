"""The cached repository configuration (submission/definition.py).

It is shared by every user of an instance and only changes when DSpace is
reconfigured -- but it does change, so entries expire rather than living for the
life of the process.
"""

from __future__ import annotations

from scholardrop.submission.definition import ConfigCache


class FakeClock:
    """Monotonic clock we can advance by hand, so TTL tests don't sleep."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def cache_at(clock: FakeClock, ttl_seconds: float = 100) -> ConfigCache:
    cache = ConfigCache(ttl_seconds=ttl_seconds)
    for store in cache._all():
        store._clock = clock
    return cache


def test_cached_configuration_goes_stale_and_is_refetched() -> None:
    """Repository config only changes when DSpace is reconfigured, but it does
    change -- without a TTL a stale form was served until someone restarted us."""
    clock = FakeClock()
    cache = cache_at(clock)

    cache.forms[("shareok", "traditionalpageone")] = {"rows": ["old"]}
    assert cache.forms.get(("shareok", "traditionalpageone")) == {"rows": ["old"]}
    assert ("shareok", "traditionalpageone") in cache.forms

    clock.advance(101)

    assert cache.forms.get(("shareok", "traditionalpageone")) is None
    assert ("shareok", "traditionalpageone") not in cache.forms


def test_the_cache_does_not_grow_without_bound() -> None:
    clock = FakeClock()
    cache = cache_at(clock)

    for n in range(50):
        cache.collection_names[("shareok", f"collection-{n}")] = f"Collection {n}"
    assert len(cache) == 50

    clock.advance(101)

    assert cache.sweep() == 50
    assert len(cache) == 0


def test_a_licence_cached_as_none_is_still_a_cache_hit() -> None:
    """An unreadable licence is remembered as None so it is not retried per request;
    `in` is what distinguishes that from a miss."""
    cache = ConfigCache(ttl_seconds=100)
    cache.licences[("shareok", "c1")] = None

    assert ("shareok", "c1") in cache.licences
    assert cache.licences[("shareok", "c1")] is None
