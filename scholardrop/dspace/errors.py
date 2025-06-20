"""Exceptions raised by DSpaceClient.

Two rules shape this hierarchy:

* An *authorization* 403 is a domain event, not a bug (DESIGN.md §4). It gets its
  own type carrying enough context for the access-denied screen.
* ACL *questions* never raise -- can_submit() returns a bool.
"""

from __future__ import annotations


class DSpaceError(Exception):
    """Base for every DSpace interaction failure."""

    def __init__(
        self,
        message: str,
        *,
        method: str | None = None,
        path: str | None = None,
        status: int | None = None,
    ) -> None:
        self.method = method
        self.path = path
        self.status = status
        detail = " ".join(
            part for part in (method, path, f"-> {status}" if status else None) if part
        )
        super().__init__(f"{message} ({detail})" if detail else message)


class DSpaceAuthError(DSpaceError):
    """Bad credentials, or a token that could not be refreshed.

    The user must sign in again. Never carries the credential or the token.
    """


class DSpaceForbidden(DSpaceError):
    """DSpace refused the operation for this EPerson.

    Raised where the answer is "you may not do that" -- e.g. creating a
    workspaceitem in a collection the user cannot submit to. Routes turn this
    into the access-denied screen (DESIGN.md §4), so keep the context useful.
    """

    def __init__(
        self,
        message: str,
        *,
        method: str | None = None,
        path: str | None = None,
        status: int | None = 403,
        resource: str | None = None,
        detail: str | None = None,
    ) -> None:
        self.resource = resource
        self.detail = detail
        super().__init__(message, method=method, path=path, status=status)


class DSpaceNotFound(DSpaceError):
    """The object is not there any more (or never was).

    Normal in this flow rather than exceptional: a discarded draft's URL stays in
    the browser's history, and a draft deposited or removed in the DSpace UI is
    gone from ScholarDrop's point of view too.
    """


class ValidationIssue:
    """One entry from a workspaceitem `errors` array.

    `message` is a DSpace i18n key (e.g. "error.validation.required"); `paths`
    point at the offending section fields
    (e.g. "/sections/traditionalpageone/dc.title").
    """

    __slots__ = ("message", "paths")

    def __init__(self, message: str, paths: list[str] | None = None) -> None:
        self.message = message
        self.paths = paths or []

    @classmethod
    def from_payload(cls, payload: dict) -> ValidationIssue:
        raw_paths = payload.get("paths") or []
        paths = [p for p in raw_paths if isinstance(p, str)]
        return cls(message=str(payload.get("message", "")), paths=paths)

    def fields(self) -> list[str]:
        """Section-qualified field names, e.g. 'traditionalpageone/dc.title'."""
        return [p.removeprefix("/sections/") for p in self.paths]

    def __repr__(self) -> str:
        return f"ValidationIssue(message={self.message!r}, paths={self.paths!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ValidationIssue):
            return NotImplemented
        return (self.message, self.paths) == (other.message, other.paths)


class DSpaceValidationError(DSpaceError):
    """DSpace rejected a submission because the item is incomplete or invalid.

    Carries the parsed `errors` array so the review form can flag the exact
    fields instead of showing a generic failure (DESIGN.md §7).
    """

    def __init__(
        self,
        message: str,
        *,
        issues: list[ValidationIssue] | None = None,
        method: str | None = None,
        path: str | None = None,
        status: int | None = None,
    ) -> None:
        self.issues = issues or []
        super().__init__(message, method=method, path=path, status=status)

    def field_messages(self) -> dict[str, list[str]]:
        """{'traditionalpageone/dc.title': ['error.validation.required'], ...}"""
        out: dict[str, list[str]] = {}
        for issue in self.issues:
            for field in issue.fields():
                out.setdefault(field, []).append(issue.message)
        return out


class DSpaceUnavailable(DSpaceError):
    """DSpace could not be reached, or returned 5xx."""
