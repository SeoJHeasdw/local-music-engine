"""Typed records and state-transition rules for persisted project data."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, NotRequired, TypedDict


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"


TERMINAL_JOB_STATUSES = {
    JobStatus.SUCCEEDED,
    JobStatus.PARTIAL,
    JobStatus.FAILED,
    JobStatus.CANCELLED,
}

ALLOWED_JOB_TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
    JobStatus.QUEUED: {JobStatus.RUNNING, JobStatus.CANCELLED, JobStatus.FAILED},
    JobStatus.RUNNING: {
        JobStatus.SUCCEEDED,
        JobStatus.PARTIAL,
        JobStatus.FAILED,
        JobStatus.CANCELLING,
        JobStatus.CANCELLED,
    },
    JobStatus.CANCELLING: {JobStatus.CANCELLED, JobStatus.FAILED},
    JobStatus.SUCCEEDED: set(),
    JobStatus.PARTIAL: set(),
    JobStatus.FAILED: set(),
    JobStatus.CANCELLED: set(),
}


class AudioMetadata(TypedDict):
    frames: int
    sampleRate: int
    channels: int
    sampleWidthBytes: int
    durationSeconds: float
    peak: float
    rms: float
    silentFraction: float


class ArtifactRecord(TypedDict):
    artifactId: str
    kind: str
    path: str
    sha256: str
    bytes: int
    createdAt: str
    createdByJobId: str | None
    audio: NotRequired[AudioMetadata]
    externalPath: NotRequired[str]


class FindingRecord(TypedDict):
    findingId: str
    artifactId: str
    check: str
    severity: str
    message: str
    observed: dict[str, Any]
    threshold: dict[str, Any]
    confidence: float
    startSeconds: float | None
    endSeconds: float | None
    createdAt: str


class CandidateRecord(TypedDict):
    candidateId: str
    requestId: str
    artifactId: str
    findingIds: list[str]
    status: str
    parentCandidateId: str | None
    editRange: dict[str, float] | None
    contextRange: dict[str, float] | None
    humanReview: dict[str, Any]
    createdAt: str


def validate_job_transition(current: str, requested: str) -> None:
    """Reject state rewrites that could make failed work appear successful."""

    before = JobStatus(current)
    after = JobStatus(requested)
    if before == after:
        return
    if after not in ALLOWED_JOB_TRANSITIONS[before]:
        raise ValueError(f"invalid job transition: {before.value} -> {after.value}")
