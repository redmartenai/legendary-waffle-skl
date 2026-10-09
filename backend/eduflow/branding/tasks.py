"""Background jobs for branding. Platform-wide, so they run under the logged bypass in services."""

from __future__ import annotations

from celery import shared_task

from . import services


@shared_task(name="eduflow.branding.tasks.recheck_domains")
def recheck_domains() -> dict[str, int]:
    """Daily: disable verified custom domains whose DNS proof has lapsed (ADR-029)."""
    return services.recheck_verified_domains()
