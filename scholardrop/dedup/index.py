"""The local checksum index (DESIGN.md §12, layer 1).

DSpace stores an MD5 for every bitstream but offers no way to search by it, so
the only way to answer "does this repository already hold these exact bytes?" in
the time an upload takes is to have asked in advance. `scripts/harvest_checksums.py`
fills this table off-peak; the app only ever reads it.

Two properties are structural rather than incidental:

**The app cannot write it.** `open_index` opens the file read-only through a
`file:...?mode=ro` URI, so a bug in a request handler cannot corrupt the index and
a missing file is a clean "no index" rather than an empty one silently created in
whatever directory the process happened to start in.

**One index belongs to one repository.** A checksum from repository A means
nothing about an upload to repository B, and the row would still render a
confident warning with a link to the wrong place. The `base_url` is written into
the index when it is created and checked on every later harvest.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger("scholardrop.dedup")

SCHEMA_VERSION = "1"

_UNSAFE = re.compile(r"[^a-z0-9]+")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bitstreams (
    bitstream_uuid TEXT PRIMARY KEY,
    checksum       TEXT NOT NULL,
    algorithm      TEXT NOT NULL,
    item_uuid      TEXT NOT NULL,
    handle         TEXT,
    title          TEXT,
    filename       TEXT,
    size_bytes     INTEGER,
    seen_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS bitstreams_by_checksum ON bitstreams (checksum, algorithm);
CREATE INDEX IF NOT EXISTS bitstreams_by_item ON bitstreams (item_uuid);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def slugify(value: str) -> str:
    """An instance key, made safe to put in a filename.

    Keys are people-facing config, not identifiers: `config/instances.yaml` really
    has one called `DSpace 9.2`. Lowercased and reduced to `dspace-9-2`, which is
    stable across runs -- the harvester and the app derive the same name from the
    same key without either telling the other.
    """
    slug = _UNSAFE.sub("-", value.strip().lower()).strip("-")
    return slug or "instance"


def index_path_for(
    base_path: Path | str | None, instance_key: str, *, override: str | None = None
) -> Path | None:
    """Where one instance's index lives.

    **One file per repository, because one index only ever describes one.** The
    file records the `base_url` it was harvested from and `open_index` refuses a
    mismatch, so sharing a path between two instances does not corrupt anything --
    it just means the second instance silently never gets an exact-file check,
    which is the sort of quiet nothing that takes a day to notice. Deriving the
    name removes the choice.

    `var/checksums.sqlite` + key `shareok` -> `var/checksums-shareok.sqlite`. An
    instance may name its own file instead (`dedup_checksum_index_path` in
    `instances.yaml`) for a repository whose index belongs on another disk.
    """
    if override:
        return Path(override)
    if not base_path:
        return None
    base = Path(base_path)
    return base.with_name(f"{base.stem}-{slugify(instance_key)}{base.suffix}")


class IndexUnavailable(Exception):
    """There is no index to read, or it belongs to a different repository.

    Never fatal: layer 1 is an optional extra, and a repository with no harvest
    behind it simply gets no exact-file warnings.
    """


@dataclass(frozen=True)
class IndexedBitstream:
    """One harvested file. `title` and `handle` are the *item's*, for the warning."""

    bitstream_uuid: str
    checksum: str
    algorithm: str
    item_uuid: str
    handle: str = ""
    title: str = ""
    filename: str = ""
    size_bytes: int = 0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> IndexedBitstream:
        return cls(
            bitstream_uuid=row["bitstream_uuid"],
            checksum=row["checksum"],
            algorithm=row["algorithm"],
            item_uuid=row["item_uuid"],
            handle=row["handle"] or "",
            title=row["title"] or "",
            filename=row["filename"] or "",
            size_bytes=int(row["size_bytes"] or 0),
        )


class ChecksumIndex:
    """Read/write access to one repository's checksum index.

    Sync on purpose. A lookup is a single indexed SELECT against a local file --
    microseconds -- so handing it to a thread would cost more than it saves. The
    *hashing* is the expensive half, and that happens chunk by chunk while the
    upload streams (`submission/storage.py`), never as one blocking read.
    """

    def __init__(self, connection: sqlite3.Connection, *, path: Path, writable: bool) -> None:
        self._db = connection
        self._db.row_factory = sqlite3.Row
        self.path = path
        self.writable = writable

    # ---- lifecycle ---------------------------------------------------------

    @classmethod
    def create(cls, path: Path, *, base_url: str) -> ChecksumIndex:
        """Open for writing, creating the file and schema if needed.

        Only the harvester calls this.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
        connection.executescript(_SCHEMA)
        index = cls(connection, path=path, writable=True)
        index._claim(base_url)
        return index

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> ChecksumIndex:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- state -------------------------------------------------------------

    def get_state(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_state(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT INTO state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._db.commit()

    @property
    def base_url(self) -> str:
        return self.get_state("base_url") or ""

    @property
    def watermark(self) -> str | None:
        """Newest accession date of the last *completed* run.

        Written only when a run finishes: an interrupted harvest that moved the
        watermark would leave a permanent hole in the index, and nothing would
        ever go back for it.
        """
        return self.get_state("watermark")

    def set_watermark(self, value: str) -> None:
        self.set_state("watermark", value)
        self.set_state("watermark_written_at", _now())

    def _claim(self, base_url: str) -> None:
        existing = self.base_url
        if not existing:
            self.set_state("base_url", base_url)
            self.set_state("schema_version", SCHEMA_VERSION)
            return
        if existing.rstrip("/") != base_url.rstrip("/"):
            raise IndexUnavailable(
                f"{self.path} was harvested from {existing}, not {base_url}. "
                "Use one index file per repository."
            )

    # ---- reading -----------------------------------------------------------

    def lookup(self, checksum: str, *, algorithm: str = "MD5") -> list[IndexedBitstream]:
        """Every indexed file with this digest, newest harvest first.

        The algorithm is part of the query, not an assumption: an MD5 hex string
        compared against some other algorithm's digest of the same length would
        be a false match presented as a certainty.
        """
        if not checksum:
            return []
        rows = self._db.execute(
            "SELECT * FROM bitstreams WHERE checksum = ? AND algorithm = ? "
            "ORDER BY seen_at DESC, bitstream_uuid",
            (checksum.strip().lower(), algorithm.upper()),
        ).fetchall()
        return [IndexedBitstream.from_row(row) for row in rows]

    def count(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) AS n FROM bitstreams").fetchone()["n"])

    def items_indexed(self) -> int:
        return int(
            self._db.execute("SELECT COUNT(DISTINCT item_uuid) AS n FROM bitstreams").fetchone()[
                "n"
            ]
        )

    def all_rows(self) -> Iterator[IndexedBitstream]:
        for row in self._db.execute("SELECT * FROM bitstreams"):
            yield IndexedBitstream.from_row(row)

    # ---- writing -----------------------------------------------------------

    def upsert(self, rows: Iterable[IndexedBitstream]) -> int:
        """Insert or refresh rows, keyed by bitstream uuid. Returns how many.

        Upsert rather than insert because a re-harvest of the same item must not
        double it, and because an item that was retitled since the last run should
        end up with its new title rather than two rows disagreeing.
        """
        seen_at = _now()
        payload = [
            (
                row.bitstream_uuid,
                row.checksum.strip().lower(),
                row.algorithm.upper(),
                row.item_uuid,
                row.handle,
                row.title,
                row.filename,
                row.size_bytes,
                seen_at,
            )
            for row in rows
            if row.bitstream_uuid and row.checksum
        ]
        if not payload:
            return 0
        self._db.executemany(
            "INSERT INTO bitstreams (bitstream_uuid, checksum, algorithm, item_uuid, handle, "
            "title, filename, size_bytes, seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(bitstream_uuid) DO UPDATE SET "
            "checksum = excluded.checksum, algorithm = excluded.algorithm, "
            "item_uuid = excluded.item_uuid, handle = excluded.handle, title = excluded.title, "
            "filename = excluded.filename, size_bytes = excluded.size_bytes, "
            "seen_at = excluded.seen_at",
            payload,
        )
        self._db.commit()
        return len(payload)

    def forget_item(self, item_uuid: str) -> int:
        """Drop an item's rows -- used when a hit turns out to be gone."""
        cursor = self._db.execute("DELETE FROM bitstreams WHERE item_uuid = ?", (item_uuid,))
        self._db.commit()
        return cursor.rowcount


def open_index(path: Path | str | None, *, base_url: str = "") -> ChecksumIndex:
    """Open the index read-only, or say plainly why there is none.

    Raises `IndexUnavailable` for every "we cannot check" case -- no path
    configured, no file yet, a file from another repository, an unreadable file --
    because they are all the same thing to the caller: this upload gets no
    exact-file warning, and the submission carries on.
    """
    if not path:
        raise IndexUnavailable("no duplicate index is configured")
    resolved = Path(path)
    if not resolved.is_file():
        raise IndexUnavailable(
            f"{resolved} does not exist yet -- run scripts/harvest_checksums.py to build it"
        )
    try:
        connection = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
        index = ChecksumIndex(connection, path=resolved, writable=False)
        # `connect` does not read the file, so a truncated or non-SQLite one is
        # only discovered by a query. Ask now, while the failure is still "there
        # is no index" rather than an exception out of a lookup mid-upload.
        harvested_from = index.base_url
    except sqlite3.Error as exc:
        raise IndexUnavailable(f"{resolved} could not be read: {exc}") from exc

    if base_url and harvested_from and harvested_from.rstrip("/") != base_url.rstrip("/"):
        index.close()
        raise IndexUnavailable(f"{resolved} was harvested from {harvested_from}, not {base_url}")
    return index


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
