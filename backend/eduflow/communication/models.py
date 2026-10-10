"""Communication (prototype ``Announcement``, ``Thread``, ``ChatMessage``, ``Complaint``; monitoring rules
"Parent waiting 24h+" and "Open parent complaints").

Announcements
    Sent by the office (or by a teacher to a section they teach) to audiences: staff, parents, students,
    optionally limited to one section. Recipients **acknowledge** them; staff can **respond** (prototype
    ``responses``). Unacknowledged announcements are pending responses.

Threads
    A private conversation about one student between a teacher and a guardian (class teacher or subject
    teacher) or between a teacher and the student. A thread **awaits a reply** while its last message is from
    the family side; the time waited is measured from that message.

Complaints and feedback
    Raised by a parent about their child (category, text) or by the office on their behalf. The reporter
    chooses the sentiment (negative, neutral, positive): EduFlow does not infer sentiment from text.
    Status: open -> in progress -> resolved.
"""

from __future__ import annotations

from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models import Q

from eduflow.academics.models import Section, Subject
from eduflow.core.ids import uuid7
from eduflow.people.models import Guardian, StaffProfile, Student
from eduflow.tenancy.models import Membership, TenantModel, TenantQuerySet


def _uniq(model: str) -> models.UniqueConstraint:
    return models.UniqueConstraint(fields=["id", "school"], name=f"communication_{model}_id_school_uniq")


class Audience(models.TextChoices):
    STAFF = "staff", "Staff"
    PARENTS = "parents", "Parents"
    STUDENTS = "students", "Students"


class Announcement(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    title = models.CharField(max_length=200)
    body = models.TextField(max_length=10000)
    audiences = ArrayField(models.CharField(max_length=16, choices=Audience.choices), size=3)
    section = models.ForeignKey(Section, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    pinned = models.BooleanField(default=False)
    requires_acknowledgement = models.BooleanField(default=True)
    author = models.ForeignKey(Membership, on_delete=models.SET_NULL, null=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    archived_at = models.DateTimeField(null=True, blank=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "communication_announcement"
        constraints = [
            models.CheckConstraint(
                condition=Q(audiences__contained_by=Audience.values) & ~Q(audiences=[]),
                name="communication_audiences_check",
            ),
            _uniq("announcement"),
        ]


class Acknowledgement(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    announcement = models.ForeignKey(Announcement, on_delete=models.CASCADE, related_name="acknowledgements")
    member = models.ForeignKey(Membership, on_delete=models.CASCADE, related_name="+")
    at = models.DateTimeField(auto_now_add=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "communication_acknowledgement"
        constraints = [
            models.UniqueConstraint(fields=["announcement", "member"], name="communication_ack_uniq"),
            _uniq("acknowledgement"),
        ]


class AnnouncementResponse(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    announcement = models.ForeignKey(Announcement, on_delete=models.CASCADE, related_name="responses")
    member = models.ForeignKey(Membership, on_delete=models.CASCADE, related_name="+")
    text = models.CharField(max_length=2000)
    at = models.DateTimeField(auto_now_add=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "communication_announcement_response"
        constraints = [_uniq("announcement_response")]


class ThreadKind(models.TextChoices):
    PARENT_CLASS_TEACHER = "parent_class_teacher", "Parent and class teacher"
    PARENT_SUBJECT_TEACHER = "parent_subject_teacher", "Parent and subject teacher"
    STUDENT_TEACHER = "student_teacher", "Student and teacher"


class Thread(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    kind = models.CharField(max_length=32, choices=ThreadKind.choices)
    student = models.ForeignKey(Student, on_delete=models.PROTECT, related_name="threads")
    staff = models.ForeignKey(StaffProfile, on_delete=models.PROTECT, related_name="threads")
    guardian = models.ForeignKey(
        Guardian, on_delete=models.PROTECT, null=True, blank=True, related_name="threads"
    )
    subject = models.ForeignKey(Subject, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    awaiting_reply_since = models.DateTimeField(null=True, blank=True)
    last_message_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "communication_thread"
        constraints = [
            models.CheckConstraint(
                condition=Q(kind="student_teacher", guardian__isnull=True)
                | (~Q(kind="student_teacher") & Q(guardian__isnull=False)),
                name="communication_thread_parties_check",
            ),
            models.UniqueConstraint(
                fields=["kind", "student", "staff", "guardian"],
                nulls_distinct=False,
                name="communication_thread_uniq",
            ),
            _uniq("thread"),
        ]
        indexes = [models.Index(fields=["school", "awaiting_reply_since"], name="communication_awaiting_idx")]


class Message(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    thread = models.ForeignKey(Thread, on_delete=models.CASCADE, related_name="messages")
    author = models.ForeignKey(Membership, on_delete=models.PROTECT, related_name="+")
    from_staff = models.BooleanField()
    text = models.TextField(max_length=5000)
    at = models.DateTimeField(auto_now_add=True)
    reply_seconds = models.PositiveIntegerField(
        null=True, blank=True, help_text="For a staff reply: seconds since the family's message it answers."
    )

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "communication_message"
        ordering = ["at"]
        constraints = [_uniq("message")]


class ComplaintCategory(models.TextChoices):
    TRANSPORT = "transport", "Transport"
    ACADEMICS = "academics", "Academics"
    FEES = "fees", "Fees"
    FACILITIES = "facilities", "Facilities"
    STAFF = "staff", "Staff"
    FOOD = "food", "Food"


class ComplaintStatus(models.TextChoices):
    OPEN = "open", "Open"
    IN_PROGRESS = "in_progress", "In progress"
    RESOLVED = "resolved", "Resolved"


class Sentiment(models.TextChoices):
    NEGATIVE = "negative", "Negative"
    NEUTRAL = "neutral", "Neutral"
    POSITIVE = "positive", "Positive"


class Complaint(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    student = models.ForeignKey(Student, on_delete=models.PROTECT, related_name="complaints")
    category = models.CharField(max_length=16, choices=ComplaintCategory.choices)
    text = models.CharField(max_length=3000)
    sentiment = models.CharField(max_length=16, choices=Sentiment.choices)
    status = models.CharField(max_length=16, choices=ComplaintStatus.choices, default=ComplaintStatus.OPEN)
    raised_by = models.ForeignKey(Membership, on_delete=models.PROTECT, related_name="+")
    assigned_to = models.ForeignKey(
        Membership, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    resolution = models.CharField(max_length=2000, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "communication_complaint"
        constraints = [
            models.CheckConstraint(
                condition=~Q(status="resolved") | Q(resolved_at__isnull=False),
                name="communication_resolved_check",
            ),
            _uniq("complaint"),
        ]
