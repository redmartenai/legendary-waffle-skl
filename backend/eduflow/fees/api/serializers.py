from __future__ import annotations

from decimal import Decimal
from typing import Any

from rest_framework import serializers

from eduflow.academics.api.serializers import Ref
from eduflow.core.api import StrictSerializer
from eduflow.people.api.serializers import PersonRef

from ..models import FeePlan, Instalment, Payment, PaymentMode, Refund, StudentFee

CENT = Decimal("0.01")


def Money(**kwargs: Any) -> serializers.DecimalField:
    return serializers.DecimalField(max_digits=12, decimal_places=2, **kwargs)


class InstalmentOut(serializers.ModelSerializer[Instalment]):
    class Meta:
        model = Instalment
        fields = ("id", "label", "due_date", "amount")
        read_only_fields = fields


class InstalmentIn(StrictSerializer):
    label = serializers.CharField(max_length=60)  # type: ignore[assignment]
    due_date = serializers.DateField()
    amount = Money(min_value=CENT)


class PlanOut(serializers.ModelSerializer[FeePlan]):
    academic_year = Ref()
    grade = Ref(allow_null=True)
    instalments = InstalmentOut(many=True)
    total = Money()

    class Meta:
        model = FeePlan
        fields = ("id", "name", "academic_year", "grade", "instalments", "total", "created_at")
        read_only_fields = fields


class PlanIn(StrictSerializer):
    name = serializers.CharField(max_length=100)
    academic_year_id = serializers.UUIDField()
    grade_id = serializers.UUIDField(required=False, allow_null=True)
    instalments = InstalmentIn(many=True, allow_empty=False)


class PlanUpdateIn(StrictSerializer):
    name = serializers.CharField(max_length=100, required=False)
    instalments = InstalmentIn(many=True, allow_empty=False, required=False)


class AssignSectionIn(StrictSerializer):
    section_id = serializers.UUIDField()


class AssignedOut(serializers.Serializer[Any]):
    assigned = serializers.IntegerField()


class StudentFeeOut(serializers.ModelSerializer[StudentFee]):
    student = PersonRef()
    plan = Ref()

    class Meta:
        model = StudentFee
        fields = ("id", "student", "plan", "scholarship_percent", "scholarship_note", "created_at")
        read_only_fields = fields


class StudentFeeIn(StrictSerializer):
    student_id = serializers.UUIDField()
    plan_id = serializers.UUIDField()
    scholarship_percent = serializers.DecimalField(
        max_digits=5, decimal_places=2, min_value=0, max_value=100, required=False
    )
    scholarship_note = serializers.CharField(max_length=200, required=False, allow_blank=True)


class StudentFeeUpdateIn(StrictSerializer):
    scholarship_percent = serializers.DecimalField(
        max_digits=5, decimal_places=2, min_value=0, max_value=100, required=False
    )
    scholarship_note = serializers.CharField(max_length=200, required=False, allow_blank=True)


class PaymentOut(serializers.ModelSerializer[Payment]):
    student = PersonRef()
    collected_by = serializers.CharField(source="collected_by.user.full_name")

    class Meta:
        model = Payment
        fields = (
            "id",
            "student",
            "amount",
            "mode",
            "reference",
            "paid_on",
            "receipt_number",
            "note",
            "collected_by",
            "created_at",
        )
        read_only_fields = fields


class PaymentIn(StrictSerializer):
    student_id = serializers.UUIDField()
    amount = Money(min_value=CENT)
    mode = serializers.ChoiceField(PaymentMode.choices)
    paid_on = serializers.DateField(required=False)
    reference = serializers.CharField(max_length=100, required=False, allow_blank=True)
    note = serializers.CharField(max_length=300, required=False, allow_blank=True)
    client_key = serializers.CharField(
        max_length=64, required=False, allow_blank=True, help_text="Or the `Idempotency-Key` header."
    )


class RefundOut(serializers.ModelSerializer[Refund]):
    payment_id = serializers.UUIDField()
    receipt_number = serializers.CharField(source="payment.receipt_number")

    class Meta:
        model = Refund
        fields = (
            "id",
            "payment_id",
            "receipt_number",
            "amount",
            "reason",
            "status",
            "decided_at",
            "decision_note",
            "created_at",
        )
        read_only_fields = fields


class RefundIn(StrictSerializer):
    amount = Money(min_value=CENT)
    reason = serializers.CharField(max_length=500)


class LineOut(serializers.Serializer[Any]):
    plan = serializers.CharField()
    label = serializers.CharField()  # type: ignore[assignment]
    due_date = serializers.DateField()
    amount = Money()


class StatementOut(serializers.Serializer[Any]):
    student = PersonRef()
    total = Money()
    due_to_date = Money()
    paid = Money()
    refunded = Money()
    net_paid = Money()
    balance = Money()
    overdue = Money()
    next_due = serializers.DateField(allow_null=True)
    lines = LineOut(many=True)


class CollectionsOut(serializers.Serializer[Any]):
    received = Money()
    refunded = Money()
    net = Money()
    by_mode = serializers.DictField(child=Money())
