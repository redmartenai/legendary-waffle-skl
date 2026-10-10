"""Homework and assignments (prototype ``Homework``; screen documentation "Homework").

* A teacher sets homework for a section and subject they teach (or an office user school-wide), with an
  optional attachment and a due date.
* A student submits once (text and/or a file); a submission after the due date (school time) is ``late``.
  A submission may be replaced until it is reviewed.
* The teacher reviews it with feedback and an optional score. ``pending`` is not stored: an actively
  enrolled student of the section without a submission is pending.
"""

from __future__ import annotations

from django.db import models
from django.db.models import Q

from eduflow.academics.models import Section, Subject
from eduflow.core.ids import uuid7
from eduflow.documents.models import StoredFile
from eduflow.people.models import StaffProfile, Student
from eduflow.tenancy.models import Membership, TenantModel, TenantQuerySet


class HomeworkStatus(models.TextChoices):
    PUBLISHED = "published", "Published"
    ARCHIVED = "archived", "Archived"


class SubmissionStatus(models.TextChoices):
    PENDING = "pending", "Pending"  # derived only; never stored
    SUBMITTED = "submitted", "Submitted"
    LATE = "late", "Late"
    REVIEWED = "reviewed", "Reviewed"


STORED_STATES = [SubmissionStatus.SUBMITTED, SubmissionStatus.LATE, SubmissionStatus.REVIEWED]


class Homework(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    section = models.ForeignKey(Section, on_delete=models.PROTECT, related_name="homework")
    subject = models.ForeignKey(Subject, on_delete=models.PROTECT, related_name="+")
    teacher = models.ForeignKey(
        StaffProfile, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    title = models.CharField(max_length=200)
    description = models.TextField(max_length=5000, blank=True)
    assigned_on = models.DateField()
    due_date = models.DateField()
    max_score = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    attachment = models.ForeignKey(
        StoredFile, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    status = models.CharField(max_length=16, choices=HomeworkStatus.choices, default=HomeworkStatus.PUBLISHED)
    created_by = models.ForeignKey(Membership, on_delete=models.SET_NULL, null=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "homework_homework"
        constraints = [
            models.CheckConstraint(
                condition=Q(due_date__gte=models.F("assigned_on")), name="homework_due_check"
            ),
            models.CheckConstraint(
                condition=Q(max_score__isnull=True) | Q(max_score__gt=0), name="homework_max_score_check"
            ),
            models.CheckConstraint(
                condition=Q(status__in=HomeworkStatus.values), name="homework_status_check"
            ),
            models.UniqueConstraint(fields=["id", "school"], name="homework_homework_id_school_uniq"),
        ]
        indexes = [models.Index(fields=["school", "section", "due_date"], name="homework_section_due_idx")]


class Submission(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    homework = models.ForeignKey(Homework, on_delete=models.CASCADE, related_name="submissions")
    student = models.ForeignKey(Student, on_delete=models.PROTECT, related_name="homework_submissions")
    text = models.TextField(max_length=10000, blank=True)
    file = models.ForeignKey(StoredFile, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    status = models.CharField(max_length=16, choices=SubmissionStatus.choices)
    submitted_at = models.DateTimeField()
    feedback = models.CharField(max_length=2000, blank=True)
    score = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    reviewed_by = models.ForeignKey(
        Membership, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "homework_submission"
        constraints = [
            models.UniqueConstraint(fields=["homework", "student"], name="homework_one_submission_uniq"),
            models.CheckConstraint(
                condition=Q(status__in=STORED_STATES), name="homework_submission_status_check"
            ),
            models.CheckConstraint(
                condition=~Q(status=SubmissionStatus.REVIEWED) | Q(reviewed_at__isnull=False),
                name="homework_reviewed_has_time_check",
            ),
            models.CheckConstraint(
                condition=Q(score__isnull=True) | Q(score__gte=0), name="homework_score_check"
            ),
            models.UniqueConstraint(fields=["id", "school"], name="homework_submission_id_school_uniq"),
        ]
