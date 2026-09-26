from django.conf import settings
from django.db import models

from apps.core.models import SchoolScopedModel


class Announcement(SchoolScopedModel):
    class Audience(models.TextChoices):
        EVERYONE = "everyone", "Everyone"
        FAMILIES = "families", "Parents and students"
        STAFF = "staff", "Staff"
        CLASSES = "classes", "Selected classes"
        PARENTS = "parents", "Parents only"
        ROUTE = "route", "Families on a bus route"

    class Kind(models.TextChoices):
        GENERAL = "general", "General"
        EVENT = "event", "Event"
        HOLIDAY = "holiday", "Holiday"
        EXAM = "exam", "Exam"
        TRANSPORT = "transport", "Transport"
        SAFETY = "safety", "Safety"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PUBLISHED = "published", "Published"

    title = models.CharField(max_length=120)
    body = models.TextField(max_length=4000)
    # Drafts are the principal's work in progress: never listed to families, never delivered.
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PUBLISHED)
    # With a class audience: only guardians, not the students' own logins ("Grades 6–8 · Parents").
    parents_only = models.BooleanField(default=False)
    # How many people it reached when it went out (the denominator for read and acknowledgement rates).
    recipients = models.PositiveIntegerField(default=0)
    # Circulars are numbered, need an acknowledgement by a date, and keep their file in Documents.
    circular_no = models.PositiveSmallIntegerField(null=True, blank=True)
    ack_due_on = models.DateField(null=True, blank=True)
    document = models.ForeignKey("documents.Document", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.GENERAL)
    # For events (PTM, exams, holidays): when and where it happens.
    event_starts_at = models.DateTimeField(null=True, blank=True)
    event_ends_at = models.DateTimeField(null=True, blank=True)
    location = models.CharField(max_length=80, blank=True)
    audience = models.CharField(max_length=10, choices=Audience.choices, default=Audience.EVERYONE)
    class_groups = models.ManyToManyField("academics.ClassGroup", blank=True, related_name="+")
    route = models.ForeignKey("transport.Route", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    requires_ack = models.BooleanField(default=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    published_at = models.DateTimeField()
    # Delivery: which channels, and whether it has gone out (scheduled ones wait for published_at).
    channels = models.JSONField(default=list, blank=True)  # ["push", "in_app", "sms", "whatsapp", "email"]
    delivered_at = models.DateTimeField(null=True, blank=True)
    attachment = models.FileField(upload_to="announcements/%Y/%m/", blank=True)
    attachment_name = models.CharField(max_length=120, blank=True)
    attachment_size = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-published_at"]


class ChannelDelivery(SchoolScopedModel):
    """One message handed to an SMS, WhatsApp or email provider (push lives in notifications)."""

    announcement = models.ForeignKey(Announcement, on_delete=models.CASCADE, related_name="deliveries")
    channel = models.CharField(max_length=10)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    address = models.CharField(max_length=160)
    provider = models.CharField(max_length=30)
    status = models.CharField(max_length=12, default="queued")
    provider_ref = models.CharField(max_length=80, blank=True)


class AnnouncementRead(SchoolScopedModel):
    """Someone opened an announcement: from the app, or from the link in an SMS, WhatsApp or email.

    Push/in-app opens are also visible as ``Notification.read_at``; read rates combine both.
    """

    announcement = models.ForeignKey(Announcement, on_delete=models.CASCADE, related_name="reads")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    channel = models.CharField(max_length=10)
    read_at = models.DateTimeField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["announcement", "user", "channel"], name="uniq_announcement_read")]


class SmsCreditTopUp(SchoolScopedModel):
    """SMS credits the school bought. Each SMS sent (a ChannelDelivery on "sms") uses one credit."""

    credits = models.PositiveIntegerField()
    note = models.CharField(max_length=120, blank=True)
    added_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")


class AnnouncementAck(SchoolScopedModel):
    announcement = models.ForeignKey(Announcement, on_delete=models.CASCADE, related_name="acks")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    acked_at = models.DateTimeField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["announcement", "user"], name="uniq_announcement_ack")]
