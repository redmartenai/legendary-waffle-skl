from django.conf import settings
from django.db import models

from apps.core.models import SchoolScopedModel
from apps.core.tenant import current_school


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


class LeaveApplication(SchoolScopedModel):
    """A family asking for a child's absence to be excused (sent to the class teacher)."""

    class Kind(models.TextChoices):
        SICK = "sick", "Sick"
        FAMILY = "family", "Family"
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        PENDING = "pending", "Waiting for the class teacher"
        APPROVED = "approved", "Approved"
        DECLINED = "declined", "Declined"

    student = models.ForeignKey("academics.Student", on_delete=models.CASCADE, related_name="leave_applications")
    from_date = models.DateField()
    to_date = models.DateField()
    kind = models.CharField(max_length=8, choices=Kind.choices, default=Kind.SICK)
    reason = models.CharField(max_length=300)
    half_day = models.BooleanField(default=False)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.PENDING)
    applied_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-from_date"]


class AttendanceCorrection(SchoolScopedModel):
    """A change to a register after the cutoff. Applied once the principal approves."""

    session = models.ForeignKey(AttendanceSession, on_delete=models.CASCADE, related_name="corrections")
    # [{"student_id", "from", "to"}]; "present" means no exception row.
    entries = models.JSONField(default=list)
    reason = models.CharField(max_length=300, blank=True)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    applied_at = models.DateTimeField(null=True, blank=True)
    # The slip number people quote ("AC-0419"), sequential per school.
    number = models.PositiveIntegerField(default=0)

    def save(self, *args, **kwargs):
        if not self.number:
            last = AttendanceCorrection.all_objects.filter(school_id=self.school_id or getattr(current_school(required=False), "id", None)).aggregate(n=models.Max("number"))["n"]
            self.number = (last or 400) + 1
        super().save(*args, **kwargs)

    @property
    def code(self) -> str:
        return f"AC-{self.number:04d}"


class AbsenceContact(SchoolScopedModel):
    """Every time the school reached out about an absence: an alert to the guardians, a call, a hand-off."""

    class Channel(models.TextChoices):
        ALERT = "alert", "Absence alert"
        CALL = "call", "Phone call"
        HANDOFF = "handoff", "Handed to the front office"

    class Outcome(models.TextChoices):
        SENT = "sent", "Sent"
        REACHED = "reached", "Reached"
        NO_ANSWER = "no_answer", "No answer"

    student = models.ForeignKey("academics.Student", on_delete=models.CASCADE, related_name="absence_contacts")
    date = models.DateField()
    channel = models.CharField(max_length=8, choices=Channel.choices)
    outcome = models.CharField(max_length=10, choices=Outcome.choices, default=Outcome.SENT)
    note = models.CharField(max_length=300, blank=True)
    by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["school", "date", "channel"])]
