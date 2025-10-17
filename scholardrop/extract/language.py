"""Which language a document is written in (DESIGN.md §13.4).

Small on purpose. This exists to save somebody a dropdown, not to be a language
identifier: it answers with an ISO 639-1 code when the evidence is plain and with
nothing at all when it is not, because a wrong `dc.language.iso` sitting in a
pre-filled form is worse than an empty one somebody has to fill in.

Two stages, in order of how much they can be trusted:

* **script** -- text in Han, kana, Hangul, Cyrillic, Arabic, Devanagari, Greek or
  Hebrew answers itself. Kana settles Japanese against Chinese, which sharing the
  Han block otherwise would not;
* **function words** -- for Latin script, the handful of words a language cannot
  write a paragraph without. A winner has to be both frequent enough and clearly
  ahead of the runner-up, or this returns nothing.

No dependency: the identification libraries are large, and the accuracy they buy
over "is this obviously English" is accuracy this feature cannot spend. SHAREOK's
non-English material is mostly Chinese and Spanish, both of which the first stage
or a wide margin settles.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter

# What the translation picker offers and what detection may answer with. ISO
# 639-1, which is what DSpace's own `common_iso_languages` vocabulary uses.
LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "it": "Italian",
    "pt": "Portuguese",
    "nl": "Dutch",
    "zh": "Chinese",
    "ja": "Japanese",
    "ko": "Korean",
    "ru": "Russian",
    "ar": "Arabic",
    "hi": "Hindi",
    "el": "Greek",
    "he": "Hebrew",
}

# Detection is layout- and pattern-matching, so its values carry a tier-2
# confidence: visibly flagged on the review form, exactly like a guessed title.
DETECTED_CONFIDENCE = 0.55

# How much is looked at. A title page plus an abstract is more than enough, and
# reading a whole thesis to decide it is in English is time somebody is waiting
# through. Both bounds are needed: a language written without spaces between its
# words is one "word" however long the document is.
SAMPLE_WORDS = 2000
SAMPLE_CHARS = 20_000
# A non-Latin script this much of the letters wins outright.
SCRIPT_SHARE = 0.2
# A Latin-script winner needs this many function-word hits, and this much more
# than the language behind it.
MIN_HITS = 4
MIN_MARGIN = 1.5

_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)

# The words a language cannot write a paragraph without. Overlaps between them
# ("de", "la", "in") are why the margin rule exists.
FUNCTION_WORDS: dict[str, frozenset[str]] = {
    "en": frozenset("the of and in to is that for with this are was were as by".split()),
    "es": frozenset("el la de que en los del las por una para con no se es".split()),
    "fr": frozenset("le la les des et en du une dans pour est sur par aux".split()),
    "de": frozenset("der die das und den von mit für ist nicht eine dem auch als".split()),
    "it": frozenset("il la di che per non una con del sono nel alla gli".split()),
    "pt": frozenset("que do da em para com uma não os as pelo pela dos".split()),
    "nl": frozenset("het een van en dat is op voor niet zijn met aan door".split()),
}

# Unicode script -> language, for the writing systems that answer the question by
# themselves. `HAN` is deliberately absent: it is settled below, by kana.
_SCRIPTS: tuple[tuple[str, str], ...] = (
    ("HANGUL", "ko"),
    ("CYRILLIC", "ru"),
    ("ARABIC", "ar"),
    ("DEVANAGARI", "hi"),
    ("GREEK", "el"),
    ("HEBREW", "he"),
)


def detect_language(text: str) -> tuple[str, float]:
    """The document's language as an ISO 639-1 code, or `("", 0.0)` if unsure."""
    sample = " ".join(text.split()[:SAMPLE_WORDS])[:SAMPLE_CHARS]
    if not sample.strip():
        return "", 0.0

    by_script = _script_counts(sample)
    letters = sum(by_script.values())
    if not letters:
        return "", 0.0

    han = by_script.get("HAN", 0)
    kana = by_script.get("HIRAGANA", 0) + by_script.get("KATAKANA", 0)
    if (han + kana) / letters >= SCRIPT_SHARE:
        # Japanese writes Han characters too, but Chinese does not write kana.
        return ("ja" if kana else "zh"), DETECTED_CONFIDENCE
    for script, code in _SCRIPTS:
        if by_script.get(script, 0) / letters >= SCRIPT_SHARE:
            return code, DETECTED_CONFIDENCE

    return _by_function_words(sample)


def _script_counts(sample: str) -> Counter[str]:
    """Letters by writing system, read off each character's Unicode name.

    The name's first word is the script for everything here except Han, whose
    characters are named "CJK UNIFIED IDEOGRAPH-xxxx" -- and reading that as a
    script called "CJK" is how Chinese first came back as "no idea".
    """
    counts: Counter[str] = Counter()
    for char in sample:
        if not char.isalpha():
            continue
        try:
            name = unicodedata.name(char)
        except ValueError:  # unnamed character
            continue
        script = name.split(" ", 1)[0]
        counts["HAN" if script == "CJK" else script] += 1
    return counts


def _by_function_words(sample: str) -> tuple[str, float]:
    words = [w.lower() for w in _WORD.findall(sample)]
    if not words:
        return "", 0.0

    scores = {
        code: sum(1 for word in words if word in vocabulary)
        for code, vocabulary in FUNCTION_WORDS.items()
    }
    ranked = sorted(scores.items(), key=lambda pair: pair[1], reverse=True)
    best, best_score = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0

    if best_score < MIN_HITS:
        return "", 0.0
    # A tie between two languages that share half their function words is not an
    # answer, and guessing between them is how the wrong one gets deposited.
    if runner_up and best_score < runner_up * MIN_MARGIN:
        return "", 0.0
    return best, DETECTED_CONFIDENCE
