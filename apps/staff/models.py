from django.conf import settings
from django.db import models

from apps.core.models import SchoolScopedModel


class StaffProfile(SchoolScopedModel):
    """HR details for a staff member at one school (office hours live on the membership's settings)."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="staff_profiles")
    employee_id = models.CharField(max_length=30, blank=True)
    designation = models.CharField(max_length=80, blank=True, help_text="e.g. Mathematics")
    joined_on = models.DateField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["school", "user"], name="uniq_staff_profile")]


class StaffLeave(SchoolScopedModel):
    """A staff member's leave request; the principal decides and arranges cover."""

    class Kind(models.TextChoices):
        CASUAL = "casual", "Casual"
        SICK = "sick", "Sick"
        EARNED = "earned", "Earned"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        DECLINED = "declined", "Declined"
        CANCELLED = "cancelled", "Cancelled"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="staff_leaves")
    kind = models.CharField(max_length=8, choices=Kind.choices)
    from_date = models.DateField()
    to_date = models.DateField()
    half_day = models.BooleanField(default=False)
    days = models.DecimalField(max_digits=4, decimal_places=1)
    reason = models.CharField(max_length=400)
    certificate = models.FileField(upload_to="staff-leave/%Y/%m/", blank=True)
    certificate_name = models.CharField(max_length=120, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-from_date"]


class Substitution(SchoolScopedModel):
    """A cover period: `teacher` takes `slot` on `date` for an absent colleague."""

    date = models.DateField()
    slot = models.ForeignKey("academics.TimetableSlot", on_delete=models.CASCADE, related_name="substitutions")
    teacher = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="covers")
    absent_teacher = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    reason = models.CharField(max_length=60, blank=True, help_text="e.g. on leave")
    assigned_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    assigned_at = models.DateTimeField()
    handover_note = models.TextField(max_length=1000, blank=True)
    handover_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["date", "slot__period"]
        constraints = [models.UniqueConstraint(fields=["date", "slot"], name="uniq_substitution_slot")]


class StaffAttendance(SchoolScopedModel):
    """One staff member's day: checked in (and when), late, absent or on leave."""

    class Status(models.TextChoices):
        PRESENT = "present", "Present"
        LATE = "late", "Late"
        ABSENT = "absent", "Absent"
        LEAVE = "leave", "On leave"

    class Source(models.TextChoices):
        APP = "app", "App check-in"
        BIOMETRIC = "biometric", "Biometric"
        OFFICE = "office", "Marked by office"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="staff_attendance")
    date = models.DateField(db_index=True)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.PRESENT)
    check_in = models.TimeField(null=True, blank=True)
    check_out = models.TimeField(null=True, blank=True)
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.APP)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["school", "user", "date"], name="uniq_staff_attendance_day")]


class Vacancy(SchoolScopedModel):
    """An open position the school is hiring for, with how many people have applied so far."""

    class Kind(models.TextChoices):
        TEACHING = "teaching", "Teaching"
        SUPPORT = "support", "Support"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        FILLED = "filled", "Filled"
        CLOSED = "closed", "Closed"

    title = models.CharField(max_length=80, help_text="e.g. Physics (PGT)")
    kind = models.CharField(max_length=8, choices=Kind.choices, default=Kind.TEACHING)
    subject = models.ForeignKey("academics.Subject", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    positions = models.PositiveSmallIntegerField(default=1)
    applicants = models.PositiveSmallIntegerField(default=0)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.OPEN)
    opened_on = models.DateField()
    closes_on = models.DateField(null=True, blank=True)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["opened_on"]
