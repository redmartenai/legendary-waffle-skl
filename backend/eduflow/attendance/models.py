"""Attendance: daily section registers, their records, and corrections (ADR-008, ADR-028).

Model::

    AttendanceSession  (section x date: the register; taken_by, submitted_at, locked_at, client_id)
     └── AttendanceRecord  (one student: present | absent | late | half_day | excused, note)
           └── AttendanceCorrection  (old status -> new status, reason; pending -> approved | declined)

Integrity lives in the database (attendance migration 0002):

* one register per section and date; one record per student per register;
* a record's section and date equal its register's, and a record names an **enrollment of that student in
  that section** (composite foreign keys), so a register can never mark a stranger;
* statuses are limited to the documented five by ``CHECK`` constraints;
* at most one pending correction per record, and a decision always names who decided and when.
"""

from __future__ import annotations

from django.db import models
from django.db.models import F, Q

from eduflow.academics.models import AcademicYear, Section
from eduflow.core.ids import uuid7
from eduflow.people.models import Enrollment, Student
from eduflow.tenancy.models import Membership, TenantModel, TenantQuerySet


class AttendanceStatus(models.TextChoices):
    """The five statuses of the client contract (CURRENT_STATE §7)."""

    PRESENT = "present", "Present"
    ABSENT = "absent", "Absent"
    LATE = "late", "Late"
    HALF_DAY = "half_day", "Half day"
    EXCUSED = "excused", "Excused"


def _same_school_target(model: str) -> models.UniqueConstraint:
    return models.UniqueConstraint(fields=["id", "school"], name=f"attendance_{model}_id_school_uniq")


def _valid_status(field: str, name: str) -> models.CheckConstraint:
    return models.CheckConstraint(condition=Q(**{f"{field}__in": AttendanceStatus.values}), name=name)


class AttendanceSession(TenantModel):
    """A section's daily register. Re-submitting it before ``locked_at`` replaces it; afterwards changes go
    through corrections."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    section = models.ForeignKey(Section, on_delete=models.PROTECT, related_name="attendance_sessions")
    academic_year = models.ForeignKey(AcademicYear, on_delete=models.PROTECT, related_name="+")
    date = models.DateField()
    taken_by = models.ForeignKey(Membership, on_delete=models.PROTECT, related_name="+")
    submitted_at = models.DateTimeField()
    locked_at = models.DateTimeField(help_text="After this moment the register changes only by correction.")
    client_id = models.CharField(
        max_length=64, blank=True, help_text="Idempotency key of the last submission."
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "attendance_session"
        constraints = [
            models.UniqueConstraint(fields=["section", "date"], name="attendance_session_section_date_uniq"),
            _same_school_target("session"),
            # Target of the record's composite foreign key: a record's section and date are its register's.
            models.UniqueConstraint(
                fields=["id", "section", "date", "school"], name="attendance_session_record_key_uniq"
            ),
        ]
        indexes = [models.Index(fields=["school", "date"], name="attendance_sess_date_idx")]

    def __str__(self) -> str:
        return f"{self.section_id}@{self.date}"


class AttendanceRecord(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    session = models.ForeignKey(AttendanceSession, on_delete=models.CASCADE, related_name="records")
    student = models.ForeignKey(Student, on_delete=models.PROTECT, related_name="attendance_records")
    enrollment = models.ForeignKey(Enrollment, on_delete=models.PROTECT, related_name="attendance_records")
    status = models.CharField(max_length=16, choices=AttendanceStatus.choices)
    note = models.CharField(max_length=200, blank=True)
    # Copies of the register's section and date, kept equal by a composite foreign key (for scoping and
    # per-student history without a join).
    section = models.ForeignKey(Section, on_delete=models.PROTECT, related_name="+", editable=False)
    date = models.DateField(editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "attendance_record"
        constraints = [
            models.UniqueConstraint(
                fields=["session", "student"], name="attendance_record_session_student_uniq"
            ),
            _valid_status("status", "attendance_record_status_check"),
            _same_school_target("record"),
        ]
        indexes = [
            models.Index(fields=["school", "student", "date"], name="attendance_rec_student_idx"),
            models.Index(fields=["school", "section", "date"], name="attendance_rec_section_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.student_id}@{self.date}"


class CorrectionStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    APPROVED = "approved", "Approved"
    DECLINED = "declined", "Declined"


class AttendanceCorrection(TenantModel):
    """A requested change to a locked record. Applied only when approved; the row is the change history."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    record = models.ForeignKey(AttendanceRecord, on_delete=models.PROTECT, related_name="corrections")
    old_status = models.CharField(max_length=16, choices=AttendanceStatus.choices)
    new_status = models.CharField(max_length=16, choices=AttendanceStatus.choices)
    reason = models.CharField(max_length=500)
    status = models.CharField(
        max_length=16, choices=CorrectionStatus.choices, default=CorrectionStatus.PENDING
    )
    requested_by = models.ForeignKey(Membership, on_delete=models.PROTECT, related_name="+")
    decided_by = models.ForeignKey(
        Membership, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "attendance_correction"
        constraints = [
            _valid_status("old_status", "attendance_correction_old_status_check"),
            _valid_status("new_status", "attendance_correction_new_status_check"),
            models.CheckConstraint(
                condition=~Q(old_status=F("new_status")), name="attendance_correction_changes_status_check"
            ),
            models.CheckConstraint(condition=~Q(reason=""), name="attendance_correction_reason_check"),
            models.CheckConstraint(
                condition=Q(status=CorrectionStatus.PENDING, decided_by__isnull=True, decided_at__isnull=True)
                | (
                    Q(status__in=[CorrectionStatus.APPROVED, CorrectionStatus.DECLINED])
                    & Q(decided_by__isnull=False, decided_at__isnull=False)
                ),
                name="attendance_correction_decision_check",
            ),
            models.UniqueConstraint(
                fields=["record"],
                condition=Q(status=CorrectionStatus.PENDING),
                name="attendance_correction_one_pending_uniq",
            ),
            _same_school_target("correction"),
        ]
        indexes = [models.Index(fields=["school", "status"], name="attendance_corr_status_idx")]

    def __str__(self) -> str:
        return str(self.id)
