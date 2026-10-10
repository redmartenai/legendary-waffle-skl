from __future__ import annotations

from decimal import Decimal
from typing import Any

from rest_framework import serializers

from eduflow.core.api import StrictSerializer
from eduflow.people.api.serializers import PersonRef

from ..models import Alert, MonitoringSettings

THRESHOLDS = (
    "attendance_risk",
    "attendance_window_days",
    "slip_drop",
    "slip_recent_days",
    "absent_streak_days",
    "marks_drop",
    "behaviour_incidents",
    "fee_overdue",
    "reply_hours",
    "unreviewed_submissions",
    "late_arrivals",
    "late_window_days",
    "approval_hours",
    "bus_delay_minutes",
    "register_by",
)


class AlertItemOut(serializers.Serializer[Any]):
    id = serializers.CharField()
    label = serializers.CharField()  # type: ignore[assignment]
    sub = serializers.CharField(allow_blank=True)
    value = serializers.CharField()
    student_id = serializers.CharField(required=False, allow_null=True)
    section_id = serializers.CharField(required=False, allow_null=True)
    staff_id = serializers.CharField(required=False, allow_null=True)


class AlertOut(serializers.ModelSerializer[Alert]):
    items = AlertItemOut(many=True)

    class Meta:
        model = Alert
        fields = (
            "id",
            "rule",
            "title",
            "domain",
            "severity",
            "headline",
            "why",
            "owner",
            "escalates_to",
            "items",
            "status",
            "first_seen_at",
            "last_seen_at",
            "occurrences",
            "escalated_at",
            "acknowledged_at",
            "resolved_at",
            "auto_resolved",
            "resolution_note",
        )
        read_only_fields = fields


class AlertResolveIn(StrictSerializer):
    note = serializers.CharField(max_length=500, required=False, allow_blank=True)


class MonitoringSettingsOut(serializers.ModelSerializer[MonitoringSettings]):
    class Meta:
        model = MonitoringSettings
        fields = THRESHOLDS
        read_only_fields = fields


class MonitoringSettingsIn(StrictSerializer):
    attendance_risk = serializers.DecimalField(
        max_digits=3, decimal_places=2, min_value=Decimal("0.01"), max_value=Decimal(1), required=False
    )
    attendance_window_days = serializers.IntegerField(min_value=5, max_value=365, required=False)
    slip_drop = serializers.DecimalField(
        max_digits=3, decimal_places=2, min_value=Decimal("0.01"), max_value=Decimal(1), required=False
    )
    slip_recent_days = serializers.IntegerField(min_value=3, max_value=90, required=False)
    absent_streak_days = serializers.IntegerField(min_value=2, max_value=30, required=False)
    marks_drop = serializers.DecimalField(
        max_digits=5, decimal_places=2, min_value=Decimal(1), max_value=Decimal(100), required=False
    )
    behaviour_incidents = serializers.IntegerField(min_value=1, max_value=100, required=False)
    fee_overdue = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=Decimal(0), required=False
    )
    reply_hours = serializers.IntegerField(min_value=1, max_value=720, required=False)
    unreviewed_submissions = serializers.IntegerField(min_value=1, max_value=10000, required=False)
    late_arrivals = serializers.IntegerField(min_value=1, max_value=60, required=False)
    late_window_days = serializers.IntegerField(min_value=1, max_value=365, required=False)
    approval_hours = serializers.IntegerField(min_value=1, max_value=720, required=False)
    bus_delay_minutes = serializers.IntegerField(min_value=1, max_value=600, required=False)
    register_by = serializers.TimeField(required=False)


class RuleOut(serializers.Serializer[Any]):
    key = serializers.CharField()
    title = serializers.CharField()
    domain = serializers.CharField()
    severity = serializers.CharField()
    owner = serializers.CharField()
    escalates_to = serializers.CharField()
    escalate_after_hours = serializers.IntegerField(allow_null=True)


class EvaluatedOut(serializers.Serializer[Any]):
    opened = serializers.IntegerField()
    resolved = serializers.IntegerField()
    escalated = serializers.IntegerField()
    live = serializers.IntegerField()


class FactorOut(serializers.Serializer[Any]):
    key = serializers.CharField()
    label = serializers.CharField()  # type: ignore[assignment]
    weight = serializers.IntegerField()


class RiskOut(serializers.Serializer[Any]):
    student = PersonRef()
    score = serializers.IntegerField()
    level = serializers.ChoiceField(["ok", "watch", "at_risk"])
    factors = FactorOut(many=True)


class ScorecardOut(serializers.Serializer[Any]):
    staff_id = serializers.UUIDField(source="staff.pk")
    name = serializers.CharField(source="staff.membership.user.full_name")
    score = serializers.IntegerField()
    grade = serializers.CharField()
    punctuality = serializers.FloatField()
    marks_on_time = serializers.FloatField()
    review_rate = serializers.FloatField()
    reply_hours = serializers.FloatField(allow_null=True)
    class_avg_delta = serializers.FloatField()
    pending_marks = serializers.IntegerField()
    unreviewed = serializers.IntegerField()
    unanswered = serializers.IntegerField()
    no_data = serializers.ListField(
        child=serializers.CharField(), help_text="Components without data (scored as full)."
    )


class TrendPointOut(serializers.Serializer[Any]):
    date = serializers.DateField()
    rate = serializers.FloatField(allow_null=True)


class PulseOut(serializers.Serializer[Any]):
    date = serializers.DateField()
    students = serializers.IntegerField()
    registers_marked = serializers.IntegerField()
    registers_due = serializers.IntegerField()
    present_today = serializers.IntegerField()
    marked_today = serializers.IntegerField()
    attendance_today = serializers.FloatField(allow_null=True)
    staff_in = serializers.IntegerField()
    staff_total = serializers.IntegerField()
    trips_arrived = serializers.IntegerField()
    trips_today = serializers.IntegerField()
    pending_approvals = serializers.IntegerField()
    collected_this_month = serializers.DecimalField(max_digits=14, decimal_places=2)
    open_alerts = serializers.DictField(child=serializers.IntegerField())
    attendance_trend = TrendPointOut(many=True)
    brief = serializers.CharField()
    focus = serializers.CharField()


class AskIn(StrictSerializer):
    question = serializers.CharField(max_length=300)


class AskRowOut(serializers.Serializer[Any]):
    id = serializers.CharField()
    cells = serializers.ListField(child=serializers.CharField())
    tone = serializers.CharField(allow_null=True)


class AnswerOut(serializers.Serializer[Any]):
    intent = serializers.CharField()
    summary = serializers.CharField()
    columns = serializers.ListField(child=serializers.CharField())
    rows = AskRowOut(many=True)
    follow_ups = serializers.ListField(child=serializers.CharField())


class SuggestionsOut(serializers.Serializer[Any]):
    suggestions = serializers.ListField(child=serializers.CharField())
