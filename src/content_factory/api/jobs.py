"""jobs.py — In-memory job tracker for web UI background tasks."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Literal

JobState = Literal["pending", "running", "done", "error"]


@dataclass
class Job:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    type: str = ""          # "cut" | "generate"
    state: JobState = "pending"
    message: str = "В очереди…"
    result: dict | None = None
    error: str | None = None


_store: dict[str, Job] = {}


def create(type_: str) -> Job:
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


def all_jobs() -> list[Job]:
    return sorted(_store.values(), key=lambda j: j.id, reverse=True)


def as_dict(job: Job) -> dict:
    return {
        "id": job.id,
        "type": job.type,
        "state": job.state,
        "message": job.message,
        "result": job.result,
        "error": job.error,
    }
