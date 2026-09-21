from django.conf import settings
from django.db import models

from apps.core.models import SchoolScopedModel


class Announcement(SchoolScopedModel):
    class Audience(models.TextChoices):
        EVERYONE = "everyone", "Everyone"
        FAMILIES = "families", "Parents and students"
        STAFF = "staff", "Staff"
        CLASSES = "classes", "Selected classes"

    class Kind(models.TextChoices):
        GENERAL = "general", "General"
        EVENT = "event", "Event"
        HOLIDAY = "holiday", "Holiday"
        EXAM = "exam", "Exam"
        TRANSPORT = "transport", "Transport"
        SAFETY = "safety", "Safety"

    title = models.CharField(max_length=120)
    body = models.TextField(max_length=4000)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.GENERAL)
    audience = models.CharField(max_length=10, choices=Audience.choices, default=Audience.EVERYONE)
    class_groups = models.ManyToManyField("academics.ClassGroup", blank=True, related_name="+")
    requires_ack = models.BooleanField(default=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    published_at = models.DateTimeField()

    class Meta:
        ordering = ["-published_at"]


class AnnouncementAck(SchoolScopedModel):
    announcement = models.ForeignKey(Announcement, on_delete=models.CASCADE, related_name="acks")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    acked_at = models.DateTimeField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["announcement", "user"], name="uniq_announcement_ack")]
