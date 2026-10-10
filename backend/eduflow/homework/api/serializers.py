from __future__ import annotations

from typing import Any

from rest_framework import serializers

from eduflow.academics.api.serializers import Ref
from eduflow.core.api import StrictSerializer
from eduflow.documents.api.serializers import FileOut

from ..models import Homework, Submission, SubmissionStatus


class HomeworkOut(serializers.ModelSerializer[Homework]):
    section = Ref()
    subject = Ref()
    teacher = serializers.CharField(source="teacher.membership.user.full_name", allow_null=True, default=None)
    attachment = FileOut(allow_null=True)

    class Meta:
        model = Homework
        fields = (
            "id",
            "section",
            "subject",
            "teacher",
            "title",
            "description",
            "assigned_on",
            "due_date",
            "max_score",
            "attachment",
            "status",
            "created_at",
        )
        read_only_fields = fields


class HomeworkIn(StrictSerializer):
    section_id = serializers.UUIDField()
    subject_id = serializers.UUIDField()
    title = serializers.CharField(max_length=200)
    description = serializers.CharField(max_length=5000, required=False, allow_blank=True)
    due_date = serializers.DateField()
    max_score = serializers.DecimalField(max_digits=6, decimal_places=2, min_value=1, required=False)
    attachment = serializers.FileField(required=False, help_text="Optional; same file rules as documents.")


class HomeworkUpdateIn(StrictSerializer):
    title = serializers.CharField(max_length=200, required=False)
    description = serializers.CharField(max_length=5000, required=False, allow_blank=True)
    due_date = serializers.DateField(required=False)


class SubmissionOut(serializers.ModelSerializer[Submission]):
    student_id = serializers.UUIDField()
    student = serializers.CharField(source="student.full_name")
    homework_id = serializers.UUIDField()
    file = FileOut(allow_null=True)

    class Meta:
        model = Submission
        fields = (
            "id",
            "homework_id",
            "student_id",
            "student",
            "status",
            "text",
            "file",
            "submitted_at",
            "feedback",
            "score",
            "reviewed_at",
        )
        read_only_fields = fields


class BoardRowOut(serializers.Serializer[Any]):
    student_id = serializers.UUIDField(source="student.pk")
    student = serializers.CharField(source="student.full_name")
    status = serializers.ChoiceField(SubmissionStatus.choices)
    submission = SubmissionOut(allow_null=True)


class HomeworkSubmitIn(StrictSerializer):
    text = serializers.CharField(max_length=10000, required=False, allow_blank=True)
    file = serializers.FileField(required=False)


class ReviewIn(StrictSerializer):
    feedback = serializers.CharField(max_length=2000, required=False, allow_blank=True)
    score = serializers.DecimalField(max_digits=6, decimal_places=2, min_value=0, required=False)
