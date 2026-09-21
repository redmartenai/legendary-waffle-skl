from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.core.models import TimeStampedModel


class Category(models.TextChoices):
    BUS = "bus", "Bus"
    SAFETY = "safety", "Safety"
    ATTENDANCE = "attendance", "Attendance"
    HOMEWORK = "homework", "Homework"
    FEES = "fees", "Fees"
    RESULTS = "results", "Results"
    CHAT = "chat", "Messages"
    ANNOUNCEMENT = "announcement", "Announcements"
    GENERAL = "general", "General"


# Time-critical categories ignore quiet hours. Everything else waits for morning.
ALWAYS_DELIVER = frozenset({Category.BUS, Category.SAFETY, Category.ATTENDANCE})


class Priority(models.TextChoices):
    NORMAL = "normal", "Normal"
    HIGH = "high", "High"
    CRITICAL = "critical", "Critical"


class PushStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    SENT = "sent", "Sent"
    QUIET = "quiet", "Held for quiet hours"
    SKIPPED = "skipped", "Push disabled"
    NO_DEVICE = "no_device", "No device"
    FAILED = "failed", "Failed"


class Notification(TimeStampedModel):
    """The in-app inbox is the source of truth; push is best-effort on top of it."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    school = models.ForeignKey("tenancy.School", null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    category = models.CharField(max_length=16, choices=Category.choices, default=Category.GENERAL)
    title = models.CharField(max_length=140)
    body = models.CharField(max_length=500)
    data = models.JSONField(default=dict, blank=True)
    priority = models.CharField(max_length=10, choices=Priority.choices, default=Priority.NORMAL)
    dedupe_key = models.CharField(max_length=200, null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    push_status = models.CharField(max_length=12, choices=PushStatus.choices, default=PushStatus.PENDING)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "school", "-created_at"], name="notif_user_school_idx")]
        constraints = [
            # One notification per person per event: retries and re-sent batches can't spam anyone.
            models.UniqueConstraint(
                fields=["user", "dedupe_key"],
                condition=Q(dedupe_key__isnull=False),
                name="uniq_notification_dedupe",
            )
        ]
