from __future__ import annotations

from decimal import Decimal
from typing import Any

from rest_framework import serializers

from eduflow.academics.api.serializers import Ref
from eduflow.core.api import StrictSerializer
from eduflow.documents.api.serializers import FileOut

from ..models import (
    Candidate,
    CandidateStage,
    JobOpening,
    LeaveRequest,
    LeaveType,
    OpeningStatus,
    PayrollRun,
    Payslip,
    SalaryStructure,
    StaffAttendance,
    StaffDayStatus,
)

ZERO = Decimal(0)


def Money(**kwargs: Any) -> serializers.DecimalField:
    return serializers.DecimalField(max_digits=12, decimal_places=2, **kwargs)


class StaffRef(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    full_name = serializers.CharField(source="membership.user.full_name")
    employee_id = serializers.CharField()


class HrSettingsOut(serializers.Serializer[Any]):
    late_after = serializers.TimeField()


class HrSettingsIn(StrictSerializer):
    late_after = serializers.TimeField()


class StaffDayOut(serializers.ModelSerializer[StaffAttendance]):
    staff = StaffRef()

    class Meta:
        model = StaffAttendance
        fields = ("id", "staff", "date", "status", "check_in", "check_out", "note")
        read_only_fields = fields


class StaffDayIn(StrictSerializer):
    staff_id = serializers.UUIDField()
    date = serializers.DateField()
    status = serializers.ChoiceField(StaffDayStatus.choices)
    check_in = serializers.TimeField(required=False, allow_null=True)
    check_out = serializers.TimeField(required=False, allow_null=True)
    note = serializers.CharField(max_length=300, required=False, allow_blank=True)


class LeaveTypeOut(serializers.ModelSerializer[LeaveType]):
    class Meta:
        model = LeaveType
        fields = ("id", "name", "days_per_year", "is_active")
        read_only_fields = fields


class LeaveTypeIn(StrictSerializer):
    name = serializers.CharField(max_length=60)
    days_per_year = serializers.DecimalField(max_digits=5, decimal_places=1, min_value=ZERO)
    is_active = serializers.BooleanField(required=False, default=True)


class LeaveTypeUpdateIn(StrictSerializer):
    name = serializers.CharField(max_length=60, required=False)
    days_per_year = serializers.DecimalField(max_digits=5, decimal_places=1, min_value=ZERO, required=False)
    is_active = serializers.BooleanField(required=False)


class LeaveOut(serializers.ModelSerializer[LeaveRequest]):
    staff = StaffRef()
    leave_type = serializers.CharField(source="leave_type.name")

    class Meta:
        model = LeaveRequest
        fields = (
            "id",
            "staff",
            "leave_type",
            "start_date",
            "end_date",
            "days",
            "reason",
            "status",
            "decided_at",
            "decision_note",
            "created_at",
        )
        read_only_fields = fields


class LeaveIn(StrictSerializer):
    leave_type_id = serializers.UUIDField()
    start_date = serializers.DateField()
    end_date = serializers.DateField()
    days = serializers.DecimalField(max_digits=5, decimal_places=1, required=False, min_value=Decimal("0.5"))
    reason = serializers.CharField(max_length=500)


class BalanceOut(serializers.Serializer[Any]):
    leave_type = LeaveTypeOut()
    remaining = serializers.DecimalField(max_digits=5, decimal_places=1)


class SalaryOut(serializers.ModelSerializer[SalaryStructure]):
    staff = StaffRef()

    class Meta:
        model = SalaryStructure
        fields = (
            "staff",
            "basic",
            "hra",
            "allowances",
            "pf",
            "esi",
            "tds",
            "bank_account_last4",
            "updated_at",
        )
        read_only_fields = fields


class SalaryIn(StrictSerializer):
    basic = Money(min_value=ZERO)
    hra = Money(min_value=ZERO, required=False, default=ZERO)
    allowances = Money(min_value=ZERO, required=False, default=ZERO)
    pf = Money(min_value=ZERO, required=False, default=ZERO, help_text="Entered amount; not calculated.")
    esi = Money(min_value=ZERO, required=False, default=ZERO, help_text="Entered amount; not calculated.")
    tds = Money(min_value=ZERO, required=False, default=ZERO, help_text="Entered amount; not calculated.")
    bank_account_last4 = serializers.RegexField(r"^\d{4}$", required=False, allow_blank=True)


class RunOut(serializers.ModelSerializer[PayrollRun]):
    month = serializers.DateField(format="%Y-%m")
    gross = Money(read_only=True)
    deductions = Money(read_only=True)
    net = Money(read_only=True)
    staff_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = PayrollRun
        fields = (
            "id",
            "month",
            "status",
            "processed_at",
            "paid_at",
            "gross",
            "deductions",
            "net",
            "staff_count",
        )
        read_only_fields = fields


class RunIn(StrictSerializer):
    month = serializers.DateField(input_formats=["%Y-%m"], help_text="YYYY-MM")


class PayslipOut(serializers.ModelSerializer[Payslip]):
    staff = StaffRef()
    month = serializers.DateField(source="run.month", format="%Y-%m")
    run_status = serializers.CharField(source="run.status")

    class Meta:
        model = Payslip
        fields = (
            "id",
            "staff",
            "month",
            "run_status",
            "basic",
            "hra",
            "allowances",
            "gross",
            "pf",
            "esi",
            "tds",
            "deductions",
            "net",
        )
        read_only_fields = fields


class OpeningOut(serializers.ModelSerializer[JobOpening]):
    department = Ref(allow_null=True)

    class Meta:
        model = JobOpening
        fields = ("id", "title", "department", "positions", "description", "status", "created_at")
        read_only_fields = fields


class OpeningIn(StrictSerializer):
    title = serializers.CharField(max_length=150)
    department_id = serializers.UUIDField(required=False, allow_null=True)
    positions = serializers.IntegerField(min_value=1, max_value=1000, required=False, default=1)
    description = serializers.CharField(max_length=5000, required=False, allow_blank=True)


class OpeningUpdateIn(StrictSerializer):
    title = serializers.CharField(max_length=150, required=False)
    department_id = serializers.UUIDField(required=False, allow_null=True)
    positions = serializers.IntegerField(min_value=1, max_value=1000, required=False)
    description = serializers.CharField(max_length=5000, required=False, allow_blank=True)
    status = serializers.ChoiceField(OpeningStatus.choices, required=False)


class CandidateOut(serializers.ModelSerializer[Candidate]):
    opening_id = serializers.UUIDField()
    resume = FileOut(allow_null=True)

    class Meta:
        model = Candidate
        fields = ("id", "opening_id", "full_name", "email", "phone", "stage", "notes", "resume", "created_at")
        read_only_fields = fields


class CandidateIn(StrictSerializer):
    opening_id = serializers.UUIDField()
    full_name = serializers.CharField(max_length=200)
    email = serializers.EmailField(required=False, allow_blank=True)
    phone = serializers.CharField(max_length=32, required=False, allow_blank=True)
    notes = serializers.CharField(max_length=2000, required=False, allow_blank=True)
    resume = serializers.FileField(required=False)


class CandidateUpdateIn(StrictSerializer):
    stage = serializers.ChoiceField(CandidateStage.choices, required=False)
    notes = serializers.CharField(max_length=2000, required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    phone = serializers.CharField(max_length=32, required=False, allow_blank=True)
