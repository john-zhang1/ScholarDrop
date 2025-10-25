"""The deposit log (DESIGN.md §13.3).

The two properties worth a test each are the ones that would fail silently: a
read that crosses users or repositories, and a write that takes a successful
deposit down with it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scholardrop.history.log import Deposit, LogUnavailable, open_log

BASE = "https://dspace.test/server"
OTHER = "https://other.test/server"


def a_deposit(**overrides: object) -> Deposit:
    fields: dict = {
        "base_url": BASE,
        "instance_key": "test",
        "eperson_uuid": "eperson-1",
        "wsi_id": "42",
        "item_uuid": "item-1",
        "collection_name": "OU - Theses",
        "title": "A Thesis",
        "deposited_at": "2026-07-30T10:00:00+00:00",
    }
    fields.update(overrides)
    return Deposit(**fields)  # type: ignore[arg-type]


def test_a_deposit_comes_back_as_written(tmp_path: Path) -> None:
    with open_log(tmp_path / "s.sqlite") as history:
        assert history.record(a_deposit()) is True
        rows = history.for_person(base_url=BASE, eperson_uuid="eperson-1")

    assert len(rows) == 1
    assert rows[0].title == "A Thesis"
    assert rows[0].item_uuid == "item-1"
    assert rows[0].collection_name == "OU - Theses"


def test_the_file_and_its_parent_are_created_on_first_use(tmp_path: Path) -> None:
    path = tmp_path / "var" / "nested" / "s.sqlite"
    with open_log(path) as history:
        history.record(a_deposit())
    assert path.is_file()


def test_one_persons_deposits_are_not_another_persons(tmp_path: Path) -> None:
    """The disclosure this file exists to avoid: one log, many users."""
    with open_log(tmp_path / "s.sqlite") as history:
        history.record(a_deposit(wsi_id="1", eperson_uuid="alice", title="Alice's thesis"))
        history.record(a_deposit(wsi_id="2", eperson_uuid="bob", title="Bob's thesis"))

        alice = history.for_person(base_url=BASE, eperson_uuid="alice")
        bob = history.for_person(base_url=BASE, eperson_uuid="bob")

    assert [row.title for row in alice] == ["Alice's thesis"]
    assert [row.title for row in bob] == ["Bob's thesis"]


def test_a_deposit_at_another_repository_is_not_listed(tmp_path: Path) -> None:
    """An eperson uuid means nothing outside the repository that issued it."""
    with open_log(tmp_path / "s.sqlite") as history:
        history.record(a_deposit(wsi_id="1", base_url=OTHER, title="Elsewhere"))
        rows = history.for_person(base_url=BASE, eperson_uuid="eperson-1")
    assert rows == []


def test_a_trailing_slash_is_not_a_different_repository(tmp_path: Path) -> None:
    with open_log(tmp_path / "s.sqlite") as history:
        history.record(a_deposit(base_url=BASE + "/"))
        assert len(history.for_person(base_url=BASE, eperson_uuid="eperson-1")) == 1


def test_reading_without_both_filters_returns_nothing(tmp_path: Path) -> None:
    """Neither filter has a safe default, so a caller that forgets one gets nothing
    rather than everything."""
    with open_log(tmp_path / "s.sqlite") as history:
        history.record(a_deposit())
        assert history.for_person(base_url=BASE, eperson_uuid="") == []
        assert history.for_person(base_url="", eperson_uuid="eperson-1") == []


def test_depositing_the_same_draft_twice_refreshes_one_row(tmp_path: Path) -> None:
    with open_log(tmp_path / "s.sqlite") as history:
        history.record(a_deposit(title="First read"))
        history.record(a_deposit(title="Corrected title", handle="11244/1"))
        rows = history.for_person(base_url=BASE, eperson_uuid="eperson-1")

    assert len(rows) == 1
    assert rows[0].title == "Corrected title"
    assert rows[0].handle == "11244/1"


def test_rows_come_back_newest_first(tmp_path: Path) -> None:
    with open_log(tmp_path / "s.sqlite") as history:
        history.record(a_deposit(wsi_id="1", deposited_at="2026-07-01T09:00:00+00:00"))
        history.record(a_deposit(wsi_id="2", deposited_at="2026-07-30T09:00:00+00:00"))
        history.record(a_deposit(wsi_id="3", deposited_at="2026-07-15T09:00:00+00:00"))
        rows = history.for_person(base_url=BASE, eperson_uuid="eperson-1")
    assert [row.wsi_id for row in rows] == ["2", "3", "1"]


def test_a_row_with_no_submitter_or_draft_is_not_written(tmp_path: Path) -> None:
    with open_log(tmp_path / "s.sqlite") as history:
        assert history.record(a_deposit(eperson_uuid="")) is False
        assert history.record(a_deposit(wsi_id="")) is False
        assert history.count_for_person(base_url=BASE, eperson_uuid="eperson-1") == 0


def test_a_write_that_fails_says_so_instead_of_raising(tmp_path: Path) -> None:
    """The deposit already happened. Nothing here may turn that into a failure."""
    path = tmp_path / "s.sqlite"
    history = open_log(path)
    history._db.close()  # the sort of thing a crashed filesystem does

    assert history.record(a_deposit()) is False


def test_a_read_that_fails_degrades_to_no_history(tmp_path: Path) -> None:
    """Corruption SQLite only finds mid-query -- the file opened fine -- must
    read as "no history", not take the status page down with a 500."""
    history = open_log(tmp_path / "s.sqlite")
    history.record(a_deposit())
    history._db.close()

    assert history.for_person(base_url=BASE, eperson_uuid="eperson-1") == []
    assert history.count_for_person(base_url=BASE, eperson_uuid="eperson-1") == 0
    assert history.forget(base_url=BASE, wsi_id="42") == 0


def test_a_path_that_is_not_a_database_is_LogUnavailable(tmp_path: Path) -> None:
    path = tmp_path / "not-a-db.sqlite"
    path.write_text("this is not SQLite")
    with pytest.raises(LogUnavailable):
        open_log(path)


def test_no_path_configured_is_LogUnavailable() -> None:
    with pytest.raises(LogUnavailable):
        open_log(None)


def test_a_directory_where_the_file_should_be_is_LogUnavailable(tmp_path: Path) -> None:
    (tmp_path / "s.sqlite").mkdir()
    with pytest.raises(LogUnavailable):
        open_log(tmp_path / "s.sqlite")


def test_forgetting_a_row_removes_it(tmp_path: Path) -> None:
    with open_log(tmp_path / "s.sqlite") as history:
        history.record(a_deposit())
        assert history.forget(base_url=BASE, wsi_id="42") == 1
        assert history.count_for_person(base_url=BASE, eperson_uuid="eperson-1") == 0


def test_the_schema_survives_reopening(tmp_path: Path) -> None:
    path = tmp_path / "s.sqlite"
    with open_log(path) as history:
        history.record(a_deposit())
    with open_log(path) as history:
        assert history.count_for_person(base_url=BASE, eperson_uuid="eperson-1") == 1
    with sqlite3.connect(path) as db:
        version = db.execute("SELECT value FROM state WHERE key = 'schema_version'").fetchone()
    assert version[0] == "1"
