from __future__ import annotations

import pytest

from scholardrop.session import InMemorySessionStore, SessionCookie


class FakeClock:
    """Monotonic clock we can advance by hand, so TTL tests don't sleep."""

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
def store(clock: FakeClock) -> InMemorySessionStore:
    return InMemorySessionStore(ttl_seconds=60, clock=clock)


def test_create_and_get(store: InMemorySessionStore) -> None:
    session = store.create(instance_key="shareok")

    assert len(session.id) >= 32
    assert session.instance_key == "shareok"
    assert not session.authenticated
    assert store.get(session.id) is session
    assert len(store) == 1


def test_ids_are_unique(store: InMemorySessionStore) -> None:
    ids = {store.create().id for _ in range(50)}
    assert len(ids) == 50


def test_get_unknown_id_returns_none(store: InMemorySessionStore) -> None:
    assert store.get("not-a-session") is None


def test_authenticated_flips_once_jwt_is_set(store: InMemorySessionStore) -> None:
    session = store.create()
    session.jwt = "header.payload.signature"
    store.save(session)

    assert store.get(session.id).authenticated is True


def test_expires_after_ttl(store: InMemorySessionStore, clock: FakeClock) -> None:
    session = store.create()

    clock.advance(59)
    assert store.get(session.id, touch=False) is not None

    clock.advance(1)  # exactly at the TTL boundary
    assert store.get(session.id) is None
    assert len(store) == 0  # expired session is dropped on read, not just hidden


def test_get_refreshes_idle_timeout(store: InMemorySessionStore, clock: FakeClock) -> None:
    session = store.create()

    for _ in range(5):
        clock.advance(50)
        assert store.get(session.id) is not None, "active session should not expire"

    clock.advance(61)
    assert store.get(session.id) is None


def test_get_without_touch_does_not_extend(store: InMemorySessionStore, clock: FakeClock) -> None:
    session = store.create()
    clock.advance(50)
    store.get(session.id, touch=False)
    clock.advance(11)

    assert store.get(session.id) is None


def test_sweep_removes_only_expired(store: InMemorySessionStore, clock: FakeClock) -> None:
    stale = store.create()
    clock.advance(61)
    fresh = store.create()

    assert store.sweep() == 1
    assert len(store) == 1
    assert store.get(fresh.id) is not None
    assert store.get(stale.id) is None


def test_sweep_on_empty_store_is_a_noop(store: InMemorySessionStore) -> None:
    assert store.sweep() == 0


def test_delete(store: InMemorySessionStore) -> None:
    session = store.create()
    store.delete(session.id)

    assert store.get(session.id) is None
    store.delete(session.id)  # idempotent -- logout twice must not raise


def test_cookie_round_trip() -> None:
    cookie = SessionCookie("s3cret", name="sd_session", secure=True)
    raw = cookie.dumps("session-id-123")

    assert raw != "session-id-123", "session id must not appear in the clear"
    assert cookie.loads(raw) == "session-id-123"


def test_cookie_rejects_tampering() -> None:
    cookie = SessionCookie("s3cret", name="sd_session", secure=True)
    raw = cookie.dumps("session-id-123")

    assert cookie.loads(raw[:-1] + ("x" if raw[-1] != "x" else "y")) is None
    assert cookie.loads("session-id-123") is None
    assert cookie.loads(None) is None
    assert cookie.loads("") is None


def test_cookie_rejects_other_secret() -> None:
    raw = SessionCookie("secret-a", name="sd", secure=True).dumps("session-id-123")

    assert SessionCookie("secret-b", name="sd", secure=True).loads(raw) is None


def test_per_draft_scratch_space_is_keyed_by_workspaceitem(store) -> None:
    """Two tabs are two drafts; one must not overwrite the other."""
    session = store.create()
    session.remember_submission("42", filename="first.pdf", path="/tmp/a")
    session.remember_submission("43", filename="second.pdf", path="/tmp/b")

    assert session.submission("42")["filename"] == "first.pdf"
    assert session.submission("43")["filename"] == "second.pdf"

    dropped = session.forget_submission("43")

    assert dropped["path"] == "/tmp/b"
    assert session.submission("43") == {}
    assert session.submission("42")["path"] == "/tmp/a", "the other draft is untouched"


def test_reading_an_unknown_draft_does_not_grow_the_session(store) -> None:
    """/review/{id} takes the id from the URL, so a read must not create an entry."""
    session = store.create()

    assert session.submission("no-such-draft") == {}
    assert session.forget_submission("no-such-draft") == {}
    assert session.data.get("submissions", {}) == {}


# ---- per-repository idle timeouts -------------------------------------------
#
# One store serves every institution, and their DSpace JWTs do not all expire at
# the same time (DEPLOY-AWS.md, "More than one repository"). A session carries its
# repository's TTL when that repository sets one; the store's value is the default
# for everyone else.


def test_a_session_without_its_own_ttl_uses_the_stores(store, clock: FakeClock) -> None:
    session = store.create()

    assert session.ttl_seconds is None
    assert session.effective_ttl(60) == 60

    clock.advance(60)
    assert store.get(session.id) is None


def test_a_shorter_repository_ttl_expires_the_session_early(store, clock: FakeClock) -> None:
    """An institution whose JWT dies at 10s must not leave us holding it for 60."""
    session = store.create(instance_key="short", ttl_seconds=10)

    clock.advance(9)
    assert store.get(session.id, touch=False) is not None

    clock.advance(1)
    assert store.get(session.id) is None
    assert len(store) == 0


def test_a_longer_repository_ttl_outlives_the_stores(store, clock: FakeClock) -> None:
    session = store.create(instance_key="long", ttl_seconds=600)

    clock.advance(120)  # twice the store's default, and still live
    assert store.get(session.id, touch=False) is not None

    clock.advance(480)
    assert store.get(session.id, touch=False) is None


def test_the_sweep_uses_each_sessions_own_ttl(store, clock: FakeClock) -> None:
    """The sweep is a loop over one store; it must not apply one clock to all."""
    short = store.create(instance_key="short", ttl_seconds=10)
    default = store.create()
    long = store.create(instance_key="long", ttl_seconds=600)

    clock.advance(30)
    assert store.sweep() == 1
    assert store.get(short.id, touch=False) is None
    assert store.get(default.id, touch=False) is not None
    assert store.get(long.id, touch=False) is not None


def test_the_repository_ttl_is_still_an_idle_timeout(store, clock: FakeClock) -> None:
    """An override changes the length of the window, never its idle-ness."""
    session = store.create(instance_key="short", ttl_seconds=10)

    for _ in range(5):
        clock.advance(9)
        assert store.get(session.id) is not None, "an active user is never signed out"

    clock.advance(10)
    assert store.get(session.id) is None
