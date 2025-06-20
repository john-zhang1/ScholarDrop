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
from scholardrop.settings import Settings

log = logging.getLogger("scholardrop.extract")

JobState = Literal["running", "done", "failed"]


@dataclass
class ExtractionResult:
    metadata: ExtractedMetadata
    mapped: MappedMetadata


# The seam tests substitute (like client_factory): everything the route knows
# about running an extraction.
Extractor = Callable[..., Awaitable[ExtractionResult]]


async def run_extraction(
    path: Path,
    *,
    instance: Instance,
    settings: Settings,
    profile: MappingProfile,
) -> ExtractionResult:
    """Read the PDF, extract, and translate the result into metadata fields."""
    metadata, _document = await extract_from_pdf(path, instance=instance, settings=settings)
    return ExtractionResult(metadata=metadata, mapped=map_metadata(metadata, profile))


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

    def sweep(self, max_age_seconds: float) -> int:
        """Drop finished jobs older than the session TTL. Returns how many."""
        now = time.monotonic()
        stale = [
            key
            for key, job in self._jobs.items()
            if job.finished and now - (job.finished_at or job.started_at) > max_age_seconds
        ]
        for key in stale:
            del self._jobs[key]
        return len(stale)

    def __len__(self) -> int:
        return len(self._jobs)
