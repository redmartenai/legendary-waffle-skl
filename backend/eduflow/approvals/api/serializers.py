from __future__ import annotations

from typing import Any

from rest_framework import serializers

from eduflow.core.api import StrictSerializer


class ApprovalItemOut(serializers.Serializer[Any]):
    kind = serializers.CharField()
    id = serializers.CharField()
    title = serializers.CharField()
    detail = serializers.CharField(allow_blank=True)
    subject = serializers.CharField(allow_blank=True)
    requested_by = serializers.CharField()
    requested_at = serializers.DateTimeField()
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, allow_null=True)
    waiting_hours = serializers.IntegerField()


class ApprovalDecisionIn(StrictSerializer):
    decision = serializers.ChoiceField(
        ["approve", "decline", "reject"], help_text="`reject` is an alias of `decline`."
    )
    note = serializers.CharField(max_length=500, required=False, allow_blank=True)
