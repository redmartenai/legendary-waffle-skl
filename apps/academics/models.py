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


SUBJECT_SHORT = {
    "MATH": "Ma", "ENG": "En", "SCI": "Sc", "HIN": "Hi", "SST": "SS", "CS": "CS", "ART": "Art",
    "PE": "PE", "MUS": "Mu", "ECO": "Ec", "PHY": "Ph", "CHEM": "Ch", "BIO": "Bi", "EVS": "EVS",
}


class Subject(SchoolScopedModel):
    name = models.CharField(max_length=60)
    code = models.CharField(max_length=12)
    color = models.CharField(max_length=7, default="#4E5A66")

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["school", "code"], name="uniq_subject_code")]

    def __str__(self):
        return self.name

    @property
    def short_name(self) -> str:
        """Two-letter label for timetable ribbons ("Ma", "Sc", "SS", "CS", "Art")."""
        return SUBJECT_SHORT.get(self.code) or (self.code if len(self.code) <= 3 else self.name[:2])


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
        """"6-B", as printed on circulars and ID cards."""
        return f"{self.grade}-{self.section}"


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
    house = models.CharField(max_length=20, blank=True)
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
    # What to bring, set by the subject teacher ("Bring your geometry box").
    note = models.CharField(max_length=120, blank=True)

    class Meta:
        ordering = ["weekday", "period"]
        constraints = [
            models.UniqueConstraint(fields=["class_group", "weekday", "period"], name="uniq_timetable_period")
        ]


class Remark(SchoolScopedModel):
    """A teacher's note about a student, shared with the family."""

    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="remarks")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    class Visibility(models.TextChoices):
        FAMILY = "family", "Parent & student"
        STAFF = "staff", "Staff only"

    body = models.TextField(max_length=1000)
    tone = models.CharField(max_length=10, default="positive")  # positive | concern | info
    visibility = models.CharField(max_length=8, choices=Visibility.choices, default=Visibility.FAMILY)
    # A remark can point at the homework it is about ("the lab record is incomplete").
    homework = models.ForeignKey("homework.Homework", null=True, blank=True, on_delete=models.SET_NULL, related_name="remarks")
    # Concerns ask the family to confirm they have read them.
    requires_ack = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]


class RemarkAck(SchoolScopedModel):
    """A family member confirming they have read a remark."""

    remark = models.ForeignKey(Remark, on_delete=models.CASCADE, related_name="acks")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["remark", "user"], name="uniq_remark_ack")]


class SubjectVacancy(SchoolScopedModel):
    """A subject in a section left without a teacher (someone left, or is on long leave), until it is filled."""

    class_group = models.ForeignKey(ClassGroup, on_delete=models.CASCADE, related_name="vacancies")
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE, related_name="+")
    since = models.DateField()
    reason = models.CharField(max_length=20, default="vacant")  # vacant | long_leave | new_subject
    filled_at = models.DateTimeField(null=True, blank=True)
    filled_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    class Meta:
        ordering = ["since"]


class TimetableDraft(SchoolScopedModel):
    """An unpublished change to one period of a class's week. `subject` empty means the period is cleared.

    Edits made together (a swap touches two periods) share a `batch`, so they are undone and published together.
    """

    class_group = models.ForeignKey(ClassGroup, on_delete=models.CASCADE, related_name="timetable_drafts")
    weekday = models.PositiveSmallIntegerField()
    period = models.PositiveSmallIntegerField()
    subject = models.ForeignKey(Subject, null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    teacher = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    room = models.CharField(max_length=30, blank=True)
    batch = models.UUIDField()
    kind = models.CharField(max_length=10, default="change")  # change | swap
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")

    class Meta:
        ordering = ["weekday", "period"]
        constraints = [models.UniqueConstraint(fields=["class_group", "weekday", "period"], name="uniq_timetable_draft")]


class TimetableRevision(SchoolScopedModel):
    """One publish of timetable changes: when it went live, who published it and how many periods changed."""

    published_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    published_at = models.DateTimeField()
    changes = models.PositiveSmallIntegerField(default=0)
    summary = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-published_at"]
