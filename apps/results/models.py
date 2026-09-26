from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.core.models import SchoolScopedModel


class Exam(SchoolScopedModel):
    class_group = models.ForeignKey("academics.ClassGroup", on_delete=models.CASCADE, related_name="exams")
    name = models.CharField(max_length=60)
    held_on = models.DateField()
    # When families can expect results for an exam that isn't published yet.
    results_on = models.DateField(null=True, blank=True)
    # Exam day logistics for the date sheet and admit card.
    admit_cards_from = models.DateField(null=True, blank=True)
    report_by = models.TimeField(null=True, blank=True, help_text="Be in your seat by")
    is_published = models.BooleanField(default=False)
    published_on = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["held_on"]

    def save(self, *args, **kwargs):
        if self.is_published and not self.published_on:
            self.published_on = timezone.localdate()
        super().save(*args, **kwargs)


class ExamMark(SchoolScopedModel):
    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name="marks")
    student = models.ForeignKey("academics.Student", on_delete=models.CASCADE, related_name="+")
    subject = models.ForeignKey("academics.Subject", on_delete=models.RESTRICT, related_name="+")
    marks = models.DecimalField(max_digits=5, decimal_places=1)
    max_marks = models.DecimalField(max_digits=5, decimal_places=1, default=100)
    # Absent on the test day: `marks` is 0 and the row is left out of totals and averages.
    is_absent = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["exam", "student", "subject"], name="uniq_exam_mark")]


class ReportCardNote(SchoolScopedModel):
    """The class teacher's comment on one student's report card for an exam."""

    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name="notes")
    student = models.ForeignKey("academics.Student", on_delete=models.CASCADE, related_name="+")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    body = models.TextField(max_length=600)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["exam", "student"], name="uniq_report_card_note")]


class ExamPaper(SchoolScopedModel):
    """One paper on an exam's date sheet."""

    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name="papers")
    subject = models.ForeignKey("academics.Subject", on_delete=models.CASCADE, related_name="+")
    date = models.DateField()
    starts_at = models.TimeField()
    ends_at = models.TimeField()
    room = models.CharField(max_length=40, blank=True)
    syllabus = models.JSONField(default=list, blank=True)  # ["Ch 1–5", "Poems 1–4"]

    class Meta:
        ordering = ["date", "starts_at"]


class PrepItem(SchoolScopedModel):
    """A student's exam-prep checklist item (the school suggests them; the student ticks them)."""

    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name="prep_items")
    student = models.ForeignKey("academics.Student", on_delete=models.CASCADE, related_name="+")
    title = models.CharField(max_length=120)
    due_date = models.DateField()
    done_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["due_date", "created_at"]


class MarkSheet(SchoolScopedModel):
    """One subject's marks for one exam: the teacher drafts, submits for review, and the school publishes."""

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        SUBMITTED = "submitted", "Submitted for review"
        # Held by the exam cell for moderation (outliers, a recount) before it can be published.
        REVIEW = "review", "In moderation"
        PUBLISHED = "published", "Published"

    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name="mark_sheets")
    subject = models.ForeignKey("academics.Subject", on_delete=models.CASCADE, related_name="+")
    max_marks = models.DecimalField(max_digits=5, decimal_places=1, default=100)
    due_on = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    saved_at = models.DateTimeField(null=True, blank=True)
    saved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    submitted_at = models.DateTimeField(null=True, blank=True)
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    # Why the exam cell is holding it ("Down 6 pts · flagged"), and when it went out to families.
    review_note = models.CharField(max_length=120, blank=True)
    moderated_at = models.DateTimeField(null=True, blank=True)
    moderated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    published_at = models.DateTimeField(null=True, blank=True)
    published_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["exam", "subject"], name="uniq_mark_sheet")]


class MarkCorrection(SchoolScopedModel):
    """A change to published marks. It needs the principal's approval; the old values are kept to undo it."""

    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name="corrections")
    subject = models.ForeignKey("academics.Subject", on_delete=models.CASCADE, related_name="+")
    reason = models.TextField(max_length=1000)
    checked_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    # [{"student_id", "from", "to", "note"}]
    entries = models.JSONField(default=list)
    applied_at = models.DateTimeField(null=True, blank=True)


class ExamSeries(SchoolScopedModel):
    """School-wide settings for one exam across every section ("Unit Test 2"), matched to `Exam.name`."""

    name = models.CharField(max_length=60)
    weightage = models.PositiveSmallIntegerField(default=0, help_text="% of the term result")
    grading_locked_at = models.DateTimeField(null=True, blank=True)
    grading_locked_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    template_approved_at = models.DateTimeField(null=True, blank=True)
    template_approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    invigilators_per_room = models.PositiveSmallIntegerField(default=2)
    published_at = models.DateTimeField(null=True, blank=True)
    published_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    parents_notified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["school", "name"], name="uniq_exam_series")]


class Invigilation(SchoolScopedModel):
    """A teacher on invigilation duty for one exam paper (one section's room)."""

    paper = models.ForeignKey(ExamPaper, on_delete=models.CASCADE, related_name="invigilations")
    teacher = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="invigilations")
    assigned_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["paper", "teacher"], name="uniq_invigilation")]
