from __future__ import annotations

from rest_framework import serializers

from eduflow.academics.api.serializers import Ref
from eduflow.core.api import StrictSerializer
from eduflow.people.api.serializers import PersonRef

from ..models import Incident, Remark, Severity, Tone


class RemarkOut(serializers.ModelSerializer[Remark]):
    student = PersonRef()
    subject = Ref(allow_null=True)
    author = serializers.CharField(source="author.user.full_name", allow_null=True, default=None)

    class Meta:
        model = Remark
        fields = ("id", "student", "subject", "tone", "text", "visible_to_family", "author", "created_at")
        read_only_fields = fields


class RemarkIn(StrictSerializer):
    student_id = serializers.UUIDField()
    subject_id = serializers.UUIDField(required=False, allow_null=True)
    tone = serializers.ChoiceField(Tone.choices)
    text = serializers.CharField(max_length=2000)
    visible_to_family = serializers.BooleanField(required=False, default=True)


class RemarkUpdateIn(StrictSerializer):
    tone = serializers.ChoiceField(Tone.choices, required=False)
    text = serializers.CharField(max_length=2000, required=False)
    visible_to_family = serializers.BooleanField(required=False)


class IncidentOut(serializers.ModelSerializer[Incident]):
    student = PersonRef()
    reported_by = serializers.CharField(source="reported_by.user.full_name", allow_null=True, default=None)

    class Meta:
        model = Incident
        fields = (
            "id",
            "student",
            "occurred_on",
            "category",
            "description",
            "severity",
            "action_taken",
            "visible_to_family",
            "status",
            "reported_by",
            "resolved_at",
            "created_at",
        )
        read_only_fields = fields


class IncidentIn(StrictSerializer):
    student_id = serializers.UUIDField()
    occurred_on = serializers.DateField()
    category = serializers.CharField(max_length=60)
    description = serializers.CharField(max_length=2000)
    severity = serializers.ChoiceField(Severity.choices)
    action_taken = serializers.CharField(max_length=1000, required=False, allow_blank=True)
    visible_to_family = serializers.BooleanField(required=False, default=False)


class IncidentUpdateIn(StrictSerializer):
    category = serializers.CharField(max_length=60, required=False)
    description = serializers.CharField(max_length=2000, required=False)
    severity = serializers.ChoiceField(Severity.choices, required=False)
    action_taken = serializers.CharField(max_length=1000, required=False, allow_blank=True)
    visible_to_family = serializers.BooleanField(required=False)


class IncidentResolveIn(StrictSerializer):
    action_taken = serializers.CharField(max_length=1000, required=False, allow_blank=True)
