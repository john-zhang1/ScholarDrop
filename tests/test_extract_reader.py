"""PDF reading. Fixtures are built with PyMuPDF so no binary blobs live in git."""

from __future__ import annotations

from pathlib import Path

import fitz
import pytest

from scholardrop.extract.reader import (
    BACK_PAGES,
    FRONT_PAGES,
    PdfUnreadable,
    looks_like_pdf,
    read_pdf,
)


def build_pdf(
    pages: list[str],
    *,
    docinfo: dict[str, str] | None = None,
    title_size: float | None = None,
    encrypt_with: str | None = None,
) -> bytes:
    document = fitz.open()
    for index, body in enumerate(pages):
        page = document.new_page()
        if index == 0 and title_size:
            page.insert_text((72, 100), "The Big Title Of This Work", fontsize=title_size)
            page.insert_text((72, 160), body, fontsize=11)
        else:
            page.insert_text((72, 100), body, fontsize=11)
    if docinfo:
        document.set_metadata(docinfo)
    if encrypt_with:
        return document.tobytes(
            encryption=fitz.PDF_ENCRYPT_AES_256, user_pw=encrypt_with, owner_pw=encrypt_with
        )
    return document.tobytes()


def test_reads_page_count_and_text() -> None:
    pdf = build_pdf(["page one text", "page two text"])

    document = read_pdf(pdf)

    assert document.page_count == 2
    assert "page one text" in document.front_text
    assert document.has_text is True
    assert document.encrypted is False


def test_reads_from_a_path(tmp_path: Path) -> None:
    path = tmp_path / "doc.pdf"
    path.write_bytes(build_pdf(["hello from disk"]))

    document = read_pdf(path)

    assert "hello from disk" in document.front_text


def test_front_text_is_capped_at_the_first_pages() -> None:
    pages = [f"marker page {i}" for i in range(20)]

    document = read_pdf(build_pdf(pages))

    assert document.page_count == 20
    assert "marker page 0" in document.front_text
    assert f"marker page {FRONT_PAGES - 1}" in document.front_text
    assert f"marker page {FRONT_PAGES}" not in document.front_text


def test_back_text_is_the_final_pages() -> None:
    pages = [f"marker page {i}" for i in range(20)]

    document = read_pdf(build_pdf(pages))

    assert f"marker page {20 - BACK_PAGES}" in document.back_text
    assert "marker page 0" not in document.back_text


def test_short_document_does_not_repeat_pages_between_front_and_back() -> None:
    pages = [f"marker page {i}" for i in range(3)]

    document = read_pdf(build_pdf(pages))

    assert document.back_text == "", "a 3-page document is entirely front matter"
    assert "marker page 2" in document.front_text


def test_text_for_llm_joins_front_and_back() -> None:
    document = read_pdf(build_pdf([f"page {i}" for i in range(12)]))

    combined = document.text_for_llm

    assert "page 0" in combined
    assert "page 11" in combined
    assert "end of document" in combined


def test_docinfo_is_read_and_stripped() -> None:
    pdf = build_pdf(["body"], docinfo={"title": "  A Title  ", "author": "Doe, Jane"})

    document = read_pdf(pdf)

    assert document.docinfo["title"] == "A Title"
    assert document.docinfo["author"] == "Doe, Jane"


def test_first_page_blocks_carry_font_sizes() -> None:
    document = read_pdf(build_pdf(["smaller body text"], title_size=24.0))

    blocks = document.first_page_blocks

    assert blocks, "expected text blocks from page 1"
    biggest = max(blocks, key=lambda b: b.max_size)
    assert "The Big Title" in biggest.text
    assert biggest.max_size > 20


def test_empty_pdf_has_no_text_but_does_not_raise() -> None:
    document = fitz.open()
    document.new_page()

    result = read_pdf(document.tobytes())

    assert result.page_count == 1
    assert result.has_text is False
    assert result.probably_scanned is True


def test_encrypted_pdf_returns_gracefully() -> None:
    pdf = build_pdf(["secret text"], encrypt_with="letmein")

    document = read_pdf(pdf)

    assert document.encrypted is True
    assert document.has_text is False
    assert document.front_text == ""


def test_pdf_with_empty_user_password_is_read() -> None:
    """ "Protected" ETDs often have an owner password only."""
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 100), "readable despite protection", fontsize=11)
    pdf = document.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="owner-only")

    result = read_pdf(pdf)

    assert result.encrypted is False
    assert "readable despite protection" in result.front_text


def test_not_a_pdf_raises() -> None:
    with pytest.raises(PdfUnreadable):
        read_pdf(b"this is definitely not a pdf")


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(PdfUnreadable):
        read_pdf(tmp_path / "nope.pdf")


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (b"%PDF-1.7\n...", True),
        (b"%PDF-", True),
        (b"PK\x03\x04", False),
        (b"<html>", False),
        (b"", False),
    ],
)
def test_magic_byte_check(content: bytes, expected: bool) -> None:
    assert looks_like_pdf(content) is expected
