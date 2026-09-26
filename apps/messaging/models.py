from django.conf import settings
from django.db import models

from apps.core.models import SchoolScopedModel


class Conversation(SchoolScopedModel):
    """A family's thread with a teacher (direct) or with an office team (department).

    Always tied to one child so a parent with several children sees clear context.
    Not end-to-end encrypted by design: the school must be able to review chats
    for safeguarding. Messages are encrypted in transit and at rest.
    """

    class Kind(models.TextChoices):
        DIRECT = "direct", "Direct"
        DEPARTMENT = "department", "Office team"

    kind = models.CharField(max_length=12, choices=Kind.choices)
    student = models.ForeignKey(
        "academics.Student", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    department = models.CharField(max_length=16, blank=True)
    last_message_at = models.DateTimeField(null=True, blank=True)
    last_message_preview = models.CharField(max_length=140, blank=True)

    class Meta:
        ordering = ["-last_message_at", "-created_at"]


class ConversationMember(SchoolScopedModel):
    class Side(models.TextChoices):
        FAMILY = "family", "Family"
        STAFF = "staff", "Staff"

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="members")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    side = models.CharField(max_length=8, choices=Side.choices)
    label = models.CharField(max_length=80, blank=True)
    last_read_at = models.DateTimeField(null=True, blank=True)
    muted = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["conversation", "user"], name="uniq_conversation_member")]


class Message(SchoolScopedModel):
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="messages")
    sender = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    body = models.TextField(max_length=2000, blank=True)
    attachment = models.FileField(upload_to="chat/%Y/%m/", blank=True)
    attachment_name = models.CharField(max_length=120, blank=True)
    attachment_size = models.PositiveIntegerField(default=0)
    client_id = models.CharField(max_length=64)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [models.Index(fields=["conversation", "created_at"], name="message_conv_time_idx")]
        constraints = [
            # The app generates client_id before sending, so retries never duplicate a message.
            models.UniqueConstraint(fields=["conversation", "client_id"], name="uniq_message_client_id")
        ]


class Meeting(SchoolScopedModel):
    """A parent–teacher meeting slot booked inside a conversation."""

    class Status(models.TextChoices):
        REQUESTED = "requested", "Requested"
        BOOKED = "booked", "Booked"
        CANCELLED = "cancelled", "Cancelled"

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="meetings")
    title = models.CharField(max_length=120)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    location = models.CharField(max_length=80, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.BOOKED)
    booked_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")

    class Meta:
        ordering = ["starts_at"]
