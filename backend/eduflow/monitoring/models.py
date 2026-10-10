"""Monitoring Intelligence Layer (prototype ``engine/monitoring.ts``; README "First alert set").

Thresholds
    ``MonitoringSettings`` holds the school's thresholds. Defaults are the prototype's ``RULES`` constants.

Alerts
    The engine evaluates every rule over the school's live data (every 15 minutes, per school, inside the
    school's RLS context). One rule produces at most one **open alert per school** (deduplicated by
    ``(school, rule)`` while not resolved), with an explanation (``why``), the rows behind it (``items``), an
    owner and an escalation target.

    Lifecycle: ``open -> acknowledged -> resolved``. A rule that no longer fires resolves its alert
    automatically (``auto_resolved``). If it fires again later, a new alert opens. An alert that stays open
    (not acknowledged) past the rule's escalation time is **escalated**: the principal is notified once.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models import Q

from eduflow.core.ids import uuid7
from eduflow.tenancy.models import Membership, TenantModel, TenantQuerySet


class MonitoringSettings(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    attendance_risk = models.DecimalField(max_digits=3, decimal_places=2, default=Decimal("0.75"))
    attendance_window_days = models.PositiveSmallIntegerField(default=60)
    slip_drop = models.DecimalField(max_digits=3, decimal_places=2, default=Decimal("0.20"))
    slip_recent_days = models.PositiveSmallIntegerField(default=15)
    absent_streak_days = models.PositiveSmallIntegerField(default=3)
    marks_drop = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("12"))
    behaviour_incidents = models.PositiveSmallIntegerField(default=3)
    fee_overdue = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("10000"))
    reply_hours = models.PositiveSmallIntegerField(default=24)
    unreviewed_submissions = models.PositiveSmallIntegerField(default=25)
    late_arrivals = models.PositiveSmallIntegerField(default=5)
    late_window_days = models.PositiveSmallIntegerField(default=30)
    approval_hours = models.PositiveSmallIntegerField(default=24)
    bus_delay_minutes = models.PositiveSmallIntegerField(default=10)
    register_by = models.TimeField(default=datetime.time(8, 15))

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "monitoring_settings"
        constraints = [
            models.UniqueConstraint(fields=["school"], name="monitoring_settings_school_uniq"),
            models.CheckConstraint(
                condition=Q(attendance_risk__gt=0, attendance_risk__lte=1)
                & Q(slip_drop__gt=0, slip_drop__lte=1),
                name="monitoring_ratio_check",
            ),
        ]


class Severity(models.TextChoices):
    CRITICAL = "critical", "Critical"
    HIGH = "high", "High"
    WATCH = "watch", "Watch"


class Domain(models.TextChoices):
    STUDENTS = "students", "Students"
    STAFF = "staff", "Staff"
    OPERATIONS = "operations", "Operations"
    FINANCE = "finance", "Finance"


class AlertStatus(models.TextChoices):
    OPEN = "open", "Open"
    ACKNOWLEDGED = "acknowledged", "Acknowledged"
    RESOLVED = "resolved", "Resolved"


class Alert(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    rule = models.CharField(max_length=40)
    title = models.CharField(max_length=120)
    domain = models.CharField(max_length=16, choices=Domain.choices)
    severity = models.CharField(max_length=16, choices=Severity.choices)
    headline = models.CharField(max_length=300)
    why = models.CharField(max_length=1000)
    owner = models.CharField(max_length=100)
    escalates_to = models.CharField(max_length=150)
    items = models.JSONField(default=list)
    staff_ids = ArrayField(models.UUIDField(), default=list, help_text="Staff an item is about or owned by.")
    section_ids = ArrayField(models.UUIDField(), default=list)
    status = models.CharField(max_length=16, choices=AlertStatus.choices, default=AlertStatus.OPEN)
    first_seen_at = models.DateTimeField()
    last_seen_at = models.DateTimeField()
    occurrences = models.PositiveIntegerField(default=1)
    escalated_at = models.DateTimeField(null=True, blank=True)
    acknowledged_by = models.ForeignKey(
        Membership, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        Membership, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    auto_resolved = models.BooleanField(default=False)
    resolution_note = models.CharField(max_length=500, blank=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "monitoring_alert"
        constraints = [
            models.UniqueConstraint(
                fields=["school", "rule"],
                condition=~Q(status="resolved"),
                name="monitoring_one_live_alert_per_rule",
            ),
            models.CheckConstraint(
                condition=~Q(status="resolved") | Q(resolved_at__isnull=False),
                name="monitoring_resolved_check",
            ),
            models.UniqueConstraint(fields=["id", "school"], name="monitoring_alert_id_school_uniq"),
        ]
        indexes = [models.Index(fields=["school", "status"], name="monitoring_alert_status_idx")]
