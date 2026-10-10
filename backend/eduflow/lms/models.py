"""Learning management (prototype ``Lesson``, ``Quiz``, ``QuizQuestion``; screen documentation "LMS",
"Live classes", "Learning paths").

* A **lesson** is content for a section and subject: a recorded video (a link to where the school hosts it:
  EduFlow integrates no video host), notes (text and/or an attached file) or a link to a quiz. Lessons are
  drafts until published; families see published lessons only.
* **Progress** per student and lesson is 0-100 and only grows; 100 marks it completed.
* A **learning path** orders lessons; a student's path progress is the share of its lessons completed.
* A **quiz** has multiple-choice questions (options, the correct index, an explanation). A student's
  **attempts** are scored on submission; the best score counts (prototype). Answer keys are shown to a
  student only after their first attempt.
* A **live class** is scheduled with any provider's join link (provider-neutral); joining through EduFlow
  records attendance and returns the link.
"""

from __future__ import annotations

from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models import F, Q

from eduflow.academics.models import Section, Subject
from eduflow.core.ids import uuid7
from eduflow.documents.models import StoredFile
from eduflow.people.models import Student
from eduflow.tenancy.models import Membership, TenantModel, TenantQuerySet


def _uniq(model: str) -> models.UniqueConstraint:
    return models.UniqueConstraint(fields=["id", "school"], name=f"lms_{model}_id_school_uniq")


class PublishStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    PUBLISHED = "published", "Published"
    ARCHIVED = "archived", "Archived"


class LessonKind(models.TextChoices):
    VIDEO = "video", "Recorded video"
    NOTES = "notes", "Notes"
    QUIZ = "quiz", "Quiz"


class Quiz(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    section = models.ForeignKey(Section, on_delete=models.PROTECT, related_name="quizzes")
    subject = models.ForeignKey(Subject, on_delete=models.PROTECT, related_name="+")
    title = models.CharField(max_length=200)
    max_attempts = models.PositiveSmallIntegerField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=PublishStatus.choices, default=PublishStatus.DRAFT)
    author = models.ForeignKey(Membership, on_delete=models.SET_NULL, null=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "lms_quiz"
        constraints = [_uniq("quiz")]


class Question(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    quiz = models.ForeignKey(Quiz, on_delete=models.CASCADE, related_name="questions")
    position = models.PositiveSmallIntegerField()
    text = models.CharField(max_length=1000)
    options = ArrayField(models.CharField(max_length=300), size=8)
    answer = models.PositiveSmallIntegerField(help_text="Index of the correct option.")
    explanation = models.CharField(max_length=1000, blank=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "lms_question"
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(fields=["quiz", "position"], name="lms_question_position_uniq"),
            _uniq("question"),
        ]


class Attempt(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    quiz = models.ForeignKey(Quiz, on_delete=models.CASCADE, related_name="attempts")
    student = models.ForeignKey(Student, on_delete=models.PROTECT, related_name="quiz_attempts")
    answers = ArrayField(models.SmallIntegerField())
    score = models.PositiveSmallIntegerField()
    total = models.PositiveSmallIntegerField()
    submitted_at = models.DateTimeField(auto_now_add=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "lms_attempt"
        constraints = [
            models.CheckConstraint(
                condition=Q(score__lte=F("total")), name="lms_attempt_score_le_total_check"
            ),
            _uniq("attempt"),
        ]


class Lesson(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    section = models.ForeignKey(Section, on_delete=models.PROTECT, related_name="learning_lessons")
    subject = models.ForeignKey(Subject, on_delete=models.PROTECT, related_name="+")
    title = models.CharField(max_length=200)
    chapter = models.CharField(max_length=150, blank=True)
    kind = models.CharField(max_length=16, choices=LessonKind.choices)
    body = models.TextField(max_length=50000, blank=True)
    video_url = models.URLField(max_length=500, blank=True)
    file = models.ForeignKey(StoredFile, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    quiz = models.ForeignKey(Quiz, on_delete=models.PROTECT, null=True, blank=True, related_name="lessons")
    minutes = models.PositiveSmallIntegerField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=PublishStatus.choices, default=PublishStatus.DRAFT)
    published_at = models.DateTimeField(null=True, blank=True)
    author = models.ForeignKey(Membership, on_delete=models.SET_NULL, null=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "lms_lesson"
        constraints = [
            models.CheckConstraint(
                condition=~Q(kind="video") | ~Q(video_url=""), name="lms_video_needs_url_check"
            ),
            models.CheckConstraint(
                condition=~Q(kind="quiz") | Q(quiz__isnull=False), name="lms_quiz_lesson_check"
            ),
            _uniq("lesson"),
        ]


class Progress(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    lesson = models.ForeignKey(Lesson, on_delete=models.CASCADE, related_name="progress")
    student = models.ForeignKey(Student, on_delete=models.PROTECT, related_name="lesson_progress")
    percent = models.PositiveSmallIntegerField(default=0)
    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "lms_progress"
        constraints = [
            models.UniqueConstraint(fields=["lesson", "student"], name="lms_progress_uniq"),
            models.CheckConstraint(condition=Q(percent__lte=100), name="lms_progress_range_check"),
            _uniq("progress"),
        ]


class LearningPath(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    section = models.ForeignKey(Section, on_delete=models.PROTECT, related_name="learning_paths")
    subject = models.ForeignKey(Subject, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    title = models.CharField(max_length=200)
    description = models.CharField(max_length=2000, blank=True)
    status = models.CharField(max_length=16, choices=PublishStatus.choices, default=PublishStatus.DRAFT)
    author = models.ForeignKey(Membership, on_delete=models.SET_NULL, null=True, related_name="+")

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "lms_learning_path"
        constraints = [_uniq("learning_path")]


class PathStep(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    path = models.ForeignKey(LearningPath, on_delete=models.CASCADE, related_name="steps")
    lesson = models.ForeignKey(Lesson, on_delete=models.PROTECT, related_name="+")
    position = models.PositiveSmallIntegerField()

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "lms_path_step"
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(fields=["path", "position"], name="lms_step_position_uniq"),
            models.UniqueConstraint(fields=["path", "lesson"], name="lms_step_lesson_uniq"),
            _uniq("path_step"),
        ]


class LiveStatus(models.TextChoices):
    SCHEDULED = "scheduled", "Scheduled"
    CANCELLED = "cancelled", "Cancelled"


class LiveClass(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    section = models.ForeignKey(Section, on_delete=models.PROTECT, related_name="live_classes")
    subject = models.ForeignKey(Subject, on_delete=models.PROTECT, related_name="+")
    title = models.CharField(max_length=200)
    starts_at = models.DateTimeField()
    duration_minutes = models.PositiveSmallIntegerField()
    provider = models.CharField(max_length=40, help_text="Free text, e.g. the meeting service used.")
    join_url = models.URLField(max_length=500)
    status = models.CharField(max_length=16, choices=LiveStatus.choices, default=LiveStatus.SCHEDULED)
    host = models.ForeignKey(Membership, on_delete=models.SET_NULL, null=True, related_name="+")

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "lms_live_class"
        constraints = [
            models.CheckConstraint(condition=Q(duration_minutes__gte=1), name="lms_live_duration_check"),
            _uniq("live_class"),
        ]


class LiveAttendance(TenantModel):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    live_class = models.ForeignKey(LiveClass, on_delete=models.CASCADE, related_name="attendance")
    student = models.ForeignKey(Student, on_delete=models.PROTECT, related_name="+")
    joined_at = models.DateTimeField(auto_now_add=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "lms_live_attendance"
        constraints = [
            models.UniqueConstraint(fields=["live_class", "student"], name="lms_live_attendance_uniq"),
            _uniq("live_attendance"),
        ]
