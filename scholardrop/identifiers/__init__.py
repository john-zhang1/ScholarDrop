"""Identifier-first submission (DESIGN.md §13.1).

Three pieces, kept apart on purpose:

* `ids` parses what a person pasted into a normalised identifier. It knows
  nothing about the network.
* `sources` asks the *repository* to resolve it -- DSpace's own live-import
  providers, which map the registry record with the repository's configuration
  rather than ours. This is the preferred path.
* `resolvers` is what runs when the repository has no provider for it (or the
  provider fails): ScholarDrop asks the registry itself and returns the same
  neutral `ExtractedMetadata` shape the PDF pipeline produces.

Nothing here knows about Dublin Core -- the same rule the extractor lives by.
Registry values come back as neutral fields, and the instance's mapping profile
decides where they land.
"""

from __future__ import annotations

from scholardrop.identifiers.ids import (
    Identifier,
    IdentifierKind,
    UnknownIdentifier,
    parse_identifier,
)

__all__ = [
    "Identifier",
    "IdentifierKind",
    "UnknownIdentifier",
    "parse_identifier",
]
