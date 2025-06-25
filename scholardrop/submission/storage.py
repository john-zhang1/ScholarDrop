"""Taking the uploaded PDF onto disk, safely (DESIGN.md §8).

Three rules, all of them because the browser is not to be trusted: the file is
validated by **magic bytes** rather than its extension or Content-Type, it is
capped while it streams (never read whole into memory first), and it lands in a
random per-session directory whose name is not the session id.
"""

from __future__ import annotations

import logging
import re
import secrets
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from scholardrop.extract.reader import PDF_MAGIC, looks_like_pdf

log = logging.getLogger("scholardrop.upload")

CHUNK = 1024 * 1024
MAX_FILENAME = 120
_SAFE = re.compile(r"[^A-Za-z0-9._ -]+")
DEFAULT_FILENAME = "upload.pdf"


class UploadRejected(Exception):
    """The upload cannot be accepted. `message` is shown to the user as-is."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class UploadStream(Protocol):
    """The slice of Starlette's UploadFile this module needs."""

    filename: str | None

    async def read(self, size: int = -1) -> bytes: ...


@dataclass(frozen=True)
class StoredUpload:
    path: Path
    filename: str
    size: int

    def read_bytes(self) -> bytes:
        return self.path.read_bytes()


def safe_filename(raw: str | None) -> str:
    """A filename safe to join to a path and to send to DSpace.

    Only the basename survives, and only characters that cannot escape a
    directory. The name still reaches the repository, so it is kept recognisable
    rather than replaced with a hash.
    """
    name = Path(str(raw or "")).name
    name = _SAFE.sub("_", name).strip(" .")
    if not name:
        return DEFAULT_FILENAME
    return name[:MAX_FILENAME]


def session_upload_dir(upload_dir: Path, session: Any) -> Path:
    """A per-session directory with a random name.

    The session id is deliberately not used: it is a live credential, and this
    name ends up in file paths and log lines. Phase 6's cleanup job sweeps these
    directories by age.
    """
    token = session.data.get("upload_token")
    if not token:
        token = secrets.token_urlsafe(12)
        session.data["upload_token"] = token
    directory = Path(upload_dir) / token
    directory.mkdir(parents=True, exist_ok=True)
    return directory


async def store_upload(upload: UploadStream, *, directory: Path, max_bytes: int) -> StoredUpload:
    """Stream an upload to disk, rejecting anything that is not a capped PDF.

    The cap is enforced *during* the copy, so an oversized file never occupies
    more than one chunk of memory or a fraction of the disk before it is
    refused; the partial file is removed on every rejection path.
    """
    filename = safe_filename(getattr(upload, "filename", None))
    target = directory / f"{secrets.token_urlsafe(8)}-{filename}"
    size = 0
    # The signature can straddle chunks: `read()` may hand back fewer bytes than
    # asked for, so a body fed in dribs could pass a check made on the first chunk
    # alone. Buffer until there is enough to judge, then judge once.
    head = b""
    checked = False

    try:
        with target.open("wb") as sink:
            while True:
                chunk = await upload.read(CHUNK)
                if not chunk:
                    break
                if not checked:
                    head += chunk
                    if len(head) >= len(PDF_MAGIC):
                        if not looks_like_pdf(head):
                            raise UploadRejected(
                                "That file is not a PDF. ScholarDrop accepts a single PDF."
                            )
                        checked = True
                size += len(chunk)
                if size > max_bytes:
                    raise UploadRejected(
                        f"That file is larger than the {_megabytes(max_bytes)} limit."
                    )
                sink.write(chunk)
    except UploadRejected:
        target.unlink(missing_ok=True)
        raise
    except OSError as exc:
        target.unlink(missing_ok=True)
        log.error("could not store upload: %s", exc)
        raise UploadRejected("The file could not be saved. Please try again.") from exc

    if size == 0:
        target.unlink(missing_ok=True)
        raise UploadRejected("That file is empty. Choose the PDF you want to deposit.")

    if not checked:
        # Ran out of file before the signature was complete: too short to be a PDF.
        target.unlink(missing_ok=True)
        raise UploadRejected("That file is not a PDF. ScholarDrop accepts a single PDF.")

    log.info("stored upload %s (%d bytes)", target.name, size)
    return StoredUpload(path=target, filename=filename, size=size)


def discard_upload_dir(upload_dir: Path, session: Any) -> None:
    """Remove this session's uploads now, rather than waiting for the sweep.

    Called when a session ends deliberately (sign-out): the file has already
    served its purpose the moment it reached DSpace, and an uploaded thesis is
    not something to leave lying around for the TTL.
    """
    token = session.data.get("upload_token")
    if not token:
        return
    _remove_tree(Path(upload_dir) / str(token))
    session.data.pop("upload_token", None)


def sweep_uploads(upload_dir: Path, max_age_seconds: float, *, now: float | None = None) -> int:
    """Delete per-session upload directories nothing has touched in a while.

    The backstop for every path that does not clean up after itself: an abandoned
    tab, a crash between upload and deposit, a session that simply expired.
    Returns how many directories were removed.
    """
    root = Path(upload_dir)
    if not root.is_dir():
        return 0
    cutoff = (now if now is not None else time.time()) - max_age_seconds
    removed = 0
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        if _newest_mtime(entry) > cutoff:
            continue
        if _remove_tree(entry):
            removed += 1
    if removed:
        log.info("swept %d abandoned upload director%s", removed, "y" if removed == 1 else "ies")
    return removed


def _newest_mtime(directory: Path) -> float:
    """The most recent mtime in a directory, its own included.

    Files, not the directory, because writing into a directory updates its mtime
    but reading does not -- and an upload that is still being reviewed should not
    be swept out from under the user.
    """
    newest = directory.stat().st_mtime
    for path in directory.rglob("*"):
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:  # vanished mid-sweep; nothing to protect
            continue
    return newest


def _remove_tree(directory: Path) -> bool:
    try:
        shutil.rmtree(directory)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.warning("could not remove %s: %s", directory, exc)
        return False


def _megabytes(value: int) -> str:
    return f"{value / (1024 * 1024):.0f} MB"
