"""Study material, syllabus progress and projects/assignments (beyond day-to-day homework)."""

from django.conf import settings
from django.db import models

from apps.core.models import SchoolScopedModel


class StudyMaterial(SchoolScopedModel):
    """Notes, videos, slides or worksheets a teacher shares with a class."""

    class Kind(models.TextChoices):
        NOTES = "notes", "Notes"
        VIDEO = "video", "Video lesson"
        SLIDES = "slides", "Slides"
        WORKSHEET = "worksheet", "Worksheet"
        MAP = "map", "Map"

    class_group = models.ForeignKey("academics.ClassGroup", on_delete=models.CASCADE, related_name="materials")
    subject = models.ForeignKey("academics.Subject", on_delete=models.CASCADE, related_name="+")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    title = models.CharField(max_length=160)
    description = models.CharField(max_length=300, blank=True)
    file = models.FileField(upload_to="materials/%Y/%m/", blank=True)
    url = models.URLField(blank=True, help_text="For videos hosted elsewhere")
    size = models.PositiveIntegerField(default=0)
    pages = models.PositiveSmallIntegerField(null=True, blank=True)
    duration_minutes = models.PositiveSmallIntegerField(null=True, blank=True)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    published_at = models.DateTimeField()

    class Meta:
        ordering = ["-published_at"]


class SyllabusProgress(SchoolScopedModel):
    """How much of the term's syllabus a class has covered in a subject (updated by the teacher)."""

    class_group = models.ForeignKey("academics.ClassGroup", on_delete=models.CASCADE, related_name="syllabus")
    subject = models.ForeignKey("academics.Subject", on_delete=models.CASCADE, related_name="+")
    percent = models.PositiveSmallIntegerField(default=0)
    # Where the teacher's lesson plan says the class should be by now.
    planned_percent = models.PositiveSmallIntegerField(null=True, blank=True)
    current_topic = models.CharField(max_length=120, blank=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["class_group", "subject"], name="uniq_syllabus_progress")]


class Assignment(SchoolScopedModel):
    """A project or graded assignment: individual or in groups, with milestones and a rubric."""

    class Kind(models.TextChoices):
        PROJECT = "project", "Project"
        ASSIGNMENT = "assignment", "Assignment"

    class_group = models.ForeignKey("academics.ClassGroup", on_delete=models.CASCADE, related_name="assignments")
    subject = models.ForeignKey("academics.Subject", on_delete=models.CASCADE, related_name="+")
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.ASSIGNMENT)
    title = models.CharField(max_length=160)
    description = models.TextField(blank=True)
    # Shown before the brief opens, e.g. "300 words".
    teaser = models.CharField(max_length=80, blank=True)
    group_size = models.PositiveSmallIntegerField(default=1)
    max_marks = models.PositiveSmallIntegerField(default=20)
    # [{"key": "concept", "label": "Concept", "max": 10}, ...]
    rubric = models.JSONField(default=list, blank=True)
    opens_on = models.DateField(null=True, blank=True, help_text="Hidden details until this date")
    due_date = models.DateField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")

    class Meta:
        ordering = ["due_date"]


class AssignmentGroup(SchoolScopedModel):
    assignment = models.ForeignKey(Assignment, on_delete=models.CASCADE, related_name="groups")
    name = models.CharField(max_length=60, blank=True)
    members = models.ManyToManyField("academics.Student", related_name="assignment_groups")


class AssignmentMilestone(SchoolScopedModel):
    assignment = models.ForeignKey(Assignment, on_delete=models.CASCADE, related_name="milestones")
    title = models.CharField(max_length=120)
    due_date = models.DateField()
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["order", "due_date"]


class MilestoneProgress(SchoolScopedModel):
    """A group's own plan for a milestone: who owns it and when it was ticked off."""

    group = models.ForeignKey(AssignmentGroup, on_delete=models.CASCADE, related_name="progress")
    milestone = models.ForeignKey(AssignmentMilestone, on_delete=models.CASCADE, related_name="+")
    owners = models.ManyToManyField("academics.Student", blank=True, related_name="+")
    done_at = models.DateTimeField(null=True, blank=True)
    done_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["group", "milestone"], name="uniq_milestone_progress")]


class AssignmentSubmission(SchoolScopedModel):
    """One student's (or one group's) work on an assignment, and the teacher's grading."""

    class Status(models.TextChoices):
        SUBMITTED = "submitted", "Submitted"
        GRADED = "graded", "Graded"
        REDO = "redo", "Returned for redo"

    assignment = models.ForeignKey(Assignment, on_delete=models.CASCADE, related_name="submissions")
    student = models.ForeignKey("academics.Student", null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    group = models.ForeignKey(AssignmentGroup, null=True, blank=True, on_delete=models.CASCADE, related_name="submissions")
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    submitted_at = models.DateTimeField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.SUBMITTED)
    scores = models.JSONField(default=dict, blank=True)  # {"concept": 8, "presentation": 4}
    total = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)
    grade = models.CharField(max_length=4, blank=True)
    feedback = models.TextField(blank=True)
    graded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    graded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-submitted_at"]


class SubmissionFile(SchoolScopedModel):
    submission = models.ForeignKey(AssignmentSubmission, on_delete=models.CASCADE, related_name="files")
    file = models.FileField(upload_to="assignments/%Y/%m/")
    name = models.CharField(max_length=120)
    size = models.PositiveIntegerField(default=0)
