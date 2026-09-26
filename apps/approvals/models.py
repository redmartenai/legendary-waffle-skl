from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models

from apps.core.models import SchoolScopedModel


class ApprovalRequest(SchoolScopedModel):
    """Something that needs the principal's yes or no: leave, a marks correction, a refund, an admission, an attendance change."""

    class Kind(models.TextChoices):
        LEAVE = "leave", "Leave"
        MARKS = "marks", "Marks correction"
        REFUND = "refund", "Refund"
        ADMISSION = "admission", "Admission"
        ATTENDANCE = "attendance", "Attendance correction"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        DECLINED = "declined", "Declined"
        SENT_BACK = "sent_back", "Sent back"
        WITHDRAWN = "withdrawn", "Withdrawn"

    kind = models.CharField(max_length=12, choices=Kind.choices)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    target_type = models.ForeignKey(ContentType, on_delete=models.CASCADE, related_name="+")
    target_id = models.UUIDField()
    target = GenericForeignKey("target_type", "target_id")
    summary = models.CharField(max_length=200, blank=True)
    # When it matters (a leave's first day); the in-tray sorts by this.
    due_on = models.DateField(null=True, blank=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["due_on", "created_at"]
        indexes = [models.Index(fields=["status", "kind"])]


class ApprovalEvent(SchoolScopedModel):
    """Audit trail: every submission, decision and undo, with who, when and from which device."""

    class Action(models.TextChoices):
        SUBMITTED = "submitted", "Submitted"
        # A second pair of eyes before the principal (a marks correction's HOD check).
        CHECKED = "checked", "Checked"
        APPROVED = "approved", "Approved"
        DECLINED = "declined", "Declined"
        SENT_BACK = "sent_back", "Sent back"
        UNDONE = "undone", "Undone"
        WITHDRAWN = "withdrawn", "Withdrawn"

    request = models.ForeignKey(ApprovalRequest, on_delete=models.CASCADE, related_name="events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    action = models.CharField(max_length=10, choices=Action.choices)
    note = models.CharField(max_length=300, blank=True)
    device = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["created_at"]


class ApprovalAttachment(SchoolScopedModel):
    """Evidence filed with a request: a corrected answer key, scanned scripts, a receipt."""

    class Kind(models.TextChoices):
        DOCUMENT = "document", "Document"
        SCAN = "scan", "Scanned pages"

    request = models.ForeignKey(ApprovalRequest, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(upload_to="approvals/%Y/%m/")
    name = models.CharField(max_length=120)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.DOCUMENT)
    pages = models.PositiveSmallIntegerField(null=True, blank=True)
    size = models.PositiveIntegerField(default=0)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    class Meta:
        ordering = ["created_at"]
