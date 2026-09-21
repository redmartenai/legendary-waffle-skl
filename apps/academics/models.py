from django.conf import settings
from django.db import models

from apps.core.models import SchoolScopedModel
from apps.core.utils import first_name, initials


class AcademicYear(SchoolScopedModel):
    name = models.CharField(max_length=20)  # "2026–27"
    starts_on = models.DateField()
    ends_on = models.DateField()
    is_current = models.BooleanField(default=False)

    class Meta:
        ordering = ["-starts_on"]

    def __str__(self):
        return self.name


class Subject(SchoolScopedModel):
    name = models.CharField(max_length=60)
    code = models.CharField(max_length=12)
    color = models.CharField(max_length=7, default="#4E5A66")

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["school", "code"], name="uniq_subject_code")]

    def __str__(self):
        return self.name


class ClassGroup(SchoolScopedModel):
    """A class section for one academic year, e.g. Grade 8 · B (or a college section)."""

    academic_year = models.ForeignKey(AcademicYear, on_delete=models.RESTRICT, related_name="class_groups")
    grade = models.CharField(max_length=20)
    section = models.CharField(max_length=10)
    class_teacher = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ["grade", "section"]
        constraints = [
            models.UniqueConstraint(
                fields=["school", "academic_year", "grade", "section"], name="uniq_class_group"
            )
        ]

    def __str__(self):
        return self.label

    @property
    def label(self) -> str:
        return f"Grade {self.grade} · {self.section}"

    @property
    def short_label(self) -> str:
        return f"{self.grade}{self.section}"


class Student(SchoolScopedModel):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="student_profile",
        help_text="Only for students old enough to sign in themselves.",
    )
    full_name = models.CharField(max_length=120)
    admission_no = models.CharField(max_length=30)
    roll_no = models.PositiveSmallIntegerField(default=0)
    class_group = models.ForeignKey(ClassGroup, on_delete=models.RESTRICT, related_name="students")
    date_of_birth = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=10, blank=True)
    photo_url = models.URLField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["class_group__grade", "class_group__section", "roll_no", "full_name"]
        constraints = [
            models.UniqueConstraint(fields=["school", "admission_no"], name="uniq_admission_no")
        ]

    def __str__(self):
        return self.full_name

    @property
    def initials(self) -> str:
        return initials(self.full_name)

    @property
    def first_name(self) -> str:
        return first_name(self.full_name)


class StudentGuardian(SchoolScopedModel):
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="guardian_links")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="guardian_links")
    relationship = models.CharField(max_length=20, default="parent")
    is_primary = models.BooleanField(default=False)
    can_pickup = models.BooleanField(default=True)
    receives_alerts = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["student", "user"], name="uniq_guardian_link")]


class TeachingAssignment(SchoolScopedModel):
    teacher = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="teaching_assignments")
    class_group = models.ForeignKey(ClassGroup, on_delete=models.CASCADE, related_name="teaching_assignments")
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE, related_name="+")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["teacher", "class_group", "subject"], name="uniq_teaching_assignment")
        ]


class TimetableSlot(SchoolScopedModel):
    class_group = models.ForeignKey(ClassGroup, on_delete=models.CASCADE, related_name="timetable_slots")
    weekday = models.PositiveSmallIntegerField(help_text="0 = Monday")
    period = models.PositiveSmallIntegerField()
    starts_at = models.TimeField()
    ends_at = models.TimeField()
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE, related_name="+")
    teacher = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    room = models.CharField(max_length=30, blank=True)

    class Meta:
        ordering = ["weekday", "period"]
        constraints = [
            models.UniqueConstraint(fields=["class_group", "weekday", "period"], name="uniq_timetable_period")
        ]


class Remark(SchoolScopedModel):
    """A teacher's note about a student, shared with the family."""

    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="remarks")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    body = models.TextField(max_length=1000)
    tone = models.CharField(max_length=10, default="positive")  # positive | concern | info

    class Meta:
        ordering = ["-created_at"]
