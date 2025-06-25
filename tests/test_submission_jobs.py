"""The extraction job registry.

Its whole job is to make a slow, failure-prone step observable without letting it
break anything: a crash is a state, not an exception, and one user's poll can
never see another user's job.
"""

from __future__ import annotations

import asyncio

import pytest

from scholardrop.extract.mapper import MappedMetadata
from scholardrop.extract.models import ExtractedMetadata
from scholardrop.extract.reader import PdfUnreadable
from scholardrop.submission.jobs import ExtractionJobs, ExtractionResult


def result() -> ExtractionResult:
    return ExtractionResult(metadata=ExtractedMetadata(), mapped=MappedMetadata())


async def finished() -> ExtractionResult:
    return result()


async def boom() -> ExtractionResult:
    raise RuntimeError("the model exploded")


async def unreadable() -> ExtractionResult:
    raise PdfUnreadable("could not open PDF")


async def settle() -> None:
    """Let the background tasks run."""
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def test_a_finished_job_carries_its_result() -> None:
    jobs = ExtractionJobs()

    job = jobs.start("session-1", "42", finished(), filename="thesis.pdf")
    assert job.state == "running"
    await settle()

    assert job.state == "done"
    assert job.metadata is not None and job.mapped is not None
    assert job.finished and job.filename == "thesis.pdf"


@pytest.mark.parametrize(
    ("work", "expected"),
    [(boom, "Extraction failed"), (unreadable, "could not be read")],
)
async def test_a_failure_is_a_state_not_an_exception(work, expected) -> None:
    jobs = ExtractionJobs()

    job = jobs.start("session-1", "42", work())
    await settle()

    assert job.state == "failed"
    assert expected in job.error
    assert job.result is None


async def test_jobs_are_scoped_to_their_session() -> None:
    jobs = ExtractionJobs()
    jobs.start("session-1", "42", finished())
    await settle()

    assert jobs.get("session-1", "42") is not None
    assert jobs.get("session-2", "42") is None, "another session must not see this job"


async def test_forgetting_a_job_and_a_session() -> None:
    jobs = ExtractionJobs()
    jobs.start("session-1", "42", finished())
    jobs.start("session-1", "43", finished())
    await settle()

    jobs.forget("session-1", "42")
    assert jobs.get("session-1", "42") is None
    assert len(jobs) == 1

    jobs.forget_owner("session-1")
    assert len(jobs) == 0


async def test_sweep_drops_finished_jobs_only() -> None:
    jobs = ExtractionJobs()
    gate = asyncio.Event()

    async def waiting() -> ExtractionResult:
        await gate.wait()
        return result()

    jobs.start("session-1", "42", finished())
    jobs.start("session-1", "43", waiting())
    await settle()

    assert jobs.sweep(max_age_seconds=-1) == 1
    assert jobs.get("session-1", "43") is not None

    gate.set()
    await settle()


async def test_a_live_session_keeps_its_result_however_long_it_waits() -> None:
    """Sweeping finished jobs on age alone silently un-prefills the review form of
    a user who uploaded, went to look something up, and came back."""
    jobs = ExtractionJobs()
    jobs.start("session-1", "42", finished())
    await settle()

    swept = jobs.sweep(max_age_seconds=-1, owner_is_live=lambda owner: owner == "session-1")

    assert swept == 0
    assert jobs.get("session-1", "42") is not None


async def test_a_result_goes_as_soon_as_its_session_does() -> None:
    """Nobody can ask for it again, so it need not wait out the TTL."""
    jobs = ExtractionJobs()
    jobs.start("session-1", "42", finished())
    await settle()

    swept = jobs.sweep(max_age_seconds=99999, owner_is_live=lambda owner: False)

    assert swept == 1
    assert jobs.get("session-1", "42") is None
