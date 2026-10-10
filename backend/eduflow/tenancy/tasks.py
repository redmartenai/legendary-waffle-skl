"""Tenant-aware Celery tasks (docs/security/rls.md#background-jobs).

A task that works on one school's data derives from :class:`TenantTask` and takes ``school_id`` as a keyword
argument::

    @shared_task(base=TenantTask, name="eduflow.attendance.tasks.send_absence_alerts")
    def send_absence_alerts(*, school_id: str, date: str) -> None:
        ...  # runs with eduflow.school_id = school_id; RLS shows only that school's rows

    send_absence_alerts.delay(school_id=str(actor.school.pk), date="2026-10-08")

Before the task body runs, ``TenantTask``:

1. requires ``school_id`` (a UUID) -> otherwise :class:`MissingTenantContext`;
2. if the task was enqueued during a tenant request, requires the publisher's school (``school_id``
   message header) to match -> otherwise :class:`TenantContextMismatch`, so a request in school A can never
   start work in school B by passing the wrong ID;
3. requires the school to exist and be active -> otherwise :class:`InactiveTenant`;
4. sets the database context to that school for the task's duration and restores the previous one after.

All three errors are permanent: the task fails without retrying, and the failure is logged with the task ID.
"""

from __future__ import annotations

import uuid
from typing import Any

from celery import Task, shared_task

from eduflow.core import db_context
from eduflow.core.celery_context import SCHOOL_HEADER
from eduflow.core.logging import get_logger
from eduflow.core.request_context import bind_request_info

from .models import School

log = get_logger(__name__)


class TenantContextError(Exception):
    """A tenant task could not establish a valid tenant. Never retried."""


class MissingTenantContext(TenantContextError):
    pass


class TenantContextMismatch(TenantContextError):
    pass


class InactiveTenant(TenantContextError):
    pass


def _parse(value: Any) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError):
        raise MissingTenantContext("school_id must be a UUID.") from None


class TenantTask(Task):  # type: ignore[type-arg]
    abstract = True
    # Tenant errors are configuration bugs, not transient failures.
    dont_autoretry_for = (TenantContextError,)

    def _publisher_school(self) -> str | None:
        request = self.request
        value = getattr(request, SCHOOL_HEADER, None)
        if value is None:
            value = (getattr(request, "headers", None) or {}).get(SCHOOL_HEADER)
        return str(value) if value else None

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if "school_id" not in kwargs or kwargs["school_id"] in (None, ""):
            log.error("tenant_task_missing_school", task=self.name)
            raise MissingTenantContext(f"{self.name} requires a school_id keyword argument.")
        school_id = _parse(kwargs["school_id"])

        publisher = self._publisher_school()
        if publisher is not None and publisher != str(school_id):
            log.error("tenant_task_school_mismatch", task=self.name)
            raise TenantContextMismatch(f"{self.name} was enqueued in another school's context.")

        if not School.objects.filter(pk=school_id, is_active=True).exists():
            log.error("tenant_task_inactive_school", task=self.name)
            raise InactiveTenant(f"{self.name}: the school does not exist or is inactive.")

        bind_request_info(school_id=str(school_id))
        # The same context in a worker and in eager mode: the school only, never the enqueuing user.
        with db_context.scoped(db_context.DbContext(school_id=school_id)):
            return super().__call__(*args, **kwargs)


@shared_task(base=TenantTask, name="eduflow.tenancy.tasks.school_jobs", ignore_result=True)
def school_jobs(*, school_id: str, cadence: str) -> dict[str, str]:
    from . import jobs

    school = School.objects.get(pk=school_id)
    return jobs.run(cadence, school)


@shared_task(name="eduflow.tenancy.tasks.run_school_jobs", ignore_result=True)
def run_school_jobs(cadence: str) -> int:
    """Beat: enqueue the cadence's jobs for every active school (one tenant task each)."""
    with db_context.system_context("schedule per-school jobs"):
        ids = [str(pk) for pk in School.objects.filter(is_active=True).values_list("pk", flat=True)]
    for school_id in ids:
        school_jobs.delay(school_id=school_id, cadence=cadence)
    return len(ids)
