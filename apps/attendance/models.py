from django.conf import settings
from django.db import models

from apps.core.models import SchoolScopedModel


class AttendanceSession(SchoolScopedModel):
    """One class's attendance for one day. Only exceptions are stored (absent, late, ...):
    everyone else in the class was present. That keeps the table ~10x smaller."""

    class_group = models.ForeignKey("academics.ClassGroup", on_delete=models.CASCADE, related_name="attendance_sessions")
    date = models.DateField()
    marked_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    marked_at = models.DateTimeField()
    client_id = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ["-date"]
        constraints = [models.UniqueConstraint(fields=["class_group", "date"], name="uniq_attendance_session")]


class AttendanceStatus(models.TextChoices):
    ABSENT = "absent", "Absent"
    LATE = "late", "Late"
    HALF_DAY = "half_day", "Half day"
    EXCUSED = "excused", "On leave"


class AttendanceException(SchoolScopedModel):
    session = models.ForeignKey(AttendanceSession, on_delete=models.CASCADE, related_name="exceptions")
    student = models.ForeignKey("academics.Student", on_delete=models.CASCADE, related_name="attendance_exceptions")
    status = models.CharField(max_length=10, choices=AttendanceStatus.choices)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["session", "student"], name="uniq_attendance_exception")]
