"""Delivery of opted-in notifications on external channels, one school at a time (TenantTask)."""

from __future__ import annotations

from celery import shared_task

from eduflow.tenancy.tasks import TenantTask

from . import services


@shared_task(base=TenantTask, name="eduflow.notifications.tasks.deliver_notification", ignore_result=True)
def deliver_notification(*, delivery_id: str, school_id: str) -> str:
    return services.send(delivery_id)
