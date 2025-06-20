"""DSpace REST API client (DESIGN.md §3)."""

from scholardrop.dspace.client import DSpaceClient
from scholardrop.dspace.errors import (
    DSpaceAuthError,
    DSpaceError,
    DSpaceForbidden,
    DSpaceNotFound,
    DSpaceUnavailable,
    DSpaceValidationError,
    ValidationIssue,
)
from scholardrop.dspace.models import (
    AuthStatus,
    Collection,
    CollectionLicense,
    Community,
    DepositResult,
    DSpaceObject,
    EPerson,
    Page,
    PageInfo,
    VocabularyEntry,
    WorkflowItem,
    WorkspaceItem,
)

__all__ = [
    "AuthStatus",
    "Collection",
    "CollectionLicense",
    "Community",
    "DSpaceAuthError",
    "DSpaceClient",
    "DSpaceError",
    "DSpaceForbidden",
    "DSpaceObject",
    "DSpaceNotFound",
    "DSpaceUnavailable",
    "DSpaceValidationError",
    "DepositResult",
    "EPerson",
    "Page",
    "PageInfo",
    "ValidationIssue",
    "VocabularyEntry",
    "WorkflowItem",
    "WorkspaceItem",
]
