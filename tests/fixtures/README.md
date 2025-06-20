# Extraction fixtures

`*_front.txt` files are the text of a document's front matter, as PyMuPDF would
return it, and drive the tier-2 heuristic tests.

**The three shipped fixtures are synthetic** — invented titles, people, and
abstracts, hand-written to reproduce the *shapes* real ETDs come in (separate
committee page, run-in `ABSTRACT` heading, article with no committee). They are not
real SHAREOK records, so a passing test here is evidence the heuristics handle the
shape, not that they handle real documents.

To test against real documents, drop more `*_front.txt` files in beside them: the
heuristic tests pick up every fixture in this directory automatically, and
`tests/test_extract_heuristic.py` asserts per-fixture expectations only for the
named ones. Real ETD text may contain personal data — keep to what is already
public in the repository record, per the cross-phase conventions in
`docs/IMPLEMENTATION_PLAN.md`.
