"""Attendance serializers.

The client-facing shapes follow the documented contract (CURRENT_STATE §5): ``ClassRoster``, the
exceptions-only submission, and ``AttendanceMonth``. Where the documents name a type without its fields
(``AttendanceSummary``, the month ``summary``), the fields here are the minimal ones and are recorded in
docs/architecture/attendance.md as decisions for review.
"""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from eduflow.academics.api.serializers import Ref
from eduflow.core.api import StrictSerializer
from eduflow.people.api.serializers import PersonRef

from ..models import (
    AttendanceCorrection,
    AttendanceRecord,
    AttendanceSession,
    AttendanceStatus,
    CorrectionStatus,
)


class _WithClass(serializers.Serializer[Any]):
    """Publishes the ``klass`` field as ``class`` (a Python keyword, so it cannot be an attribute)."""

    def get_fields(self) -> dict[str, Any]:
        fields = super().get_fields()
        fields["class"] = fields.pop("klass")
        fields["class"].source = "klass"
        return fields


class ClassRef(serializers.Serializer[Any]):
    """The client's "class": a homeroom section (ADR-007)."""

    id = serializers.UUIDField()
    label = serializers.CharField(help_text='For example "Grade 9 · B".')  # type: ignore[assignment]
    short_label = serializers.CharField(help_text="The section code.")


def class_ref(section: Any) -> dict[str, Any]:
    return {"id": section.pk, "label": f"{section.grade.name} · {section.name}", "short_label": section.code}


class StatusCounts(serializers.Serializer[Any]):
    present = serializers.IntegerField()
    absent = serializers.IntegerField()
    late = serializers.IntegerField()
    half_day = serializers.IntegerField()
    excused = serializers.IntegerField()


# ------------------------------------------------------------------------------------------------ registers
class RosterStudent(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    name = serializers.CharField()
    initials = serializers.CharField()
    roll_no = serializers.CharField(allow_blank=True)
    status = serializers.ChoiceField(
        AttendanceStatus.choices, allow_null=True, help_text="Null until the register is taken."
    )


class ClassRosterOut(_WithClass):
    klass = ClassRef()
    date = serializers.DateField()
    marked = serializers.BooleanField()
    marked_at = serializers.DateTimeField(allow_null=True)
    cutoff = serializers.DateTimeField(help_text="When the register locks: the end of the date, school time.")
    locked = serializers.BooleanField()
    students = RosterStudent(many=True)


class RosterQuery(StrictSerializer):
    date = serializers.DateField(required=False, help_text="Defaults to today (school time).")


MAX_ENTRIES = 500  # far above any class size; bounds the work one request can ask for


class EntryIn(StrictSerializer):
    student_id = serializers.UUIDField()
    status = serializers.ChoiceField(AttendanceStatus.choices)
    note = serializers.CharField(max_length=200, required=False, allow_blank=True)


class SubmitIn(StrictSerializer):
    entries: serializers.ListSerializer[Any] = serializers.ListSerializer(
        child=EntryIn(),
        max_length=MAX_ENTRIES,
        help_text="Exceptions only: every student on the roster not listed is present.",
    )
    client_id = serializers.CharField(
        max_length=64,
        required=False,
        allow_blank=True,
        help_text="One per draft, reused on retry: a retry returns the stored register unchanged.",
    )
    date = serializers.DateField(required=False, help_text="Defaults to today (school time).")


class AttendanceSummaryOut(_WithClass):
    session_id = serializers.UUIDField()
    klass = ClassRef()
    date = serializers.DateField()
    marked_at = serializers.DateTimeField()
    cutoff = serializers.DateTimeField()
    locked = serializers.BooleanField()
    total = serializers.IntegerField()
    counts = StatusCounts()
    replayed = serializers.BooleanField(help_text="True when this was a retry of an accepted submission.")


# ------------------------------------------------------------------------------------------------ month view
class MonthQuery(StrictSerializer):
    month = serializers.RegexField(
        r"^\d{4}-(0[1-9]|1[0-2])$", required=False, help_text="YYYY-MM. Defaults to the current month."
    )


class DayOut(serializers.Serializer[Any]):
    date = serializers.DateField()
    status = serializers.ChoiceField(
        [*AttendanceStatus.values, "not_marked", "upcoming"],
        allow_null=True,
        help_text="Null on days the student was not enrolled.",
    )


class MonthSummary(StatusCounts):
    not_marked = serializers.IntegerField()
    marked_days = serializers.IntegerField()


class AttendanceMonthOut(serializers.Serializer[Any]):
    month = serializers.CharField()
    days = DayOut(many=True)
    summary = MonthSummary()


# ------------------------------------------------------------------------------------------------ resources
class RegisterOut(serializers.ModelSerializer[AttendanceSession]):
    section = Ref()
    taken_by = PersonRef(source="taken_by.user")
    locked = serializers.SerializerMethodField()
    counts = serializers.SerializerMethodField()

    class Meta:
        model = AttendanceSession
        fields = ("id", "section", "date", "taken_by", "submitted_at", "locked_at", "locked", "counts")
        read_only_fields = fields

    def get_locked(self, obj: AttendanceSession) -> bool:
        from ..services import is_locked

        return is_locked(obj)

    @extend_schema_field(StatusCounts)
    def get_counts(self, obj: AttendanceSession) -> dict[str, int]:
        return {s: getattr(obj, f"n_{s}", 0) for s in AttendanceStatus.values}


class RecordOut(serializers.ModelSerializer[AttendanceRecord]):
    student = PersonRef()
    section = Ref()

    class Meta:
        model = AttendanceRecord
        fields = ("id", "session_id", "date", "section", "student", "status", "note", "updated_at")
        read_only_fields = fields


class CorrectionOut(serializers.ModelSerializer[AttendanceCorrection]):
    record = RecordOut()
    requested_by = PersonRef(source="requested_by.user")
    decided_by = PersonRef(source="decided_by.user", allow_null=True)

    class Meta:
        model = AttendanceCorrection
        fields = (
            "id",
            "record",
            "old_status",
            "new_status",
            "reason",
            "status",
            "requested_by",
            "created_at",
            "decided_by",
            "decided_at",
            "decision_note",
        )
        read_only_fields = fields


class CorrectionIn(StrictSerializer):
    record_id = serializers.UUIDField()
    new_status = serializers.ChoiceField(AttendanceStatus.choices)
    reason = serializers.CharField(max_length=500)


class DecisionIn(StrictSerializer):
    note = serializers.CharField(max_length=500, required=False, allow_blank=True)


CORRECTION_STATUS = serializers.ChoiceField(CorrectionStatus.choices)
ATTENDANCE_STATUS = serializers.ChoiceField(AttendanceStatus.choices)
