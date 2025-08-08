"""Colour contrast, and ids that must stay unique. No browser required.

Contrast is arithmetic on the stylesheet, so it belongs in the fast suite rather
than behind `--e2e`: a colour picked in a hurry should fail before anyone
installs a browser. The pairs listed here are the ones the pages actually render;
adding a colour means adding it here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CSS = Path(__file__).resolve().parent.parent / "scholardrop" / "static" / "app.css"

BG = "#fbfaf8"  # --bg
WHITE = "#ffffff"  # cards, header, inputs

# name, foreground, background, minimum ratio (4.5 normal text, 3.0 for a border
# or a large/heavy element).
PAIRS = [
    ("body text", "#1b1b1d", BG, 4.5),
    ("muted text on the page", "#5d6067", BG, 4.5),
    ("muted text on a card", "#5d6067", WHITE, 4.5),
    ("links and the accent", "#7a1f1f", BG, 4.5),
    ("default badge", "#5d6067", "#eef0f4", 4.5),
    ("low-confidence badge", "#6b4a06", "#f6e7c8", 4.5),
    ("readiness: ready", "#1d5b28", "#dff0e0", 4.5),
    ("readiness: needs", "#6b4a06", "#f6e7c8", 4.5),
    ("readiness: problem", "#7c1d1d", "#f7dcdc", 4.5),
    ("danger button label", "#ffffff", "#8d1414", 4.5),
    ("ready row border", "#2f7d3b", WHITE, 3.0),
    # Border of the "something is missing" notice. 3:1 is the non-text
    # threshold, and the notice says what it means in words anyway -- the
    # colour is reinforcement, never the message.
    ("missing-notice border", "#b8860b", WHITE, 3.0),
]


def _channel(value: int) -> float:
    c = value / 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(colour: str) -> float:
    colour = colour.lstrip("#")
    r, g, b = (int(colour[i : i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast(foreground: str, background: str) -> float:
    a, b = luminance(foreground), luminance(background)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


def test_the_contrast_maths_is_right() -> None:
    """Anchored on the two ratios everyone knows, so a broken formula shows up."""
    assert contrast("#000000", "#ffffff") == pytest.approx(21.0, abs=0.01)
    assert contrast("#ffffff", "#ffffff") == pytest.approx(1.0, abs=0.01)


@pytest.mark.parametrize(("name", "foreground", "background", "minimum"), PAIRS)
def test_text_meets_wcag_aa(name: str, foreground: str, background: str, minimum: float) -> None:
    ratio = contrast(foreground, background)
    assert ratio >= minimum, f"{name}: {ratio:.2f}:1, needs {minimum}:1"


def test_every_colour_in_the_stylesheet_is_accounted_for() -> None:
    """A colour nobody checked is a colour nobody checked.

    Not a contrast test -- it cannot know which pairs meet -- but it fails when a
    new colour appears, which is the moment to add it to PAIRS above.
    """
    known = {c.lower() for _n, fg, bg, _m in PAIRS for c in (fg, bg)} | {
        "#d9dade",  # --line, borders only
        "#611818",  # .button:hover
        "#6f0f0f",  # .danger:hover
        "#f2efe9",  # .quiet-button:hover
        "#f7f6f3",  # licence text panel
    }
    found = {c.lower() for c in re.findall(r"#[0-9a-fA-F]{6}\b", CSS.read_text())}
    unaccounted = sorted(found - known)
    assert unaccounted == [], (
        f"new colour(s) {unaccounted} -- add them to PAIRS with the background "
        "they sit on, or to the known list if they are never behind text"
    )
