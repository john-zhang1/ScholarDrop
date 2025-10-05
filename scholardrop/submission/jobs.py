"""Extraction that runs while the user watches (DESIGN.md §6 step 5).

Uploading is synchronous -- the workspaceitem and the bitstream must exist before
the form can render -- but extraction can take a while, so it runs as a background
task and the browser polls. The rule from DESIGN.md §5 still holds here:
**extraction never blocks a submission**. A failed job is a job whose result is an
empty form and an honest notice, not an error page.

Jobs live in this process, keyed by (session id, workspaceitem id), so one user's
poll can never observe another's extraction.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from scholardrop.config import Instance, MappingProfile
from scholardrop.extract.mapper import MappedMetadata, map_metadata
from scholardrop.extract.models import ExtractedMetadata
from scholardrop.extract.pipeline import extract_from_pdf
from scholardrop.extract.reader import PdfUnreadable
from scholardrop.identifiers.ids import Identifier
from scholardrop.identifiers.resolvers import RecordNotFound, RegistryUnavailable, resolve
from scholardrop.settings import Settings

log = logging.getLogger("scholardrop.extract")

JobState = Literal["running", "done", "failed"]


@dataclass
class ExtractionResult:
    metadata: ExtractedMetadata
    mapped: MappedMetadata
    # What the registry step did, when there was one (DESIGN.md §13.1). A
    # sentence for the review page rather than an exception, because a DOI that
    # would not resolve must not cost the submitter their draft.
    registry_notice: str | None = None
    registry_used: bool = False


# The seam tests substitute (like client_factory): everything the route knows
# about running an extraction.
Extractor = Callable[..., Awaitable[ExtractionResult]]


async def run_extraction(
    path: Path | None = None,
    *,
    instance: Instance,
    settings: Settings,
    profile: MappingProfile,
    force_local: bool = False,
    identifier: Identifier | None = None,
    resolve_registry: bool = True,
) -> ExtractionResult:
    """Gather what is known about this submission, and shape it for the form.

    Up to two sources, run together because both are network-bound and the user
    is watching:

    * the **registry**, when an identifier was given and the repository's own
      provider did not already resolve it (`resolve_registry=False` says it did);
    * the **PDF**, when one was uploaded.

    Precedence is DESIGN.md §13.1's: registry metadata is authoritative and the
    document fills the fields it does not carry -- committee members, and often
    keywords and the abstract. `ExtractedMetadata.merged_with` is where that
    single rule lives.

    `force_local` is the submitter's "restricted content" answer, carried from the
    upload form: it keeps the *document* off any network (DESIGN.md §5). It does
    not stop identifier resolution -- a DOI is not document content, and resolving
    one says nothing about the file.
    """
    registry_notice: str | None = None
    registry = ExtractedMetadata()
    extracted = ExtractedMetadata()

    async def _registry() -> ExtractedMetadata:
        nonlocal registry_notice
        if identifier is None or not resolve_registry:
            return ExtractedMetadata()
        try:
            return await resolve(identifier, settings)
        except RecordNotFound as exc:
            log.info("no registry record for %s: %s", identifier.display, exc)
            registry_notice = (
                f"No record was found for {identifier.display}, so nothing came from the registry."
            )
        except RegistryUnavailable as exc:
            log.info("registry lookup failed for %s: %s", identifier.display, exc)
            registry_notice = (
                f"{identifier.display} could not be looked up just now, so nothing came "
                "from the registry."
            )
        return ExtractedMetadata()

    async def _pdf() -> ExtractedMetadata:
        if path is None:
            return ExtractedMetadata()
        metadata, _document = await extract_from_pdf(
            path, instance=instance, settings=settings, force_local=force_local
        )
        return metadata

    registry, extracted = await asyncio.gather(_registry(), _pdf())
    metadata = registry.merged_with(extracted)
    return ExtractionResult(
        metadata=metadata,
        mapped=map_metadata(metadata, profile),
        registry_notice=registry_notice,
        registry_used=not registry.is_empty(),
    )


@dataclass
class ExtractionJob:
    """One extraction, observable while it runs."""

    workspaceitem_id: str
    filename: str = ""
    state: JobState = "running"
    result: ExtractionResult | None = None
    error: str | None = None
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None

    @property
    def finished(self) -> bool:
        return self.state != "running"

    @property
    def metadata(self) -> ExtractedMetadata | None:
        return self.result.metadata if self.result else None

    @property
    def mapped(self) -> MappedMetadata | None:
        return self.result.mapped if self.result else None

    @property
    def registry_notice(self) -> str | None:
        """What to say about the identifier, when there is something to say."""
        return self.result.registry_notice if self.result else None

    @property
    def elapsed(self) -> float:
        return (self.finished_at or time.monotonic()) - self.started_at


class ExtractionJobs:
    """In-process registry of running and finished extractions.

    Single-worker, like the session store, and for the same reason (DESIGN.md
    §2). If ScholarDrop ever runs multi-worker, this and SessionStore move
    together.
    """

    def __init__(self) -> None:
        self._jobs: dict[tuple[str, str], ExtractionJob] = {}
        # Tasks are held so the event loop cannot garbage-collect a running one.
        self._tasks: set[asyncio.Task[None]] = set()

    def start(
        self,
        owner: str,
        workspaceitem_id: str,
        work: Awaitable[ExtractionResult],
        *,
        filename: str = "",
    ) -> ExtractionJob:
        job = ExtractionJob(workspaceitem_id=workspaceitem_id, filename=filename)
        self._jobs[(owner, workspaceitem_id)] = job
        task = asyncio.create_task(self._run(job, work))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    async def _run(self, job: ExtractionJob, work: Awaitable[ExtractionResult]) -> None:
        try:
            job.result = await work
            job.state = "done"
            log.info(
                "extraction for workspaceitem %s finished in %.1fs (tier %s)",
                job.workspaceitem_id,
                job.elapsed,
                job.result.metadata.tier_used,
            )
        except PdfUnreadable as exc:
            job.state = "failed"
            job.error = "That PDF could not be read, so nothing was extracted from it."
            log.info("extraction failed for workspaceitem %s: %s", job.workspaceitem_id, exc)
        except Exception as exc:  # a broken extractor must not break the flow
            job.state = "failed"
            job.error = "Extraction failed, so the form has not been pre-filled."
            log.exception("extraction crashed for workspaceitem %s: %s", job.workspaceitem_id, exc)
        finally:
            job.finished_at = time.monotonic()

    def get(self, owner: str, workspaceitem_id: str) -> ExtractionJob | None:
        return self._jobs.get((owner, workspaceitem_id))

    def forget(self, owner: str, workspaceitem_id: str) -> None:
        self._jobs.pop((owner, workspaceitem_id), None)

    def forget_owner(self, owner: str) -> None:
        for key in [k for k in self._jobs if k[0] == owner]:
            del self._jobs[key]

    def sweep(
        self, max_age_seconds: float, *, owner_is_live: Callable[[str], bool] | None = None
    ) -> int:
        """Drop finished jobs nobody can ask for again. Returns how many.

        A job belongs to a session, so once that session is gone its result is
        unreachable and can go immediately. While the session is alive the result
        is kept however long the user takes: sweeping on age alone would quietly
        un-prefill the review form of someone who uploaded, went to look something
        up, and came back -- and the form would claim extraction found nothing,
        which is exactly the silent failure the extraction contract forbids.

        `max_age_seconds` is the fallback for callers that cannot say which
        sessions are live.
        """
        now = time.monotonic()
        stale = []
        for key, job in self._jobs.items():
            if not job.finished:
                continue
            if owner_is_live is None:
                if now - (job.finished_at or job.started_at) > max_age_seconds:
                    stale.append(key)
            elif not owner_is_live(key[0]):
                stale.append(key)
        for key in stale:
            del self._jobs[key]
        return len(stale)

    def __len__(self) -> int:
        return len(self._jobs)
