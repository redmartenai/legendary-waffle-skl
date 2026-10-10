from __future__ import annotations

from typing import Any

from rest_framework import serializers

from eduflow.core.api import StrictSerializer
from eduflow.people.api.serializers import PersonRef

from ..models import Visit


class VisitOut(serializers.ModelSerializer[Visit]):
    host = serializers.CharField(source="host.user.full_name", allow_null=True, default=None)
    student = PersonRef(allow_null=True)

    class Meta:
        model = Visit
        fields = (
            "id",
            "visitor_name",
            "phone",
            "purpose",
            "visitors_count",
            "vehicle_number",
            "id_proof",
            "expected_on",
            "host",
            "student",
            "status",
            "decided_at",
            "decision_note",
            "checked_in_at",
            "checked_out_at",
            "created_at",
        )
        read_only_fields = fields


class VisitWithPassOut(serializers.Serializer[Any]):
    visit = VisitOut()
    pass_token = serializers.CharField(
        allow_null=True, help_text="Shown once; encode it as the QR code. Only its hash is stored."
    )


class VisitIn(StrictSerializer):
    visitor_name = serializers.CharField(max_length=200)
    phone = serializers.CharField(max_length=32)
    purpose = serializers.CharField(max_length=300)
    expected_on = serializers.DateField(required=False)
    host_id = serializers.UUIDField(required=False, allow_null=True, help_text="Security only.")
    student_id = serializers.UUIDField(required=False, allow_null=True)
    visitors_count = serializers.IntegerField(min_value=1, max_value=50, required=False, default=1)
    vehicle_number = serializers.CharField(max_length=20, required=False, allow_blank=True)
    id_proof = serializers.CharField(max_length=100, required=False, allow_blank=True)


class VisitDecisionIn(StrictSerializer):
    decision = serializers.ChoiceField(["approve", "decline"])
    note = serializers.CharField(max_length=300, required=False, allow_blank=True)


class ScanIn(StrictSerializer):
    token = serializers.CharField(max_length=100)


class PassOut(serializers.Serializer[Any]):
    pass_token = serializers.CharField()
