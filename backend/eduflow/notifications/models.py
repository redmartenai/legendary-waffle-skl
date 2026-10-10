"""Notifications: the in-app notification centre, per-person preferences, and external delivery records.

Every notification is an in-app record for one member. External channels (email, SMS) are **opt-in** per
kind (the person's preferences) and each attempt is a ``NotificationDelivery`` with an explicit status:
``queued`` -> ``sent`` | ``failed`` | ``suppressed`` (channel not configured, no address). Nothing is
reported as sent unless a provider accepted it (identity/delivery.py). Push and WhatsApp have no provider
adapter yet and are not offered.
"""

from __future__ import annotations

from django.db import models
from django.db.models import Q

from eduflow.core.ids import uuid7
from eduflow.people.models import Student
from eduflow.tenancy.models import Membership, TenantModel, TenantQuerySet


class NotificationKind(models.TextChoices):
    """The kinds named by the screen documentation (parent notifications) and the prototype."""

    ATTENDANCE = "attendance", "Attendance"
    MARKS = "marks", "Marks"
    HOMEWORK = "homework", "Homework"
    FEES = "fees", "Fees"
    BUS = "bus", "Bus"
    ANNOUNCEMENT = "announcement", "Announcement"
    MESSAGE = "message", "Message"
    REMARK = "remark", "Remark"
    APPROVAL = "approval", "Approval"
    ALERT = "alert", "Alert"
    LEARNING = "learning", "Learning"


class Channel(models.TextChoices):
    EMAIL = "email", "Email"
    SMS = "sms", "SMS"


class DeliveryStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    SENT = "sent", "Sent"
    FAILED = "failed", "Failed"
    SUPPRESSED = "suppressed", "Suppressed"


class Notification(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    recipient = models.ForeignKey(Membership, on_delete=models.CASCADE, related_name="notifications")
    kind = models.CharField(max_length=16, choices=NotificationKind.choices)
    title = models.CharField(max_length=200)
    body = models.CharField(max_length=1000, blank=True)
    student = models.ForeignKey(Student, on_delete=models.CASCADE, null=True, blank=True, related_name="+")
    link_type = models.CharField(
        max_length=40, blank=True, help_text="The record this is about, e.g. homework."
    )
    link_id = models.CharField(max_length=64, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "notifications_notification"
        constraints = [
            models.CheckConstraint(
                condition=Q(kind__in=NotificationKind.values), name="notifications_kind_check"
            ),
            models.UniqueConstraint(
                fields=["id", "school"], name="notifications_notification_id_school_uniq"
            ),
        ]
        indexes = [models.Index(fields=["school", "recipient", "read_at"], name="notifications_inbox_idx")]


class NotificationPreference(TenantModel):
    """An opt-in to receive a kind of notification on an external channel."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    membership = models.ForeignKey(Membership, on_delete=models.CASCADE, related_name="+")
    kind = models.CharField(max_length=16, choices=NotificationKind.choices)
    channel = models.CharField(max_length=8, choices=Channel.choices)
    enabled = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "notifications_preference"
        constraints = [
            models.UniqueConstraint(
                fields=["membership", "kind", "channel"], name="notifications_preference_uniq"
            ),
            models.CheckConstraint(
                condition=Q(kind__in=NotificationKind.values) & Q(channel__in=Channel.values),
                name="notifications_preference_values_check",
            ),
        ]


class NotificationDelivery(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    notification = models.ForeignKey(Notification, on_delete=models.CASCADE, related_name="deliveries")
    channel = models.CharField(max_length=8, choices=Channel.choices)
    status = models.CharField(max_length=12, choices=DeliveryStatus.choices, default=DeliveryStatus.QUEUED)
    attempts = models.PositiveSmallIntegerField(default=0)
    reason = models.CharField(max_length=64, blank=True, help_text="Why it was suppressed or failed.")
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "notifications_delivery"
        constraints = [
            models.UniqueConstraint(fields=["notification", "channel"], name="notifications_delivery_uniq"),
            models.CheckConstraint(
                condition=Q(status__in=DeliveryStatus.values), name="notifications_delivery_status_check"
            ),
        ]
