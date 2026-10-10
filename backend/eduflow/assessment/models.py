"""Examinations and marks (prototype ``Exam`` and ``SubjectMarks``; screen documentation "Exams",
"Marks entry", "Report cards"; monitoring rules "Marks overdue" and "Falling marks").

Lifecycle of a mark sheet (one exam x section x subject)::

    draft --submit--> submitted --approve--> approved --publish (with the exam)--> published
      ^                   |
      +-----return--------+   (approver returns it with a note; the teacher fixes and resubmits)

* An **exam** is a school-wide assessment event (``Unit Test 2``) in an academic year, optionally in a
  term, with dates and a marks-submission deadline.
* A teacher enters marks (or "absent") for every actively enrolled student while the sheet is a draft, and
  submits it. The submission time is kept, so "submitted on time" is measurable.
* Approval happens in the central approvals queue (``mark_sheet``). Publishing the exam publishes every
  approved sheet and notifies families. Families only ever see published marks.
* After publication a mark changes only through a **correction** (``marks_correction`` in the approvals
  queue); an approved correction updates the mark and keeps the old value.
* **Grade bands** are the school's own scale (minimum percentage -> label). Without bands, report cards
  show percentages only: EduFlow does not invent a grading scheme.
"""

from __future__ import annotations

from django.db import models
from django.db.models import F, Q

from eduflow.academics.models import AcademicYear, Section, Subject, Term
from eduflow.core.ids import uuid7
from eduflow.people.models import StaffProfile, Student
from eduflow.tenancy.models import Membership, TenantModel, TenantQuerySet


class SheetStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    SUBMITTED = "submitted", "Submitted"
    APPROVED = "approved", "Approved"
    PUBLISHED = "published", "Published"


class CorrectionStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    APPROVED = "approved", "Approved"
    DECLINED = "declined", "Declined"


class Exam(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    name = models.CharField(max_length=100)
    academic_year = models.ForeignKey(AcademicYear, on_delete=models.PROTECT, related_name="+")
    term = models.ForeignKey(Term, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    starts_on = models.DateField()
    ends_on = models.DateField()
    marks_deadline = models.DateField()
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "assessment_exam"
        constraints = [
            models.CheckConstraint(
                condition=Q(ends_on__gte=F("starts_on")), name="assessment_exam_dates_check"
            ),
            models.CheckConstraint(
                condition=Q(marks_deadline__gte=F("ends_on")), name="assessment_exam_deadline_check"
            ),
            models.UniqueConstraint(fields=["academic_year", "name"], name="assessment_exam_name_uniq"),
            models.UniqueConstraint(fields=["id", "school"], name="assessment_exam_id_school_uniq"),
        ]


class MarkSheet(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name="sheets")
    section = models.ForeignKey(Section, on_delete=models.PROTECT, related_name="mark_sheets")
    subject = models.ForeignKey(Subject, on_delete=models.PROTECT, related_name="+")
    teacher = models.ForeignKey(
        StaffProfile, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    max_marks = models.DecimalField(max_digits=6, decimal_places=2)
    pass_marks = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    status = models.CharField(max_length=16, choices=SheetStatus.choices, default=SheetStatus.DRAFT)
    submitted_at = models.DateTimeField(null=True, blank=True)
    submitted_by = models.ForeignKey(
        Membership, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    decided_by = models.ForeignKey(
        Membership, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    return_note = models.CharField(max_length=500, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "assessment_mark_sheet"
        constraints = [
            models.UniqueConstraint(fields=["exam", "section", "subject"], name="assessment_sheet_uniq"),
            models.CheckConstraint(condition=Q(max_marks__gt=0), name="assessment_sheet_max_check"),
            models.CheckConstraint(
                condition=Q(pass_marks__isnull=True)
                | (Q(pass_marks__gte=0) & Q(pass_marks__lte=F("max_marks"))),
                name="assessment_sheet_pass_check",
            ),
            models.CheckConstraint(
                condition=Q(status=SheetStatus.DRAFT) | Q(submitted_at__isnull=False),
                name="assessment_sheet_submitted_check",
            ),
            models.CheckConstraint(
                condition=~Q(status=SheetStatus.PUBLISHED) | Q(published_at__isnull=False),
                name="assessment_sheet_published_check",
            ),
            models.UniqueConstraint(fields=["id", "school"], name="assessment_mark_sheet_id_school_uniq"),
        ]


class Mark(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    sheet = models.ForeignKey(MarkSheet, on_delete=models.CASCADE, related_name="marks")
    student = models.ForeignKey(Student, on_delete=models.PROTECT, related_name="marks")
    marks = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    absent = models.BooleanField(default=False)
    remark = models.CharField(max_length=300, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "assessment_mark"
        constraints = [
            models.UniqueConstraint(fields=["sheet", "student"], name="assessment_mark_uniq"),
            models.CheckConstraint(
                condition=(Q(absent=True) & Q(marks__isnull=True)) | (Q(absent=False) & Q(marks__gte=0)),
                name="assessment_mark_value_check",
            ),
            models.UniqueConstraint(fields=["id", "school"], name="assessment_mark_id_school_uniq"),
        ]


class MarkCorrection(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    mark = models.ForeignKey(Mark, on_delete=models.CASCADE, related_name="corrections")
    old_marks = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    old_absent = models.BooleanField()
    new_marks = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    new_absent = models.BooleanField(default=False)
    reason = models.CharField(max_length=500)
    status = models.CharField(
        max_length=16, choices=CorrectionStatus.choices, default=CorrectionStatus.PENDING
    )
    requested_by = models.ForeignKey(Membership, on_delete=models.PROTECT, related_name="+")
    decided_by = models.ForeignKey(
        Membership, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "assessment_mark_correction"
        constraints = [
            models.UniqueConstraint(
                fields=["mark"], condition=Q(status="pending"), name="assessment_one_pending_correction"
            ),
            models.CheckConstraint(
                condition=(Q(new_absent=True) & Q(new_marks__isnull=True))
                | (Q(new_absent=False) & Q(new_marks__gte=0)),
                name="assessment_correction_value_check",
            ),
            models.UniqueConstraint(
                fields=["id", "school"], name="assessment_mark_correction_id_school_uniq"
            ),
        ]


class GradeBand(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    label = models.CharField(max_length=16)
    min_percent = models.DecimalField(max_digits=5, decimal_places=2)
    description = models.CharField(max_length=100, blank=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "assessment_grade_band"
        constraints = [
            models.UniqueConstraint(fields=["school", "min_percent"], name="assessment_band_min_uniq"),
            models.UniqueConstraint(fields=["school", "label"], name="assessment_band_label_uniq"),
            models.CheckConstraint(
                condition=Q(min_percent__gte=0) & Q(min_percent__lte=100), name="assessment_band_range_check"
            ),
        ]
