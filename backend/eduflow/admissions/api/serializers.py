from __future__ import annotations

from typing import Any

from rest_framework import serializers

from eduflow.academics.api.serializers import Ref
from eduflow.core.api import StrictSerializer

from ..models import Application, ApplicationDocument, Source, Stage, StageChange


class StageChangeOut(serializers.ModelSerializer[StageChange]):
    by = serializers.CharField(source="by.user.full_name", allow_null=True, default=None)

    class Meta:
        model = StageChange
        fields = ("from_stage", "to_stage", "note", "by", "at")
        read_only_fields = fields


class ApplicationOut(serializers.ModelSerializer[Application]):
    grade = Ref()
    academic_year = Ref(allow_null=True)
    student_id = serializers.UUIDField(allow_null=True)
    history = StageChangeOut(many=True)

    class Meta:
        model = Application
        fields = (
            "id",
            "child_name",
            "date_of_birth",
            "grade",
            "academic_year",
            "parent_name",
            "phone",
            "email",
            "source",
            "stage",
            "notes",
            "fee_quoted",
            "offer_decision",
            "offer_decided_at",
            "student_id",
            "history",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class _Fields(StrictSerializer):
    child_name = serializers.CharField(max_length=200)
    date_of_birth = serializers.DateField(required=False, allow_null=True)
    grade_id = serializers.UUIDField()
    academic_year_id = serializers.UUIDField(required=False, allow_null=True)
    parent_name = serializers.CharField(max_length=200)
    phone = serializers.CharField(max_length=32, required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if not attrs.get("phone") and not attrs.get("email"):
            raise serializers.ValidationError({"phone": ["Give a phone number or an email address."]})
        return attrs


class ApplicationIn(_Fields):
    source = serializers.ChoiceField(Source.choices)  # type: ignore[assignment]
    notes = serializers.CharField(max_length=2000, required=False, allow_blank=True)
    fee_quoted = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=0, required=False, allow_null=True
    )


class ApplicationUpdateIn(StrictSerializer):
    child_name = serializers.CharField(max_length=200, required=False)
    date_of_birth = serializers.DateField(required=False, allow_null=True)
    parent_name = serializers.CharField(max_length=200, required=False)
    phone = serializers.CharField(max_length=32, required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    notes = serializers.CharField(max_length=2000, required=False, allow_blank=True)
    fee_quoted = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=0, required=False, allow_null=True
    )


class OnlineApplicationIn(_Fields):
    school_code = serializers.SlugField(max_length=32)
    message = serializers.CharField(max_length=1000, required=False, allow_blank=True)


class OnlineReceiptOut(serializers.Serializer[Any]):
    reference = serializers.UUIDField()
    message = serializers.CharField()


class MoveIn(StrictSerializer):
    stage = serializers.ChoiceField([s for s in Stage.values if s != Stage.ENROLLED])
    note = serializers.CharField(max_length=500, required=False, allow_blank=True)


class EnrolIn(StrictSerializer):
    section_id = serializers.UUIDField()
    admission_number = serializers.CharField(max_length=32)
    start_date = serializers.DateField(required=False)
    roll_number = serializers.CharField(max_length=16, required=False, allow_blank=True)


class AppDocumentOut(serializers.ModelSerializer[ApplicationDocument]):
    filename = serializers.CharField(source="file.filename")
    content_type = serializers.CharField(source="file.content_type")
    size = serializers.IntegerField(source="file.size")

    class Meta:
        model = ApplicationDocument
        fields = ("id", "title", "filename", "content_type", "size", "created_at")
        read_only_fields = fields


class AppDocumentIn(StrictSerializer):
    title = serializers.CharField(max_length=200)
    file = serializers.FileField()
