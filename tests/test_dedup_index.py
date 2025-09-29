"""The checksum index (DESIGN.md §12, layer 1).

The properties worth pinning are the ones that keep a *wrong* warning off the
screen: an index from another repository is refused, a digest only matches a
digest of the same algorithm, and a re-harvest updates rather than duplicates.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scholardrop.dedup.index import (
    ChecksumIndex,
    IndexedBitstream,
    IndexUnavailable,
    index_path_for,
    open_index,
    slugify,
)

BASE = "https://dspace.test/server"


def row(
    checksum: str = "abc123",
    *,
    bitstream: str = "bs-1",
    item: str = "item-1",
    algorithm: str = "MD5",
    title: str = "A Thesis",
    filename: str = "thesis.pdf",
) -> IndexedBitstream:
    return IndexedBitstream(
        bitstream_uuid=bitstream,
        checksum=checksum,
        algorithm=algorithm,
        item_uuid=item,
        handle="11244/1",
        title=title,
        filename=filename,
        size_bytes=1024,
    )


@pytest.fixture
def index(tmp_path: Path) -> ChecksumIndex:
    with ChecksumIndex.create(tmp_path / "sub" / "checksums.sqlite", base_url=BASE) as store:
        yield store


def test_it_creates_its_own_directory(tmp_path: Path) -> None:
    path = tmp_path / "var" / "nested" / "checksums.sqlite"
    with ChecksumIndex.create(path, base_url=BASE):
        pass
    assert path.is_file()


def test_a_row_can_be_found_by_its_checksum(index: ChecksumIndex) -> None:
    assert index.upsert([row()]) == 1
    found = index.lookup("abc123")
    assert [f.item_uuid for f in found] == ["item-1"]
    assert found[0].filename == "thesis.pdf"


def test_the_lookup_is_case_insensitive_about_hex(index: ChecksumIndex) -> None:
    """DSpace writes lowercase hex and hashlib produces lowercase, but a digest
    that arrived uppercase from anywhere must not silently miss."""
    index.upsert([row("ABC123")])
    assert index.lookup("abc123")
    assert index.lookup("ABC123")


def test_a_digest_only_matches_the_same_algorithm(index: ChecksumIndex) -> None:
    """An MD5 hex string and a same-length digest of another algorithm would
    compare equal as text. The claim being made is "identical file", so the
    algorithm is part of the question."""
    index.upsert([row("deadbeef", algorithm="SHA-256")])
    assert index.lookup("deadbeef", algorithm="MD5") == []
    assert index.lookup("deadbeef", algorithm="SHA-256")


def test_an_empty_checksum_never_matches(index: ChecksumIndex) -> None:
    index.upsert([row("")])
    assert index.lookup("") == []
    assert index.count() == 0


def test_re_harvesting_updates_a_row_rather_than_doubling_it(index: ChecksumIndex) -> None:
    index.upsert([row(title="Old Title")])
    index.upsert([row(title="New Title")])
    found = index.lookup("abc123")
    assert len(found) == 1
    assert found[0].title == "New Title"


def test_two_items_holding_the_same_file_are_two_rows(index: ChecksumIndex) -> None:
    index.upsert([row(bitstream="bs-1", item="item-1"), row(bitstream="bs-2", item="item-2")])
    assert {f.item_uuid for f in index.lookup("abc123")} == {"item-1", "item-2"}
    assert index.count() == 2
    assert index.items_indexed() == 2


def test_the_watermark_starts_unset_and_persists(tmp_path: Path) -> None:
    path = tmp_path / "checksums.sqlite"
    with ChecksumIndex.create(path, base_url=BASE) as store:
        assert store.watermark is None
        store.set_watermark("2026-07-28T07:50:01Z")
    with ChecksumIndex.create(path, base_url=BASE) as store:
        assert store.watermark == "2026-07-28T07:50:01Z"


def test_an_index_belongs_to_one_repository(tmp_path: Path) -> None:
    """A checksum from repository A says nothing about an upload to B -- but the
    warning it renders would look exactly as confident."""
    path = tmp_path / "checksums.sqlite"
    with ChecksumIndex.create(path, base_url=BASE):
        pass
    with pytest.raises(IndexUnavailable, match="one index file per repository"):
        ChecksumIndex.create(path, base_url="https://other.test/server")


def test_a_trailing_slash_is_not_a_different_repository(tmp_path: Path) -> None:
    path = tmp_path / "checksums.sqlite"
    with ChecksumIndex.create(path, base_url=BASE):
        pass
    with ChecksumIndex.create(path, base_url=BASE + "/") as store:
        assert store.base_url == BASE


def test_forgetting_an_item_removes_all_of_its_files(index: ChecksumIndex) -> None:
    index.upsert([row(bitstream="bs-1"), row(bitstream="bs-2", checksum="other")])
    assert index.forget_item("item-1") == 2
    assert index.count() == 0


# ---- open_index: every "we cannot check" case is the same case ---------------


def test_no_path_configured_is_not_an_error_it_is_no_index() -> None:
    with pytest.raises(IndexUnavailable, match="no duplicate index is configured"):
        open_index(None)


def test_a_missing_file_says_how_to_make_one(tmp_path: Path) -> None:
    with pytest.raises(IndexUnavailable, match="harvest_checksums"):
        open_index(tmp_path / "not-there.sqlite")


def test_an_index_from_another_repository_is_refused_on_read(tmp_path: Path) -> None:
    path = tmp_path / "checksums.sqlite"
    with ChecksumIndex.create(path, base_url=BASE):
        pass
    with pytest.raises(IndexUnavailable, match="was harvested from"):
        open_index(path, base_url="https://other.test/server")


def test_the_app_side_handle_cannot_write(tmp_path: Path) -> None:
    """The read-only URI is what stops a bug in a request handler corrupting an
    index it takes an hour to rebuild."""
    path = tmp_path / "checksums.sqlite"
    with ChecksumIndex.create(path, base_url=BASE) as store:
        store.upsert([row()])
    with open_index(path, base_url=BASE) as store:
        assert store.lookup("abc123")
        assert store.writable is False
        with pytest.raises(sqlite3.OperationalError):
            store.upsert([row(bitstream="bs-9")])


def test_a_file_that_is_not_a_database_is_just_no_index(tmp_path: Path) -> None:
    """`sqlite3.connect` succeeds on any file, so the failure has to be provoked
    at open time -- otherwise it surfaces from a lookup in the middle of an
    upload, where it is an exception rather than a missing warning."""
    path = tmp_path / "checksums.sqlite"
    path.write_text("this is not a database")
    with pytest.raises(IndexUnavailable, match="could not be read"):
        open_index(path)


# ---- one index per instance --------------------------------------------------


def test_the_path_is_derived_from_the_instance_key() -> None:
    """The harvester and the app both derive it from the same two inputs, so
    neither has to be told where the other put the file."""
    assert index_path_for("var/checksums.sqlite", "shareok") == Path("var/checksums-shareok.sqlite")


def test_a_key_that_is_not_a_filename_still_becomes_one() -> None:
    """`config/instances.yaml` really has an instance called `DSpace 9.2`. Keys
    are people-facing config, not identifiers."""
    assert index_path_for("var/checksums.sqlite", "DSpace 9.2") == Path(
        "var/checksums-dspace-9-2.sqlite"
    )
    assert slugify("  ") == "instance"


def test_two_instances_never_share_a_file() -> None:
    """The bug this replaced: a single path meant only the one repository that
    happened to be harvested ever got an exact-file check, and every other one
    silently got nothing at all."""
    paths = {index_path_for("var/checksums.sqlite", key) for key in ("shareok", "local", "test")}
    assert len(paths) == 3


def test_an_instance_may_name_its_own_file() -> None:
    assert index_path_for("var/checksums.sqlite", "shareok", override="/data/sk.db") == Path(
        "/data/sk.db"
    )


def test_no_configured_path_means_no_index_at_all() -> None:
    assert index_path_for(None, "shareok") is None
