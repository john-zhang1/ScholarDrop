"""Tier 2 extraction: embedded metadata, layout, and patterns (DESIGN.md §5).

Runs standalone (for an instance configured `extraction: heuristic`, e.g. one
handling embargoed material that must not leave the building) and as the fallback
when the LLM tier errors.

Confidences here are deliberately modest: XMP/DocInfo values were authored, so they
score higher than anything guessed from layout. Everything a heuristic infers lands
below LOW_CONFIDENCE so the review form flags it.
"""

from __future__ import annotations

import logging
import re

from scholardrop.extract.models import ExtractedMetadata, SourceTier, items, text
from scholardrop.extract.reader import PdfDocument, TextBlock

log = logging.getLogger("scholardrop.extract")

# --- confidences (see the module docstring for why these are low) -----------
XMP_CONFIDENCE = 0.75
# Below LOW_CONFIDENCE on purpose -- see _authors_from_metadata.
AUTHOR_METADATA_CONFIDENCE = 0.55
LAYOUT_CONFIDENCE = 0.45
SECTION_CONFIDENCE = 0.55
PATTERN_CONFIDENCE = 0.5

_ABSTRACT_HEADING = re.compile(r"^\s*abstract\b[:.]?\s*$", re.IGNORECASE | re.MULTILINE)
_KEYWORDS_LINE = re.compile(
    r"^\s*(?:key\s?words?|subject\s+(?:terms|headings))\b\s*[:.\-]?\s*(?P<body>.*)$",
    re.IGNORECASE | re.MULTILINE,
)
# A heading that ends the abstract section.
_NEXT_HEADING = re.compile(
    r"^\s*(?:table of contents|acknowledge?ments?|dedication|list of (?:tables|figures)"
    r"|chapter\s+(?:one|1|i)\b|introduction|key\s?words?|copyright|contents)\b",
    re.IGNORECASE,
)
_COMMITTEE_HEADING = re.compile(
    r"(?:thesis|dissertation|doctoral|examining|supervisory)\s+committee"
    r"|committee\s+members?"
    r"|approved\s+(?:by|for)\s+the",
    re.IGNORECASE,
)
_CHAIR_ROLE = re.compile(r"\b(chair|co-?chair|chairperson|advisor|adviser|supervisor)\b", re.I)
_PERSON_LINE = re.compile(
    r"^\s*(?:(?P<honorific>Dr|Prof(?:essor)?|Mr|Ms|Mrs)\.?\s+)?"
    r"(?P<name>[A-Z][\w.'\-]+(?:\s+[A-Z][\w.'\-]+){1,3})"
    r"\s*(?:,\s*(?P<role>[^,\n]{0,40}))?\s*$"
)
_DEGREE = re.compile(
    r"\b(Doctor of Philosophy|Master of (?:Science|Arts|Education|Fine Arts|Music)"
    r"|Ph\.?\s?D\.?|M\.?\s?S\.?|M\.?\s?A\.?|Ed\.?\s?D\.?|M\.?F\.?A\.?)\b"
)
_YEAR = re.compile(r"\b(19|20)\d{2}\b")
_DEPARTMENT = re.compile(
    r"^\s*(?:(?:the\s+)?(?:department|school|college|graduate program)\s+of\s+"
    r"(?P<name>[A-Z][^\n,]{2,60}))\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_NOISE = re.compile(
    r"^(?:by|a (?:thesis|dissertation)|submitted|in partial fulfillment|university|"
    r"copyright|all rights reserved|degree of|presented to|the graduate|"
    r"graduate (?:college|school|faculty))",
    re.IGNORECASE,
)


def extract(document: PdfDocument) -> ExtractedMetadata:
    """Best effort from a PDF with no LLM involved."""
    front = document.front_text
    result = ExtractedMetadata(tier_used=SourceTier.HEURISTIC)

    title = _title_from_metadata(document) or _title_from_layout(document.first_page_blocks)
    authors = _authors_from_metadata(document)
    if not authors.present:
        authors = _authors_from_title_page(front)
    advisor, committee = _committee(front)

    return result.model_copy(
        update={
            "title": title,
            "authors": authors,
            "advisor": advisor,
            "committee_members": committee,
            "keywords": _keywords(front),
            "abstract": _abstract(front),
            "date_issued": _year(front),
            "degree": _degree(front),
            "department": _department(front),
        }
    )


# --- embedded metadata ------------------------------------------------------


def _title_from_metadata(document: PdfDocument):
    candidate = document.docinfo.get("title") or _xmp_tag(document.xmp, "title")
    if candidate and _plausible_title(candidate):
        return text(candidate, XMP_CONFIDENCE, SourceTier.XMP)
    return None


def _authors_from_metadata(document: PdfDocument):
    raw = document.docinfo.get("author") or _xmp_tag(document.xmp, "creator")
    if not raw or "<" in raw:
        return items(None)
    # DocInfo authors are one string: "Doe, Jane" or "Jane Doe; Richard Roe".
    parts = [p.strip() for p in re.split(r"\s*(?:;|\band\b|&)\s*", raw) if p.strip()]
    # Deliberately below the review threshold, unlike the XMP *title*: a PDF's Author
    # field is very often the producer or the journal rather than the author -- a real
    # article in the assetstore reports "IJNC Editorial Committee". Worth pre-filling,
    # never worth trusting silently.
    return items(parts, AUTHOR_METADATA_CONFIDENCE, SourceTier.XMP)


_BY_LINE = re.compile(r"^\s*by\s*$", re.IGNORECASE)
_BY_INLINE = re.compile(r"^\s*by\s+(?P<name>[^\n]{4,80})$", re.IGNORECASE)


def _authors_from_title_page(front: str):
    """The name under the title page's "By" line.

    The commonest author pattern in an ETD, and not covered by XMP: DocInfo is
    routinely the producing software or the submitter's username. Names here are
    often set in capitals, so the all-caps rule used for committee lines does not
    apply -- the preceding "By" is evidence enough.
    """
    lines = front.splitlines()[:80]
    for index, raw in enumerate(lines):
        inline = _BY_INLINE.match(raw)
        if inline:
            if name := _author_name(inline.group("name")):
                return items([name], PATTERN_CONFIDENCE, SourceTier.HEURISTIC)
            continue
        if not _BY_LINE.match(raw):
            continue
        # "By" alone: the author is the next non-empty line, sometimes two names.
        found: list[str] = []
        for candidate in lines[index + 1 : index + 5]:
            if not candidate.strip():
                if found:
                    break
                continue
            name = _author_name(candidate)
            if name is None:
                break
            found.append(name)
        if found:
            return items(found, PATTERN_CONFIDENCE, SourceTier.HEURISTIC)
    return items(None)


# Named degrees only. A generic "1-4 capitals with dots" pattern also eats a middle
# initial, turning "Cortez, Daniel R." into "Cortez, Daniel".
_TRAILING_DEGREE = re.compile(
    r",\s*(?:B\.?S\.?c?|B\.?A\.?|M\.?S\.?c?|M\.?A\.?|Ph\.?\s?D\.?|Ed\.?\s?D\.?"
    r"|M\.?F\.?A\.?|J\.?D\.?|M\.?D\.?|M\.?B\.?A\.?)\.?\s*$",
    re.IGNORECASE,
)
# "Last, First M." is the form repositories want, so a comma has to survive.
_NAME_SHAPE = re.compile(r"^[A-Za-z][\w.'\-]*,?(?:\s+[A-Za-z][\w.'\-]*,?)+$")
# A title page prints the place under the author's name -- "Norman, Oklahoma" reads
# as a two-word name with a comma, exactly like "Doe, Jane".
_PLACE_TAIL = re.compile(
    r",\s*(?:Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware"
    r"|Florida|Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana"
    r"|Maine|Maryland|Massachusetts|Michigan|Minnesota|Mississippi|Missouri|Montana"
    r"|Nebraska|Nevada|New\s+Hampshire|New\s+Jersey|New\s+Mexico|New\s+York"
    r"|North\s+Carolina|North\s+Dakota|Ohio|Oklahoma|Oregon|Pennsylvania"
    r"|Rhode\s+Island|South\s+Carolina|South\s+Dakota|Tennessee|Texas|Utah|Vermont"
    r"|Virginia|Washington|West\s+Virginia|Wisconsin|Wyoming|D\.?C\.?"
    r"|U\.?S\.?A?\.?|United\s+States|Canada|England)\.?$",
    re.IGNORECASE,
)


def _author_name(value: str) -> str | None:
    # Keep trailing periods: they belong to initials ("Daniel R."), unlike the
    # punctuation _tidy strips from titles.
    name = re.sub(r"\s+", " ", value).strip().strip(",;: ")
    name = _TRAILING_DEGREE.sub("", name).strip(" ,")
    if not name or _NOT_A_PERSON.search(name) or _BANNER.search(name):
        return None
    if _YEAR.search(name) or _PLACE_TAIL.search(name):
        return None
    if not 2 <= len(name.split()) <= 5:
        return None
    if not _NAME_SHAPE.match(name):
        return None
    return name


def _xmp_tag(xmp: str | None, tag: str) -> str | None:
    """First value of a dc:* XMP element.

    The value patterns use `[^<]` throughout: a `.*?` capture happily runs past the
    end of an *empty* element and swallows the markup up to the next one, which
    produced a title of "</rdf:Alt> </dc:title> <dc:creator> ..." on a real
    dissertation. Only text between adjacent tags is ever a value.
    """
    if not xmp:
        return None
    # Step 1: isolate this element's own content, so nothing below can look inside a
    # sibling. Step 2: read the value out of that content only.
    element = re.search(rf"<dc:{tag}(?:\s[^>]*)?>(?P<inner>.*?)</dc:{tag}>", xmp, re.S)
    if element is None:
        return None
    inner = element.group("inner")

    listed = re.search(r"<rdf:li[^>]*>(?P<v>[^<]+)</rdf:li>", inner, re.S)
    raw = listed.group("v") if listed else inner
    if "<" in raw or ">" in raw:  # wrapper present but no usable text inside
        return None
    value = re.sub(r"\s+", " ", raw).strip()
    return value or None


def _plausible_title(value: str) -> bool:
    """Reject the junk PDF producers and publisher banners leave behind."""
    stripped = value.strip()
    lowered = stripped.lower()
    if len(lowered) < 6 or len(lowered) > 400:
        return False
    if lowered.endswith((".doc", ".docx", ".pdf", ".tex", ".indd")):
        return False
    # Markup means a metadata parse went wrong; never show it to the user as a title.
    if "<" in stripped or ">" in stripped:
        return False
    # Publisher stamps sit in the largest text block on many article PDFs, e.g.
    # "... All Rights Reserved. at UNIV OF OKLAHOMA LIBRARIES on January 20, 2016
    # jmd.sagepub.com Downloaded from" -- observed on real documents.
    if _BANNER.search(lowered):
        return False
    return lowered not in {"untitled", "microsoft word", "document", "thesis", "dissertation"}


_BANNER = re.compile(
    r"downloaded from|all rights reserved|rights reserved\.|sagepub|springer|elsevier"
    r"|https?://|www\.\S+|\.com\b|doi:|this content downloaded|jstor",
    re.IGNORECASE,
)


# --- layout ----------------------------------------------------------------


# A title that wraps is still one run of text. Anything up to triple the font size
# below the previous line is ordinary leading, including the double spacing a
# university's ETD template mandates.
_LINE_GAP_RATIO = 3.0


def _merge_wrapped_lines(blocks: list[TextBlock]) -> list[TextBlock]:
    """Join consecutive blocks that are lines of the same run of text.

    PyMuPDF returns one block per line on a title page typeset in Word, so a title
    that wraps arrives as two blocks and picking "the biggest block" truncates it
    at the line break. Same font size plus a gap no wider than normal leading is
    what "these belong together" looks like on a title page.
    """
    merged: list[TextBlock] = []
    previous_tops: list[float] = []

    for block in sorted(blocks, key=lambda b: b.top):
        if merged and _continues_previous(merged[-1], previous_tops[-1], block):
            group = merged[-1]
            merged[-1] = TextBlock(
                text=f"{group.text} {block.text}",
                max_size=group.max_size,
                top=group.top,
            )
            previous_tops[-1] = block.top
        else:
            merged.append(block)
            previous_tops.append(block.top)
    return merged


def _continues_previous(group: TextBlock, last_top: float, block: TextBlock) -> bool:
    gap = block.top - last_top
    return (
        round(group.max_size, 1) == round(block.max_size, 1)
        and 0 < gap <= block.max_size * _LINE_GAP_RATIO
    )


def _title_from_layout(blocks: list[TextBlock]):
    """The title block on page 1 -- when the page says which block that is."""
    candidates = [
        block
        for block in _merge_wrapped_lines(blocks)
        if 2 <= block.word_count <= 40
        and not _NOISE.match(block.text)
        and _plausible_title(block.text)
    ]
    if not candidates:
        return text(None)

    sizes = {round(b.max_size, 1) for b in blocks}
    if len(sizes) > 1:
        biggest = max(candidates, key=lambda b: (b.max_size, -b.top))
        return text(_tidy(biggest.text), LAYOUT_CONFIDENCE, SourceTier.HEURISTIC)

    # One font size across the whole page -- a Word-typeset ETD title page -- means
    # the layout carries no signal about which block is the title, and "biggest,
    # then topmost" degenerates into picking off the letterhead. One survivor of
    # the boilerplate filter is still an answer; several is a coin toss, and a
    # wrong title that looks authoritative is worse than an empty field the
    # reviewer fills in. Same rule tier 1's prompt follows.
    if len(candidates) > 1:
        log.info(
            "page 1 has no typographic hierarchy and %d title candidates; no title guessed",
            len(candidates),
        )
        return text(None)
    return text(_tidy(candidates[0].text), LAYOUT_CONFIDENCE - 0.15, SourceTier.HEURISTIC)


# --- section slicing -------------------------------------------------------


def _abstract(front: str):
    match = _ABSTRACT_HEADING.search(front)
    if match is None:
        # Also allow a run-in heading: "ABSTRACT  This dissertation ..."
        inline = re.search(r"\bABSTRACT\b[:.\s]{1,4}(?=[A-Z][^\n]{40,})", front)
        if inline is None:
            return text(None)
        # Slice from the body onwards, not just its first line -- the abstract
        # continues over many lines and ends at the next heading.
        body = front[inline.end() :]
    else:
        body = front[match.end() :]

    lines: list[str] = []
    for line in body.splitlines():
        if _NEXT_HEADING.match(line):
            break
        lines.append(line.strip())
        if sum(len(candidate) for candidate in lines) > 5000:
            break
    paragraph = _tidy(" ".join(lines))
    if len(paragraph) < 40:  # too short to be an abstract
        return text(None)
    return text(paragraph, SECTION_CONFIDENCE, SourceTier.HEURISTIC)


def _keywords(front: str):
    match = _KEYWORDS_LINE.search(front)
    if match is None:
        return items(None)
    body = match.group("body").strip()
    # A keyword list routinely wraps over several lines and ends at a blank line,
    # so keep reading rather than taking the first line only. `[1:]` drops the
    # remainder of the matched line itself, which is empty (the regex ends at `$`).
    for line in front[match.end() :].split("\n")[1:]:
        stripped = line.strip()
        if not stripped or _NEXT_HEADING.match(stripped):
            break
        body = f"{body} {stripped}".strip()
        if len(body) > 500:
            break
    parts = [p.strip(" .;") for p in re.split(r"[;,]|\s{3,}", body) if p.strip(" .;")]
    parts = [p for p in parts if 2 <= len(p) <= 60]
    return items(parts, SECTION_CONFIDENCE, SourceTier.HEURISTIC)


# --- committee / advisor ---------------------------------------------------


def _committee(front: str):
    """Names following a committee heading, split into chair/advisor and members.

    Non-person lines are *skipped*, not treated as the end of the list: real title
    pages put institutional lines between the heading and the names
    ("...APPROVED FOR THE / SCHOOL OF GEOSCIENCES / BY THE COMMITTEE CONSISTING OF").
    Only a genuine next section, or a long run of nothing, stops the scan.
    """
    match = _COMMITTEE_HEADING.search(front)
    if match is None:
        return text(None), items(None)

    advisor: str | None = None
    members: list[str] = []
    misses = 0
    window = front[match.end() : match.end() + 1500]
    for raw_line in window.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if _NEXT_HEADING.match(line):
            break
        name = _person_name(line)
        if name is None:
            misses += 1
            # Give up once the names are clearly behind us.
            if misses > 8 or (members or advisor) and misses > 2:
                break
            continue
        misses = 0
        role = _PERSON_LINE.match(line)
        role_text = (role.group("role") if role else None) or ""
        if (_CHAIR_ROLE.search(role_text) or _CHAIR_ROLE.search(line)) and advisor is None:
            advisor = name
            continue
        members.append(name)

    return (
        text(advisor, PATTERN_CONFIDENCE, SourceTier.HEURISTIC),
        items(members, PATTERN_CONFIDENCE, SourceTier.HEURISTIC),
    )


# Words that mark a line as institutional rather than a person.
_NOT_A_PERSON = re.compile(
    r"\b(?:university|college|school|department|committee|faculty|graduate|program|"
    r"office|institute|division|centre|center|approved|consisting|submitted|"
    r"requirements|degree|thesis|dissertation|"
    # Copyright boilerplate follows the committee list on many title pages, and
    # "All Rights Reserved" is three capitalised words -- observed being extracted
    # as a committee member.
    r"copyright|rights|reserved)\b",
    re.IGNORECASE,
)


def _person_name(line: str) -> str | None:
    """The person named on this line, or None if it is not a name.

    An all-caps line is a heading in this kind of document, not a name -- that one
    rule rejects most institutional boilerplate on a title page.
    """
    match = _PERSON_LINE.match(line)
    if match is None:
        return None
    name = _tidy(match.group("name"))
    # Test the *name*, not the whole line: a valid person line often carries an
    # institutional word in its role ("Dr. Ingrid Solberg, Committee Chair").
    if _NOT_A_PERSON.search(name):
        return None
    words = name.split()
    if not 2 <= len(words) <= 4:
        return None
    has_honorific = bool(match.group("honorific"))
    if name.isupper() and not has_honorific:
        return None
    return name


# --- small fields ----------------------------------------------------------


def _degree(front: str):
    match = _DEGREE.search(front)
    return text(match.group(0) if match else None, PATTERN_CONFIDENCE, SourceTier.HEURISTIC)


def _department(front: str):
    match = _DEPARTMENT.search(front)
    if match is None:
        return text(None)
    return text(_tidy(match.group(0)), PATTERN_CONFIDENCE, SourceTier.HEURISTIC)


def _year(front: str):
    """The degree/publication year from the title page.

    "Last year seen" is wrong more often than it looks -- on a real dissertation it
    picked up 1994 from body text further down the page. Prefer the year the title
    page repeats most (a title page usually states it in the date line and the
    copyright line), tie-breaking to the later one, and ignore anything implausible.
    """
    from collections import Counter
    from datetime import date

    window = front[:2000]
    candidates = [m.group(0) for m in _YEAR.finditer(window)]
    upper = date.today().year + 1
    candidates = [y for y in candidates if 1800 <= int(y) <= upper]
    if not candidates:
        return text(None)
    counts = Counter(candidates)
    best = max(counts.items(), key=lambda item: (item[1], int(item[0])))[0]
    # A repeated year is a much better bet than a lone one.
    confidence = PATTERN_CONFIDENCE - (0.1 if counts[best] > 1 else 0.2)
    return text(best, confidence, SourceTier.HEURISTIC)


def _tidy(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" .,:;")
