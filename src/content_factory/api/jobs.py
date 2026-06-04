"""jobs.py — In-memory job tracker for web UI background tasks."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Literal

JobState = Literal["pending", "running", "done", "error"]

_JOB_TTL_SECONDS = 3600  # completed/error jobs are purged after 1 hour


@dataclass
class Job:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    type: str = ""
    state: JobState = "pending"
    message: str = "В очереди…"
    result: dict | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None


_store: dict[str, Job] = {}


def _purge_expired() -> None:
    """Remove completed/error jobs older than TTL."""
    now = time.time()
    expired = [
        jid for jid, job in _store.items()
        if job.state in ("done", "error")
        and job.finished_at is not None
        and (now - job.finished_at) > _JOB_TTL_SECONDS
    ]
    for jid in expired:
        del _store[jid]


def create(type_: str) -> Job:
    _purge_expired()
    job = Job(type=type_)
    _store[job.id] = job
    return job


def get(job_id: str) -> Job | None:
    return _store.get(job_id)


def update(job_id: str, **kwargs) -> None:
    job = _store.get(job_id)
    if job is None:
        return
    for k, v in kwargs.items():
        setattr(job, k, v)
    if "state" in kwargs and kwargs["state"] in ("done", "error"):
        job.finished_at = time.time()


def all_jobs() -> list[Job]:
    _purge_expired()
    return sorted(_store.values(), key=lambda j: j.created_at, reverse=True)


def as_dict(job: Job) -> dict:
    return {
        "id": job.id,
        "type": job.type,
        "state": job.state,
        "message": job.message,
        "result": job.result,
        "error": job.error,
        "created_at": job.created_at,
    }
