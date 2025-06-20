"""PDF reading with PyMuPDF (DESIGN.md §5).

Both tiers read from the same `PdfDocument`: the LLM gets the text, the heuristics
also need the page-1 layout (font sizes) to guess a title.

Nothing here raises for a document that simply has no text -- a scanned thesis is a
real submission, and "extraction found nothing" is a valid outcome that the review
form handles by showing an empty form with a notice.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import fitz  # PyMuPDF

log = logging.getLogger("scholardrop.extract")

FRONT_PAGES = 8
BACK_PAGES = 2
PDF_MAGIC = b"%PDF-"


class PdfUnreadable(Exception):
    """The file is not a PDF, or is too damaged for PyMuPDF to open."""


@dataclass(frozen=True)
class TextBlock:
    """A block of page text with the largest font size it uses."""

    text: str
    max_size: float
    top: float

    @property
    def word_count(self) -> int:
        return len(self.text.split())


@dataclass
class PdfDocument:
    page_count: int = 0
    encrypted: bool = False
    docinfo: dict[str, str] = field(default_factory=dict)
    xmp: str | None = None
    front_text: str = ""
    back_text: str = ""
    first_page_blocks: list[TextBlock] = field(default_factory=list)

    @property
    def has_text(self) -> bool:
        return bool(self.front_text.strip() or self.back_text.strip())

    @property
    def probably_scanned(self) -> bool:
        """Pages exist but carry almost no text -- images of text, most likely."""
        return self.page_count > 0 and len(self.front_text.strip()) < 100

    @property
    def text_for_llm(self) -> str:
        """Front matter plus the tail, which is where ETD front/back matter lives."""
        if not self.back_text.strip():
            return self.front_text
        return f"{self.front_text}\n\n[... end of document ...]\n\n{self.back_text}"


def looks_like_pdf(content: bytes) -> bool:
    """Magic-byte check -- what upload validation should use, not the extension."""
    return content[:5] == PDF_MAGIC


def read_pdf(source: Path | str | bytes) -> PdfDocument:
    """Open a PDF and pull out what both extraction tiers need."""
    try:
        if isinstance(source, bytes):
            document = fitz.open(stream=source, filetype="pdf")
        else:
            document = fitz.open(source)
    except Exception as exc:  # PyMuPDF raises a variety of types
        raise PdfUnreadable(f"could not open PDF: {exc}") from exc

    with document:
        encrypted = bool(document.needs_pass)
        if encrypted:
            # An empty user password is common for "protected" ETDs.
            if document.authenticate(""):
                encrypted = False
            else:
                log.info("PDF is password-protected; no text available")
                return PdfDocument(
                    page_count=document.page_count,
                    encrypted=True,
                    docinfo=_docinfo(document),
                )

        page_count = document.page_count
        front = _page_range_text(document, range(min(FRONT_PAGES, page_count)))
        back_start = max(FRONT_PAGES, page_count - BACK_PAGES)
        back = _page_range_text(document, range(back_start, page_count))

        blocks: list[TextBlock] = []
        if page_count:
            blocks = _blocks_with_sizes(document[0])

        doc = PdfDocument(
            page_count=page_count,
            encrypted=False,
            docinfo=_docinfo(document),
            xmp=_xmp(document),
            front_text=front,
            back_text=back,
            first_page_blocks=blocks,
        )

    if not doc.has_text:
        log.info("PDF has %d page(s) but no extractable text (scanned?)", doc.page_count)
    return doc


def _docinfo(document: fitz.Document) -> dict[str, str]:
    raw = document.metadata or {}
    return {k: v.strip() for k, v in raw.items() if isinstance(v, str) and v.strip()}


def _xmp(document: fitz.Document) -> str | None:
    try:
        data = document.get_xml_metadata()
    except Exception:  # not all builds/documents support it
        return None
    return data or None


def _page_range_text(document: fitz.Document, pages: range) -> str:
    chunks = []
    for number in pages:
        try:
            chunks.append(document[number].get_text("text"))
        except Exception as exc:
            log.info("could not read page %d: %s", number, exc)
    return "\n".join(chunks).strip()


def _blocks_with_sizes(page: fitz.Page) -> list[TextBlock]:
    """Page text as blocks, each tagged with its largest font size.

    The heuristic title guess needs this: on a title page the title is usually the
    largest text that is not a single word.
    """
    try:
        payload = page.get_text("dict")
    except Exception as exc:
        log.info("could not read page layout: %s", exc)
        return []

    blocks: list[TextBlock] = []
    for block in payload.get("blocks", []):
        if block.get("type") != 0:  # 0 = text
            continue
        lines: list[str] = []
        max_size = 0.0
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            line_text = "".join(span.get("text", "") for span in spans)
            if line_text.strip():
                lines.append(line_text.strip())
            for span in spans:
                max_size = max(max_size, float(span.get("size", 0.0)))
        if lines:
            bbox = block.get("bbox") or (0, 0, 0, 0)
            blocks.append(
                TextBlock(text=" ".join(lines), max_size=round(max_size, 2), top=float(bbox[1]))
            )
    return blocks
