from django.conf import settings
from django.db import models

from apps.core.models import SchoolScopedModel


class Homework(SchoolScopedModel):
    class_group = models.ForeignKey("academics.ClassGroup", on_delete=models.CASCADE, related_name="homework")
    subject = models.ForeignKey("academics.Subject", on_delete=models.RESTRICT, related_name="+")
    title = models.CharField(max_length=120)
    description = models.TextField(max_length=2000, blank=True)
    assigned_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    assigned_on = models.DateField()
    due_date = models.DateField()
    accepts_photos = models.BooleanField(default=True)

    class Meta:
        ordering = ["-due_date", "-created_at"]


class HomeworkSubmission(SchoolScopedModel):
    class Status(models.TextChoices):
        SUBMITTED = "submitted", "Submitted"
        REVIEWED = "reviewed", "Reviewed"
        REDO = "redo", "Please redo"

    homework = models.ForeignKey(Homework, on_delete=models.CASCADE, related_name="submissions")
    student = models.ForeignKey("academics.Student", on_delete=models.CASCADE, related_name="+")
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    submitted_at = models.DateTimeField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.SUBMITTED)
    teacher_remark = models.CharField(max_length=500, blank=True)
    # Marked work: a grade or score ("B+", "18/20") and the teacher's checked copy.
    grade = models.CharField(max_length=12, blank=True)
    # "Done in my notebook": no upload, the teacher checks the notebook in class.
    in_notebook = models.BooleanField(default=False)
    checked_copy = models.FileField(upload_to="homework/checked/%Y/%m/", blank=True)
    client_id = models.CharField(max_length=64, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["homework", "student"], name="uniq_homework_submission")]


def submission_upload_path(instance, filename):
    # The photo's own school is set on save(), so take it from the (already saved) submission.
    return f"homework/{instance.submission.school_id}/{instance.submission_id}/{filename}"


class SubmissionPhoto(SchoolScopedModel):
    submission = models.ForeignKey(HomeworkSubmission, on_delete=models.CASCADE, related_name="photos")
    image = models.FileField(upload_to=submission_upload_path, max_length=255)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["order"]


class HomeworkAttachment(SchoolScopedModel):
    """A worksheet or reference file the teacher attached to homework."""

    homework = models.ForeignKey(Homework, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(upload_to="homework/attachments/%Y/%m/")
    name = models.CharField(max_length=120)
    size = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["created_at"]


class HomeworkSignoff(SchoolScopedModel):
    """A parent signing the diary: "I have seen this homework"."""

    homework = models.ForeignKey(Homework, on_delete=models.CASCADE, related_name="signoffs")
    student = models.ForeignKey("academics.Student", on_delete=models.CASCADE, related_name="+")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["homework", "student"], name="uniq_homework_signoff")]
