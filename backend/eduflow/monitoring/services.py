"""Alert lifecycle actions and settings. Transactional and audited (``monitoring.*``)."""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.utils import timezone

from eduflow.authz.grants import Actor
from eduflow.core.api import Conflict
from eduflow.tenancy import domain

from .models import Alert, AlertStatus, MonitoringSettings


@transaction.atomic
def acknowledge(actor: Actor, alert: Alert) -> Alert:
    alert = Alert.objects.select_for_update().get(pk=alert.pk)
    if alert.status != AlertStatus.OPEN:
        raise Conflict(f"This alert is already {alert.status}.")
    alert.status, alert.acknowledged_by, alert.acknowledged_at = (
        AlertStatus.ACKNOWLEDGED,
        actor.membership,
        timezone.now(),
    )
    alert.save()
    domain.record("monitoring.alert.acknowledged", alert, rule=alert.rule)
    return alert


@transaction.atomic
def resolve(actor: Actor, alert: Alert, *, note: str = "") -> Alert:
    alert = Alert.objects.select_for_update().get(pk=alert.pk)
    if alert.status == AlertStatus.RESOLVED:
        raise Conflict("This alert is already resolved.")
    alert.status, alert.resolved_by, alert.resolved_at = (
        AlertStatus.RESOLVED,
        actor.membership,
        timezone.now(),
    )
    alert.resolution_note = note
    alert.save()
    domain.record("monitoring.alert.resolved", alert, rule=alert.rule)
    return alert


@transaction.atomic
def update_settings(actor: Actor, **data: Any) -> MonitoringSettings:
    row, _ = MonitoringSettings.objects.get_or_create(school_id=actor.school.pk)
    changed = domain.apply_changes(row, data)
    if changed:
        row.save()
        domain.record("monitoring.settings.updated", row, fields=changed)
    return row
