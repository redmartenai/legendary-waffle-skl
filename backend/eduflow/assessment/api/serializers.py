from __future__ import annotations

from typing import Any

from rest_framework import serializers

from eduflow.academics.api.serializers import Ref
from eduflow.core.api import StrictSerializer
from eduflow.people.api.serializers import PersonRef

from ..models import Exam, GradeBand, MarkCorrection, MarkSheet


class ExamOut(serializers.ModelSerializer[Exam]):
    academic_year = Ref()
    term = Ref(allow_null=True)

    class Meta:
        model = Exam
        fields = (
            "id",
            "name",
            "academic_year",
            "term",
            "starts_on",
            "ends_on",
            "marks_deadline",
            "published_at",
            "created_at",
        )
        read_only_fields = fields


class ExamIn(StrictSerializer):
    name = serializers.CharField(max_length=100)
    academic_year_id = serializers.UUIDField()
    term_id = serializers.UUIDField(required=False, allow_null=True)
    starts_on = serializers.DateField()
    ends_on = serializers.DateField()
    marks_deadline = serializers.DateField()


class ExamUpdateIn(StrictSerializer):
    name = serializers.CharField(max_length=100, required=False)
    term_id = serializers.UUIDField(required=False, allow_null=True)
    starts_on = serializers.DateField(required=False)
    ends_on = serializers.DateField(required=False)
    marks_deadline = serializers.DateField(required=False)


class SheetOut(serializers.ModelSerializer[MarkSheet]):
    exam_id = serializers.UUIDField()
    section = Ref()
    subject = Ref()
    teacher = serializers.CharField(source="teacher.membership.user.full_name", allow_null=True, default=None)

    class Meta:
        model = MarkSheet
        fields: tuple[str, ...] = (
            "id",
            "exam_id",
            "section",
            "subject",
            "teacher",
            "max_marks",
            "pass_marks",
            "status",
            "submitted_at",
            "return_note",
            "published_at",
        )
        read_only_fields = fields


class SheetIn(StrictSerializer):
    section_id = serializers.UUIDField()
    subject_id = serializers.UUIDField()
    teacher_id = serializers.UUIDField(required=False, allow_null=True)
    max_marks = serializers.DecimalField(max_digits=6, decimal_places=2, min_value=1)
    pass_marks = serializers.DecimalField(
        max_digits=6, decimal_places=2, min_value=0, required=False, allow_null=True
    )


class SheetGenerateIn(StrictSerializer):
    max_marks = serializers.DecimalField(max_digits=6, decimal_places=2, min_value=1)
    pass_marks = serializers.DecimalField(
        max_digits=6, decimal_places=2, min_value=0, required=False, allow_null=True
    )


class SheetsGeneratedOut(serializers.Serializer[Any]):
    created = serializers.IntegerField()


class PublishedOut(serializers.Serializer[Any]):
    published_sheets = serializers.IntegerField()


class EntryOut(serializers.Serializer[Any]):
    mark_id = serializers.UUIDField(allow_null=True)
    student = PersonRef()
    marks = serializers.DecimalField(max_digits=6, decimal_places=2, allow_null=True)
    absent = serializers.BooleanField()
    remark = serializers.CharField()


class SheetDetailOut(SheetOut):
    entries = EntryOut(many=True)

    class Meta(SheetOut.Meta):
        fields = (*SheetOut.Meta.fields, "entries")
        read_only_fields = fields


class MarkEntryIn(StrictSerializer):
    student_id = serializers.UUIDField()
    marks = serializers.DecimalField(
        max_digits=6, decimal_places=2, min_value=0, required=False, allow_null=True
    )
    absent = serializers.BooleanField(required=False, default=False)
    remark = serializers.CharField(max_length=300, required=False, allow_blank=True)


class MarksIn(StrictSerializer):
    marks = MarkEntryIn(many=True, allow_empty=False)


class MarkCorrectionIn(StrictSerializer):
    new_marks = serializers.DecimalField(
        max_digits=6, decimal_places=2, min_value=0, required=False, allow_null=True
    )
    new_absent = serializers.BooleanField(required=False, default=False)
    reason = serializers.CharField(max_length=500)


class MarkCorrectionOut(serializers.ModelSerializer[MarkCorrection]):
    mark_id = serializers.UUIDField()

    class Meta:
        model = MarkCorrection
        fields = (
            "id",
            "mark_id",
            "old_marks",
            "old_absent",
            "new_marks",
            "new_absent",
            "reason",
            "status",
            "decided_at",
            "decision_note",
            "created_at",
        )
        read_only_fields = fields


class ReportLineOut(serializers.Serializer[Any]):
    subject = Ref()
    marks = serializers.DecimalField(max_digits=6, decimal_places=2, allow_null=True)
    absent = serializers.BooleanField()
    max_marks = serializers.DecimalField(max_digits=6, decimal_places=2)
    percent = serializers.DecimalField(max_digits=5, decimal_places=2, allow_null=True)
    grade = serializers.CharField(allow_null=True)
    passed = serializers.BooleanField(allow_null=True)
    remark = serializers.CharField()


class ReportCardOut(serializers.Serializer[Any]):
    student = PersonRef()
    exam = ExamOut()
    lines = ReportLineOut(many=True)
    total = serializers.DecimalField(max_digits=9, decimal_places=2)
    max_total = serializers.DecimalField(max_digits=9, decimal_places=2)
    percent = serializers.DecimalField(max_digits=5, decimal_places=2, allow_null=True)
    grade = serializers.CharField(allow_null=True)


class ResultRowOut(serializers.Serializer[Any]):
    student = PersonRef()
    total = serializers.DecimalField(max_digits=9, decimal_places=2)
    max_total = serializers.DecimalField(max_digits=9, decimal_places=2)
    percent = serializers.DecimalField(max_digits=5, decimal_places=2, allow_null=True)
    grade = serializers.CharField(allow_null=True)
    rank = serializers.IntegerField()


class GradeBandOut(serializers.ModelSerializer[GradeBand]):
    class Meta:
        model = GradeBand
        fields = ("id", "label", "min_percent", "description")
        read_only_fields = fields


class GradeBandIn(StrictSerializer):
    label = serializers.CharField(max_length=16)  # type: ignore[assignment]
    min_percent = serializers.DecimalField(max_digits=5, decimal_places=2, min_value=0, max_value=100)
    description = serializers.CharField(max_length=100, required=False, allow_blank=True)


class GradeBandUpdateIn(StrictSerializer):
    label = serializers.CharField(max_length=16, required=False)  # type: ignore[assignment]
    min_percent = serializers.DecimalField(
        max_digits=5, decimal_places=2, min_value=0, max_value=100, required=False
    )
    description = serializers.CharField(max_length=100, required=False, allow_blank=True)
