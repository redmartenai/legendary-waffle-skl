from __future__ import annotations

from typing import Any

from rest_framework import serializers

from eduflow.core.api import StrictSerializer

from ..models import Audience, Document


class FileOut(serializers.Serializer[Any]):
    filename = serializers.CharField()
    content_type = serializers.CharField()
    size = serializers.IntegerField()


class DocumentOut(serializers.ModelSerializer[Document]):
    file = FileOut()
    student_id = serializers.UUIDField(allow_null=True)
    staff_id = serializers.UUIDField(allow_null=True)
    audiences = serializers.ListField(child=serializers.ChoiceField(Audience.choices))

    class Meta:
        model = Document
        fields = (
            "id",
            "title",
            "category",
            "file",
            "student_id",
            "staff_id",
            "audiences",
            "archived_at",
            "created_at",
        )
        read_only_fields = fields


class DocumentIn(StrictSerializer):
    file = serializers.FileField(
        help_text="PDF, PNG, JPEG, WebP, Word, Excel, PowerPoint or plain text; 10 MB."
    )
    title = serializers.CharField(max_length=200)
    category = serializers.CharField(max_length=60, required=False, allow_blank=True)
    audiences = serializers.ListField(
        child=serializers.ChoiceField(Audience.choices), min_length=1, max_length=7
    )
    student_id = serializers.UUIDField(required=False, allow_null=True)
    staff_id = serializers.UUIDField(required=False, allow_null=True)
