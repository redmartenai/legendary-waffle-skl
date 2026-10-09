"""Timetable serializers. Compact one-level references only; a teacher is an ID and a display name."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from eduflow.academics.api.serializers import Ref
from eduflow.core.api import StrictSerializer

from ..models import Lesson, LessonStatus, Period, SlotKind, Timetable, TimetableSlot, TimetableStatus

WEEKDAY = serializers.IntegerField(
    min_value=1, max_value=7, help_text="ISO weekday: 1 = Monday ... 7 = Sunday."
)


class TeacherRef(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    full_name = serializers.CharField(source="membership.user.full_name")


class PeriodRef(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    number = serializers.IntegerField()
    name = serializers.CharField()
    start_time = serializers.TimeField()
    end_time = serializers.TimeField()


# ------------------------------------------------------------------------------------------------ timetable
class TimetableOut(serializers.ModelSerializer[Timetable]):
    academic_year = Ref()
    term = Ref(allow_null=True)

    class Meta:
        model = Timetable
        fields = (
            "id",
            "name",
            "academic_year",
            "term",
            "effective_from",
            "effective_to",
            "status",
            "published_at",
            "archived_at",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class TimetableCreateIn(StrictSerializer):
    academic_year_id = serializers.UUIDField()
    name = serializers.CharField(max_length=100)
    term_id = serializers.UUIDField(required=False, allow_null=True)
    effective_from = serializers.DateField(
        required=False, help_text="Defaults to the start of the term, or of the academic year."
    )
    effective_to = serializers.DateField(
        required=False, help_text="Defaults to the end of the term, or of the academic year."
    )


class TimetableUpdateIn(StrictSerializer):
    name = serializers.CharField(max_length=100, required=False)
    term_id = serializers.UUIDField(required=False, allow_null=True, help_text="Drafts only.")
    effective_from = serializers.DateField(required=False)
    effective_to = serializers.DateField(required=False)


class TimetableCopyIn(StrictSerializer):
    name = serializers.CharField(max_length=100)
    term_id = serializers.UUIDField(required=False, allow_null=True)
    effective_from = serializers.DateField(required=False)
    effective_to = serializers.DateField(required=False)


# ------------------------------------------------------------------------------------------------ period
class PeriodOut(serializers.ModelSerializer[Period]):
    class Meta:
        model = Period
        fields = (
            "id",
            "timetable_id",
            "number",
            "name",
            "start_time",
            "end_time",
            "is_break",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class PeriodCreateIn(StrictSerializer):
    timetable_id = serializers.UUIDField()
    number = serializers.IntegerField(min_value=1, max_value=99)
    name = serializers.CharField(max_length=50)
    start_time = serializers.TimeField()
    end_time = serializers.TimeField()
    is_break = serializers.BooleanField(required=False)


class PeriodUpdateIn(StrictSerializer):
    """A period's timetable is fixed. New times also move the period's slots (and are clash-checked)."""

    number = serializers.IntegerField(min_value=1, max_value=99, required=False)
    name = serializers.CharField(max_length=50, required=False)
    start_time = serializers.TimeField(required=False)
    end_time = serializers.TimeField(required=False)
    is_break = serializers.BooleanField(required=False)


# ------------------------------------------------------------------------------------------------ slot
class SlotOut(serializers.ModelSerializer[TimetableSlot]):
    period = PeriodRef()
    section = Ref()
    subject = Ref(allow_null=True)
    teacher = TeacherRef(source="staff", allow_null=True)
    room = Ref(allow_null=True)

    class Meta:
        model = TimetableSlot
        fields = (
            "id",
            "timetable_id",
            "period",
            "weekday",
            "section",
            "kind",
            "assignment_id",
            "subject",
            "teacher",
            "title",
            "room",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class SlotCreateIn(StrictSerializer):
    timetable_id = serializers.UUIDField()
    period_id = serializers.UUIDField()
    weekday = WEEKDAY
    section_id = serializers.UUIDField()
    kind = serializers.ChoiceField(SlotKind.choices, required=False)
    assignment_id = serializers.UUIDField(
        required=False,
        allow_null=True,
        help_text="Lessons: an active subject assignment of this section. Gives the teacher and subject.",
    )
    title = serializers.CharField(max_length=100, required=False, allow_blank=True)
    room_id = serializers.UUIDField(required=False, allow_null=True)


class SlotUpdateIn(StrictSerializer):
    """A slot's timetable, section and kind are fixed; delete it and create another instead."""

    period_id = serializers.UUIDField(required=False)
    weekday = serializers.IntegerField(min_value=1, max_value=7, required=False)
    assignment_id = serializers.UUIDField(required=False, allow_null=True)
    title = serializers.CharField(max_length=100, required=False, allow_blank=True)
    room_id = serializers.UUIDField(required=False, allow_null=True)


# ------------------------------------------------------------------------------------------------ lesson
class LessonOut(serializers.ModelSerializer[Lesson]):
    section = Ref()
    subject = Ref()
    teacher = TeacherRef(source="staff")

    class Meta:
        model = Lesson
        fields = (
            "id",
            "slot_id",
            "date",
            "section",
            "subject",
            "teacher",
            "status",
            "topic",
            "cancellation_reason",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class LessonCreateIn(StrictSerializer):
    slot_id = serializers.UUIDField()
    date = serializers.DateField()
    status = serializers.ChoiceField(LessonStatus.choices, required=False)
    topic = serializers.CharField(max_length=300, required=False, allow_blank=True)
    cancellation_reason = serializers.CharField(max_length=200, required=False, allow_blank=True)


class LessonUpdateIn(StrictSerializer):
    status = serializers.ChoiceField(LessonStatus.choices, required=False)
    topic = serializers.CharField(max_length=300, required=False, allow_blank=True)
    cancellation_reason = serializers.CharField(max_length=200, required=False, allow_blank=True)


# ------------------------------------------------------------------------------------------------ schedule
class ScheduleQuery(StrictSerializer):
    date_from = serializers.DateField(required=False, help_text="Defaults to this week's Monday.")
    date_to = serializers.DateField(
        required=False, help_text="Defaults to date_from + 6 days. At most 42 days."
    )


class LessonRef(serializers.Serializer[Any]):
    id = serializers.UUIDField()
    status = serializers.ChoiceField(LessonStatus.choices)
    topic = serializers.CharField()


class ScheduleEntryOut(serializers.Serializer[Any]):
    date = serializers.DateField()
    weekday = serializers.IntegerField(source="slot.weekday")
    role = serializers.ChoiceField(
        ["teacher", "student"], allow_null=True, help_text="In /schedule/me: whose entry this is."
    )
    slot_id = serializers.UUIDField(source="slot.pk")
    kind = serializers.ChoiceField(SlotKind.choices, source="slot.kind")
    title = serializers.CharField(source="slot.title")
    period = PeriodRef(source="slot.period")
    section = Ref(source="slot.section")
    subject = Ref(source="slot.subject", allow_null=True)
    teacher = TeacherRef(allow_null=True, help_text="Null while the class has no active teacher assignment.")
    room = Ref(source="slot.room", allow_null=True)
    lesson = LessonRef(allow_null=True, help_text="Present once the lesson has been recorded.")


class ScheduleOut(serializers.Serializer[Any]):
    date_from = serializers.DateField()
    date_to = serializers.DateField()
    entries = ScheduleEntryOut(many=True)


TIMETABLE_STATUS = serializers.ChoiceField(TimetableStatus.choices)
