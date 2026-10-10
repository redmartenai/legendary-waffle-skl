from __future__ import annotations

from typing import Any

from rest_framework import serializers

from eduflow.academics.api.serializers import Ref
from eduflow.core.api import StrictSerializer
from eduflow.documents.api.serializers import FileOut
from eduflow.people.api.serializers import PersonRef

from ..models import (
    Attempt,
    LearningPath,
    Lesson,
    LessonKind,
    LiveClass,
    LiveStatus,
    Progress,
    PublishStatus,
    Quiz,
)


class QuestionIn(StrictSerializer):
    text = serializers.CharField(max_length=1000)
    options = serializers.ListField(child=serializers.CharField(max_length=300), min_length=2, max_length=8)
    answer = serializers.IntegerField(min_value=0, max_value=7)
    explanation = serializers.CharField(max_length=1000, required=False, allow_blank=True)


class QuestionOut(serializers.Serializer[Any]):
    position = serializers.IntegerField()
    text = serializers.CharField()
    options = serializers.ListField(child=serializers.CharField())
    answer = serializers.IntegerField(allow_null=True, help_text="Hidden from a student until they attempt.")
    explanation = serializers.CharField(allow_null=True)


class QuizOut(serializers.ModelSerializer[Quiz]):
    section = Ref()
    subject = Ref()
    questions = serializers.SerializerMethodField()

    class Meta:
        model = Quiz
        fields = ("id", "section", "subject", "title", "max_attempts", "status", "questions", "created_at")
        read_only_fields = fields

    def get_questions(self, quiz: Quiz) -> list[dict[str, Any]]:
        reveal = self.context.get("reveal", True)
        return [
            {
                "position": q.position,
                "text": q.text,
                "options": q.options,
                "answer": q.answer if reveal else None,
                "explanation": q.explanation if reveal else None,
            }
            for q in quiz.questions.all()
        ]


class QuizIn(StrictSerializer):
    section_id = serializers.UUIDField()
    subject_id = serializers.UUIDField()
    title = serializers.CharField(max_length=200)
    max_attempts = serializers.IntegerField(min_value=1, max_value=50, required=False, allow_null=True)
    questions = QuestionIn(many=True, allow_empty=False)


class QuizUpdateIn(StrictSerializer):
    title = serializers.CharField(max_length=200, required=False)
    max_attempts = serializers.IntegerField(min_value=1, max_value=50, required=False, allow_null=True)
    status = serializers.ChoiceField(PublishStatus.choices, required=False)
    questions = QuestionIn(many=True, allow_empty=False, required=False)


class AttemptIn(StrictSerializer):
    answers = serializers.ListField(child=serializers.IntegerField(min_value=-1, max_value=7), max_length=200)


class AttemptOut(serializers.ModelSerializer[Attempt]):
    student = PersonRef()

    class Meta:
        model = Attempt
        fields = ("id", "student", "answers", "score", "total", "submitted_at")
        read_only_fields = fields


class QuizGenerateIn(StrictSerializer):
    subject = serializers.CharField(max_length=100)
    topic = serializers.CharField(max_length=200)
    grade = serializers.CharField(max_length=50)
    count = serializers.IntegerField(min_value=1, max_value=20)


class QuizDraftOut(serializers.Serializer[Any]):
    questions = QuestionIn(many=True)
    note = serializers.CharField()


class LmsLessonOut(serializers.ModelSerializer[Lesson]):
    section = Ref()
    subject = Ref()
    file = FileOut(allow_null=True)
    quiz_id = serializers.UUIDField(allow_null=True)

    class Meta:
        model = Lesson
        fields = (
            "id",
            "section",
            "subject",
            "title",
            "chapter",
            "kind",
            "body",
            "video_url",
            "file",
            "quiz_id",
            "minutes",
            "status",
            "published_at",
            "created_at",
        )
        read_only_fields = fields


class LmsLessonIn(StrictSerializer):
    section_id = serializers.UUIDField()
    subject_id = serializers.UUIDField()
    title = serializers.CharField(max_length=200)
    kind = serializers.ChoiceField(LessonKind.choices)
    chapter = serializers.CharField(max_length=150, required=False, allow_blank=True)
    body = serializers.CharField(max_length=50000, required=False, allow_blank=True)
    video_url = serializers.URLField(max_length=500, required=False, allow_blank=True)
    minutes = serializers.IntegerField(min_value=1, max_value=600, required=False, allow_null=True)
    quiz_id = serializers.UUIDField(required=False, allow_null=True)
    upload = serializers.FileField(required=False)


class LmsLessonUpdateIn(StrictSerializer):
    title = serializers.CharField(max_length=200, required=False)
    chapter = serializers.CharField(max_length=150, required=False, allow_blank=True)
    body = serializers.CharField(max_length=50000, required=False, allow_blank=True)
    video_url = serializers.URLField(max_length=500, required=False)
    minutes = serializers.IntegerField(min_value=1, max_value=600, required=False, allow_null=True)
    status = serializers.ChoiceField(PublishStatus.choices, required=False)


class ProgressIn(StrictSerializer):
    percent = serializers.IntegerField(min_value=0, max_value=100)


class ProgressOut(serializers.ModelSerializer[Progress]):
    lesson_id = serializers.UUIDField()
    student = PersonRef()

    class Meta:
        model = Progress
        fields = ("lesson_id", "student", "percent", "completed_at", "updated_at")
        read_only_fields = fields


class PathOut(serializers.ModelSerializer[LearningPath]):
    section = Ref()
    subject = Ref(allow_null=True)
    lesson_ids = serializers.SerializerMethodField()

    class Meta:
        model = LearningPath
        fields = ("id", "section", "subject", "title", "description", "status", "lesson_ids")
        read_only_fields = fields

    def get_lesson_ids(self, path: LearningPath) -> list[str]:
        return [str(step.lesson_id) for step in path.steps.all()]


class PathIn(StrictSerializer):
    section_id = serializers.UUIDField()
    subject_id = serializers.UUIDField(required=False, allow_null=True)
    title = serializers.CharField(max_length=200)
    description = serializers.CharField(max_length=2000, required=False, allow_blank=True)
    lesson_ids = serializers.ListField(child=serializers.UUIDField(), min_length=1, max_length=200)


class PathUpdateIn(StrictSerializer):
    title = serializers.CharField(max_length=200, required=False)
    description = serializers.CharField(max_length=2000, required=False, allow_blank=True)
    status = serializers.ChoiceField(PublishStatus.choices, required=False)
    lesson_ids = serializers.ListField(
        child=serializers.UUIDField(), min_length=1, max_length=200, required=False
    )


class PathProgressOut(serializers.Serializer[Any]):
    student = PersonRef()
    steps = serializers.IntegerField()
    completed = serializers.IntegerField()
    percent = serializers.IntegerField()


class LiveOut(serializers.ModelSerializer[LiveClass]):
    section = Ref()
    subject = Ref()

    class Meta:
        model = LiveClass
        fields = ("id", "section", "subject", "title", "starts_at", "duration_minutes", "provider", "status")
        read_only_fields = fields


class LiveIn(StrictSerializer):
    section_id = serializers.UUIDField()
    subject_id = serializers.UUIDField()
    title = serializers.CharField(max_length=200)
    starts_at = serializers.DateTimeField()
    duration_minutes = serializers.IntegerField(min_value=1, max_value=600)
    provider = serializers.CharField(max_length=40)
    join_url = serializers.URLField(max_length=500)


class LiveUpdateIn(StrictSerializer):
    title = serializers.CharField(max_length=200, required=False)
    starts_at = serializers.DateTimeField(required=False)
    duration_minutes = serializers.IntegerField(min_value=1, max_value=600, required=False)
    join_url = serializers.URLField(max_length=500, required=False)
    status = serializers.ChoiceField(LiveStatus.choices, required=False)


class JoinOut(serializers.Serializer[Any]):
    join_url = serializers.URLField()


class LessonStatOut(serializers.Serializer[Any]):
    lesson = LmsLessonOut()
    completed = serializers.IntegerField()
    completion_rate = serializers.FloatField(allow_null=True)
    average_progress = serializers.DecimalField(max_digits=4, decimal_places=1)


class QuizStatOut(serializers.Serializer[Any]):
    quiz_id = serializers.UUIDField(source="quiz.pk")
    title = serializers.CharField(source="quiz.title")
    attempted = serializers.IntegerField()
    average_best_percent = serializers.FloatField(allow_null=True)


class StudentStatOut(serializers.Serializer[Any]):
    student = PersonRef()
    lessons_completed = serializers.IntegerField()
    lessons_total = serializers.IntegerField()


class AnalyticsOut(serializers.Serializer[Any]):
    students = serializers.IntegerField()
    lessons = LessonStatOut(many=True)
    quizzes = QuizStatOut(many=True)
    per_student = StudentStatOut(many=True)
