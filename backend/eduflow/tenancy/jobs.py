"""Scheduled per-school jobs (monitoring evaluation, reminders).

Modules register a job in their ``AppConfig.ready``::

    jobs.register("daily", "library.overdue_reminders", send_overdue_reminders)

The beat task ``run_school_jobs`` (tenancy.tasks) fans out one :class:`TenantTask` per active school and
cadence, so every job runs inside that school's database context (RLS), never across schools. A failing job
is logged and does not stop the others.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from eduflow.core.logging import get_logger

log = get_logger(__name__)

CADENCES = ("frequent", "daily")
JOBS: dict[str, dict[str, Callable[[Any], Any]]] = {cadence: {} for cadence in CADENCES}


def register(cadence: str, name: str, job: Callable[[Any], Any]) -> None:
    if cadence not in JOBS:
        raise ValueError(f"Unknown cadence {cadence!r}.")
    JOBS[cadence][name] = job


def run(cadence: str, school: Any) -> dict[str, str]:
    results: dict[str, str] = {}
    for name, job in JOBS[cadence].items():
        try:
            outcome = job(school)
        except Exception:
            log.exception("school_job_failed", job=name)
            results[name] = "failed"
        else:
            results[name] = str(outcome)
    return results
