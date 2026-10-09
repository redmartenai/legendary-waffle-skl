"""Invitation maintenance. Cross-school by nature, so it uses the explicit, logged RLS bypass."""

from __future__ import annotations

from celery import shared_task

from eduflow.core.logging import get_logger

from . import services

log = get_logger(__name__)


@shared_task(name="eduflow.invitations.tasks.expire_due_invitations", ignore_result=True)
def expire_due_invitations() -> int:
    count = services.expire_due_invitations()
    log.info("invitations_expired", count=count)
    return count
