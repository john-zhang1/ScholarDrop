"""What a screen reader is handed, asserted from Chromium's accessibility tree.

The previous pass checked that controls *have* names. This checks what those names
actually are, because "has a name" and "has a name worth hearing" are different
tests and only the second one catches `textbox "Title REQUIRED dc.title"`.

It is still not a screen reader. It cannot tell you whether the page makes sense
read aloud, which is why `AX_TRANSCRIPTS=1 pytest --e2e` writes
`docs/screen-reader-transcripts.md` for a person to read.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from tests.e2e.ax import named_controls, transcript

pytestmark = pytest.mark.e2e

TRANSCRIPTS = Path(__file__).resolve().parents[2] / "docs" / "screen-reader-transcripts.md"

# Things that belong on screen but not in an accessible name: the metadata field
# name is scaffolding for staff, and "required" is a state, not a word.
NAME_NOISE = re.compile(r"\bdc\.[a-z]|\bthesis\.[a-z]|\bREQUIRED\b")


@pytest.fixture
def review_page(signed_in_page, live_app, fake_dspace):
    from tests.fakes import make_collection, make_workspaceitem, upload_file

    fake_dspace.collections_by_uuid["c1"] = make_collection("c1", "OU - Dissertations")
    # With a file attached, so the transcript covers the access picker too --
    # "who may read this file" is exactly the kind of wording a person has to
    # judge by ear (DESIGN.md §13.5).
    fake_dspace.workspaceitems["42"] = make_workspaceitem("42", files=[upload_file("thesis.pdf")])
    page = signed_in_page
    page.goto(f"{live_app}/review/42")
    page.wait_for_selector("form input[name*='dc.title']")
    return page


@pytest.fixture
def drafts_page(signed_in_page, live_app, fake_dspace):
    from tests.fakes import make_workspaceitem

    fake_dspace.workspaceitems = {
        "7": make_workspaceitem("7"),
        "8": make_workspaceitem("8", errors=[]),
    }
    page = signed_in_page
    page.goto(f"{live_app}/drafts")
    page.wait_for_selector("#draft-7")
    return page


def test_no_control_reads_out_a_metadata_field_name(review_page) -> None:
    """ "Title REQUIRED dc.title" is what this form used to announce."""
    offenders = [
        f"{role} {name!r}" for role, name in named_controls(review_page) if NAME_NOISE.search(name)
    ]

    assert offenders == [], f"scaffolding in the accessible name: {offenders}"


def test_being_required_is_a_state_not_a_word(review_page) -> None:
    lines = transcript(review_page)
    title = next(line for line in lines if 'textbox "Title"' in line)

    assert "required" in title, "the state must reach the accessibility tree"
    assert "REQUIRED" not in title, "and must not also be read as part of the name"


def test_the_form_reads_in_the_order_it_is_laid_out(review_page) -> None:
    """Sections announce as named groups, in the collection's own order."""
    groups = [line.strip() for line in transcript(review_page) if line.strip().startswith("group ")]

    assert groups[:2] == [
        'group "Describe your submission"',
        'group "Describe your submission (continued)"',
    ]
    assert 'group "Deposit licence"' in groups


def test_two_drafts_never_offer_identically_named_actions(drafts_page) -> None:
    """Tabbing a list of untitled drafts used to give "Resume, Resume"."""
    names = [f"{role}:{name}" for role, name in named_controls(drafts_page) if name]
    repeated = sorted({n for n in names if names.count(n) > 1})

    assert repeated == [], f"indistinguishable controls: {repeated}"


def test_the_discard_confirmation_says_which_draft(drafts_page, live_app) -> None:
    page = drafts_page
    page.locator("#draft-7 button:has-text('Discard')").click()
    page.wait_for_selector("#draft-7 .draft-actions .danger")

    confirm = next(line for line in transcript(page) if "Discard draft 7 permanently" in line)
    assert "Discard permanently?" in confirm, "the question must reach the button"


def test_the_landmarks_come_in_the_expected_order(drafts_page) -> None:
    order = [
        line.strip().split(" ", 1)[0]
        for line in transcript(drafts_page)
        if line.strip().split(" ", 1)[0] in {"banner", "main", "contentinfo"}
    ]

    assert order == ["banner", "main", "contentinfo"]


def test_the_skip_link_is_the_first_thing_offered(drafts_page) -> None:
    first = named_controls(drafts_page)[0]

    assert first == ("link", "Skip to the main content")


def test_the_readiness_blockers_are_readable_without_the_badge(drafts_page) -> None:
    """A blocker is prose, not a colour with a tooltip."""
    text = "\n".join(transcript(drafts_page))

    assert "Title — This field is required." in text
    assert "Needs 3" in text


def test_write_the_transcripts_for_a_person_to_read(
    signed_in_page, live_app, fake_dspace, review_page
) -> None:
    """Not really a test: the artifact a human judgement needs.

    Kept in the suite rather than in scripts/ because it needs exactly the fixtures
    the browser tests already build. Writes only when asked.
    """
    from tests.fakes import make_workspaceitem

    # Deliberately not SCHOLARDROP_-prefixed: the root conftest strips that
    # whole namespace so tests never inherit a developer's environment.
    if not os.environ.get("AX_TRANSCRIPTS"):
        pytest.skip("set AX_TRANSCRIPTS=1 to regenerate")

    from tests.fakes import upload_file

    fake_dspace.workspaceitems = {
        "7": make_workspaceitem("7"),
        "8": make_workspaceitem("8", errors=[]),
        # The review form is transcribed with a file attached, so the access
        # picker is in it: "who may read this file" and "restricted until" are
        # wording a person has to judge by ear (DESIGN.md §13.5).
        "42": make_workspaceitem("42", files=[upload_file("thesis.pdf")]),
    }
    page = signed_in_page
    pages = [
        ("The review form", f"{live_app}/review/42", "form input[name*='dc.title']"),
        ("Your drafts", f"{live_app}/drafts", "#draft-7"),
        ("Choosing a collection", f"{live_app}/browse", "#tab-panel"),
    ]

    out = [
        "# What a screen reader is handed",
        "",
        "Chromium's accessibility tree for each page, in reading order — the data",
        "NVDA, VoiceOver and Orca phrase in their own words. Roles, names, states and",
        "descriptions are exactly what they read; the wording of the announcement is",
        "theirs.",
        "",
        "**This is for judgement, not for verification.** The tests pin the properties;",
        "what nobody can automate is whether this makes sense heard rather than seen.",
        "Read it and say where it does not.",
        "",
        "Regenerate with:",
        "",
        "```bash",
        "AX_TRANSCRIPTS=1 .venv/bin/python -m pytest --e2e \\",
        "    tests/e2e/test_screen_reader.py -k transcripts",
        "```",
        "",
        "Generated against the test fakes, so the content is fixture data; the",
        "structure is the real thing.",
        "",
    ]
    for title, url, ready in pages:
        page.goto(url)
        page.wait_for_selector(ready)
        out += [f"## {title}", "", "```", *transcript(page), "```", ""]

    TRANSCRIPTS.write_text("\n".join(out), encoding="utf-8")
    assert TRANSCRIPTS.stat().st_size > 500
