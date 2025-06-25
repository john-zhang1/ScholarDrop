"""Upload validation (DESIGN.md §8): magic bytes, a cap, and a safe path."""

from __future__ import annotations

from pathlib import Path

import pytest

from scholardrop.session import InMemorySessionStore
from scholardrop.submission.storage import (
    UploadRejected,
    safe_filename,
    session_upload_dir,
    store_upload,
)

PDF = b"%PDF-1.7\n" + b"x" * 200


class FakeUpload:
    """Starlette's UploadFile, reduced to what storage.py uses."""

    def __init__(self, filename: str | None, content: bytes, *, chunk: int = 64) -> None:
        self.filename = filename
        self._content = content
        self._offset = 0
        self._chunk = chunk

    async def read(self, size: int = -1) -> bytes:
        take = self._chunk if size < 0 else min(size, self._chunk)
        data = self._content[self._offset : self._offset + take]
        self._offset += len(data)
        return data


@pytest.fixture
def directory(tmp_path: Path) -> Path:
    d = tmp_path / "uploads" / "abc"
    d.mkdir(parents=True)
    return d


async def test_a_pdf_is_stored_with_its_name_and_size(directory: Path) -> None:
    stored = await store_upload(FakeUpload("thesis.pdf", PDF), directory=directory, max_bytes=1024)

    assert stored.filename == "thesis.pdf"
    assert stored.size == len(PDF)
    assert stored.read_bytes() == PDF
    assert stored.path.parent == directory


async def test_a_non_pdf_is_refused_by_its_content_not_its_name(directory: Path) -> None:
    """The extension says PDF; the bytes say otherwise. The bytes win."""
    with pytest.raises(UploadRejected) as exc:
        await store_upload(
            FakeUpload("trojan.pdf", b"MZ\x90\x00not a pdf at all"),
            directory=directory,
            max_bytes=1024,
        )

    assert "not a PDF" in exc.value.message
    assert list(directory.iterdir()) == [], "a rejected upload must not stay on disk"


async def test_an_oversized_file_is_refused_while_it_streams(directory: Path) -> None:
    with pytest.raises(UploadRejected) as exc:
        await store_upload(
            FakeUpload("big.pdf", b"%PDF-1.7" + b"x" * 5000), directory=directory, max_bytes=512
        )

    assert "larger than" in exc.value.message
    assert list(directory.iterdir()) == []


async def test_an_empty_file_is_refused(directory: Path) -> None:
    with pytest.raises(UploadRejected):
        await store_upload(FakeUpload("empty.pdf", b""), directory=directory, max_bytes=1024)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../etc/passwd", "passwd"),
        ("/tmp/thesis.pdf", "thesis.pdf"),
        ("my thesis (final).pdf", "my thesis _final_.pdf"),
        ("", "upload.pdf"),
        (None, "upload.pdf"),
        ("...", "upload.pdf"),
    ],
)
def test_filenames_cannot_escape_the_directory(raw, expected) -> None:
    assert safe_filename(raw) == expected


async def test_two_uploads_of_the_same_name_are_two_files(directory: Path) -> None:
    first = await store_upload(FakeUpload("a.pdf", PDF), directory=directory, max_bytes=1024)
    second = await store_upload(FakeUpload("a.pdf", PDF), directory=directory, max_bytes=1024)

    assert first.path != second.path
    assert len(list(directory.iterdir())) == 2


def test_the_upload_directory_is_random_and_stable_per_session(tmp_path: Path) -> None:
    sessions = InMemorySessionStore(60)
    one, two = sessions.create(), sessions.create()

    first = session_upload_dir(tmp_path, one)
    again = session_upload_dir(tmp_path, one)
    other = session_upload_dir(tmp_path, two)

    assert first == again
    assert first != other
    assert one.id not in str(first), "the session id is a credential; keep it off the disk"
    assert first.is_dir()


async def test_a_dribbled_non_pdf_is_still_refused(directory: Path) -> None:
    """`read()` may return fewer bytes than asked for, so the signature can arrive
    split across chunks. Judging the first chunk alone let a 2-byte-at-a-time body
    past the check."""
    with pytest.raises(UploadRejected, match="not a PDF"):
        await store_upload(
            FakeUpload("trojan.pdf", b"MZ\x90\x00\x03exe payload", chunk=2),
            directory=directory,
            max_bytes=1024,
        )

    assert list(directory.iterdir()) == [], "nothing is left on disk"


async def test_a_dribbled_pdf_is_accepted(directory: Path) -> None:
    """The buffering must not reject a real PDF that arrives in small reads."""
    stored = await store_upload(
        FakeUpload("thesis.pdf", PDF, chunk=2), directory=directory, max_bytes=1024
    )

    assert stored.size == len(PDF)
    assert stored.path.read_bytes() == PDF


async def test_a_file_too_short_to_be_a_pdf_is_refused(directory: Path) -> None:
    with pytest.raises(UploadRejected, match="not a PDF"):
        await store_upload(FakeUpload("tiny.pdf", b"%PD"), directory=directory, max_bytes=1024)

    assert list(directory.iterdir()) == []
