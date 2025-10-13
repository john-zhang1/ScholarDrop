"""What this app has deposited (DESIGN.md §13.3).

DSpace can answer "what drafts does this person have?" (`findBySubmitter`) and
"what is this item?" (`/core/items/{uuid}`), but nothing joins the two: once a
deposit is archived, the workspaceitem that carried it is gone and the repository
has no record that *this app* sent it. So ScholarDrop keeps one row per deposit
it performed, and the status page reads live state back through the item uuid it
saved.

Two properties are structural rather than incidental:

**A row belongs to one person at one repository.** Every read filters on
`base_url` *and* `eperson_uuid`, and both are written at deposit time. The log is
one file shared by everyone using this process, so a query that forgot either
would show one submitter another's titles -- the same disclosure the anonymous
harvest in `dedup/index.py` exists to avoid, arrived at from the other direction.

**Writing it can never fail a deposit.** By the time a row is written the
repository already has the item; refusing the confirmation page because local
bookkeeping failed would report a successful deposit as a failure. `record()`
therefore swallows its own errors and says so in the log, and every read
degrades to "no history" rather than to an exception.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger("scholardrop.history")

SCHEMA_VERSION = "1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS submissions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    base_url         TEXT NOT NULL,
    instance_key     TEXT NOT NULL,
    eperson_uuid     TEXT NOT NULL,
    wsi_id           TEXT NOT NULL,
    item_uuid        TEXT NOT NULL DEFAULT '',
    handle           TEXT NOT NULL DEFAULT '',
    collection_uuid  TEXT NOT NULL DEFAULT '',
    collection_name  TEXT NOT NULL DEFAULT '',
    title            TEXT NOT NULL DEFAULT '',
    entered_workflow INTEGER NOT NULL DEFAULT 0,
    workflow_id      TEXT NOT NULL DEFAULT '',
    deposited_at     TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS submissions_by_draft
    ON submissions (base_url, wsi_id);
CREATE INDEX IF NOT EXISTS submissions_by_person
    ON submissions (base_url, eperson_uuid, deposited_at DESC);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class LogUnavailable(Exception):
    """There is no submissions log to read or write.

    Never fatal. A deposit still happens, a confirmation page still renders, and
    the status page still lists the drafts DSpace knows about -- it just cannot
    add the deposits this app remembers.
    """


@dataclass(frozen=True)
class Deposit:
    """One deposit this app performed.

    `item_uuid` is the load-bearing field: it is the only handle on the
    submission that survives the deposit, because the workspaceitem is consumed
    at that moment and its id names nothing afterwards. A row without one can
    still be listed, but its live state can never be looked up again.
    """

    base_url: str
    instance_key: str
    eperson_uuid: str
    wsi_id: str
    item_uuid: str = ""
    handle: str = ""
    collection_uuid: str = ""
    collection_name: str = ""
    title: str = ""
    entered_workflow: bool = False
    workflow_id: str = ""
    deposited_at: str = field(default_factory=lambda: _now())

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Deposit:
        return cls(
            base_url=row["base_url"],
            instance_key=row["instance_key"],
            eperson_uuid=row["eperson_uuid"],
            wsi_id=row["wsi_id"],
            item_uuid=row["item_uuid"] or "",
            handle=row["handle"] or "",
            collection_uuid=row["collection_uuid"] or "",
            collection_name=row["collection_name"] or "",
            title=row["title"] or "",
            entered_workflow=bool(row["entered_workflow"]),
            workflow_id=row["workflow_id"] or "",
            deposited_at=row["deposited_at"],
        )


class SubmissionLog:
    """Read/write access to the deposit log.

    Sync, like `ChecksumIndex` and for the same reason: a handful of indexed rows
    out of a local file costs microseconds, and handing that to a thread would
    cost more than it saves. Unlike the checksum index this file *is* written by
    the app -- it is the app's own record of what it did, not a harvest of the
    repository -- so it is opened read-write and created on first use.
    """

    def __init__(self, connection: sqlite3.Connection, *, path: Path) -> None:
        self._db = connection
        self._db.row_factory = sqlite3.Row
        self.path = path

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> SubmissionLog:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- writing -----------------------------------------------------------

    def record(self, deposit: Deposit) -> bool:
        """Remember one deposit. Returns whether the row was written.

        Never raises. The caller is on the far side of a successful deposit, and
        there is nothing useful it could do with a SQLite error except fail a
        page that should say "your submission is in the repository".

        Upsert on `(base_url, wsi_id)`: a draft is deposited once, so a second
        write for the same one is a retried request rather than a second
        submission, and it should refresh the row instead of duplicating it.
        """
        if not (deposit.base_url and deposit.eperson_uuid and deposit.wsi_id):
            log.info("not logging a deposit with no repository, submitter or draft id")
            return False
        try:
            self._db.execute(
                "INSERT INTO submissions (base_url, instance_key, eperson_uuid, wsi_id, "
                "item_uuid, handle, collection_uuid, collection_name, title, "
                "entered_workflow, workflow_id, deposited_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(base_url, wsi_id) DO UPDATE SET "
                "item_uuid = excluded.item_uuid, handle = excluded.handle, "
                "collection_uuid = excluded.collection_uuid, "
                "collection_name = excluded.collection_name, title = excluded.title, "
                "entered_workflow = excluded.entered_workflow, "
                "workflow_id = excluded.workflow_id, deposited_at = excluded.deposited_at",
                (
                    deposit.base_url.rstrip("/"),
                    deposit.instance_key,
                    deposit.eperson_uuid,
                    str(deposit.wsi_id),
                    deposit.item_uuid,
                    deposit.handle,
                    deposit.collection_uuid,
                    deposit.collection_name,
                    deposit.title,
                    int(deposit.entered_workflow),
                    deposit.workflow_id,
                    deposit.deposited_at,
                ),
            )
            self._db.commit()
        except sqlite3.Error as exc:
            log.warning("could not log the deposit of draft %s: %s", deposit.wsi_id, exc)
            return False
        return True

    # ---- reading -----------------------------------------------------------

    def for_person(
        self, *, base_url: str, eperson_uuid: str, limit: int = 25, offset: int = 0
    ) -> list[Deposit]:
        """This person's deposits at this repository, newest first.

        Both filters are required rather than defaulted: a call that forgot one
        would return somebody else's submissions, and the wrong default here is
        not a bug that shows up as an error.
        """
        if not (base_url and eperson_uuid):
            return []
        rows = self._db.execute(
            "SELECT * FROM submissions WHERE base_url = ? AND eperson_uuid = ? "
            "ORDER BY deposited_at DESC, id DESC LIMIT ? OFFSET ?",
            (base_url.rstrip("/"), eperson_uuid, max(int(limit), 0), max(int(offset), 0)),
        ).fetchall()
        return [Deposit.from_row(row) for row in rows]

    def count_for_person(self, *, base_url: str, eperson_uuid: str) -> int:
        if not (base_url and eperson_uuid):
            return 0
        row = self._db.execute(
            "SELECT COUNT(*) AS n FROM submissions WHERE base_url = ? AND eperson_uuid = ?",
            (base_url.rstrip("/"), eperson_uuid),
        ).fetchone()
        return int(row["n"])

    def forget(self, *, base_url: str, wsi_id: str) -> int:
        """Drop a row. For the case where the deposit it claims never happened."""
        cursor = self._db.execute(
            "DELETE FROM submissions WHERE base_url = ? AND wsi_id = ?",
            (base_url.rstrip("/"), str(wsi_id)),
        )
        self._db.commit()
        return cursor.rowcount


def open_log(path: Path | str | None) -> SubmissionLog:
    """Open the log, creating the file and schema if this is the first deposit.

    Raises `LogUnavailable` for every "there is no log" case -- unconfigured, an
    undirectory-able path, a file that is not SQLite -- because they are all the
    same thing to the caller: this deposit goes unrecorded, or this page lists
    drafts only.
    """
    if not path:
        raise LogUnavailable("no submissions log is configured")
    resolved = Path(path)
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(resolved)
        connection.executescript(_SCHEMA)
        connection.commit()
    except (OSError, sqlite3.Error) as exc:
        raise LogUnavailable(f"{resolved} could not be opened: {exc}") from exc
    submissions = SubmissionLog(connection, path=resolved)
    submissions._db.execute(
        "INSERT INTO state (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (SCHEMA_VERSION,),
    )
    submissions._db.commit()
    return submissions


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
