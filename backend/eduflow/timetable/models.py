"""Timetables: the weekly plan of who teaches what, where and when, and the lessons that follow from it.

Model (docs/architecture/phase-5.md, ADR-026)::

    Timetable (academic year, optional term, effective dates; draft -> published -> archived)
     ├── Period         (number, start and end time, or a break; periods of one timetable never overlap)
     └── TimetableSlot  (period x weekday x section -> teacher assignment [teacher + subject], room)
           └── Lesson   (one dated occurrence of a slot: held or cancelled, topic)

**Clashes are rejected by the database.** A slot copies its period's times and its timetable's dates and
"live" flag; composite foreign keys with ``ON UPDATE CASCADE`` keep those copies equal to the originals
(timetable migration 0002), so the exclusion constraints below always see current values:

* within one timetable (drafts too): a section, a teacher or a room has at most one slot per period and day;
* across every **published** timetable of the school: a section, a teacher or a room is never in two slots
  whose days are equal, times overlap and effective dates overlap, even with different bell times.

A lesson slot's teacher and subject are copied from its teacher assignment, and another composite foreign key
keeps them equal to it, so a slot can never name a teacher who is not assigned to that section and subject.
"""

from __future__ import annotations

from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateTimeRangeField, RangeOperators
from django.db import models
from django.db.models import F, Func, Q
from django.db.models.functions import Lower

from eduflow.academics.models import AcademicYear, DateRange, Room, Section, Subject, Term
from eduflow.core.ids import uuid7
from eduflow.people.models import StaffProfile, TeacherAssignment
from eduflow.tenancy.models import Membership, TenantModel, TenantQuerySet


class TimeRange(Func):
    """``eduflow_timerange(start, end)``: a half-open range of times of day (type from migration 0001)."""

    function = "eduflow_timerange"
    output_field = DateTimeRangeField()  # nearest Django range field; only the SQL matters


def _same_school_target(model: str) -> models.UniqueConstraint:
    return models.UniqueConstraint(fields=["id", "school"], name=f"timetable_{model}_id_school_uniq")


def _dates() -> DateRange:
    return DateRange("effective_from", "effective_to", models.Value("[]"))


def _times() -> TimeRange:
    return TimeRange("start_time", "end_time")


class TimetableStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    PUBLISHED = "published", "Published"
    ARCHIVED = "archived", "Archived"


class Timetable(TenantModel):
    """A version of the weekly plan, valid between two dates of one academic year.

    Only **published** timetables produce schedules and lessons, and only they take part in cross-timetable
    clash checks. Drafts can be built and checked without affecting anyone; archived ones are history.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    academic_year = models.ForeignKey(AcademicYear, on_delete=models.PROTECT, related_name="timetables")
    term = models.ForeignKey(Term, on_delete=models.PROTECT, null=True, blank=True, related_name="timetables")
    name = models.CharField(max_length=100)
    effective_from = models.DateField()
    effective_to = models.DateField()
    status = models.CharField(max_length=16, choices=TimetableStatus.choices, default=TimetableStatus.DRAFT)
    is_live = models.BooleanField(default=False, editable=False, help_text="Equals status == published.")
    published_at = models.DateTimeField(null=True, blank=True)
    archived_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "timetable_timetable"
        constraints = [
            models.CheckConstraint(
                condition=Q(effective_from__lte=F("effective_to")), name="timetable_timetable_dates_check"
            ),
            models.CheckConstraint(
                condition=Q(is_live=True, status=TimetableStatus.PUBLISHED)
                | (Q(is_live=False) & ~Q(status=TimetableStatus.PUBLISHED)),
                name="timetable_timetable_live_is_published_check",
            ),
            models.CheckConstraint(
                condition=Q(status=TimetableStatus.DRAFT) | Q(published_at__isnull=False),
                name="timetable_timetable_published_at_check",
            ),
            models.CheckConstraint(
                condition=~Q(status=TimetableStatus.ARCHIVED) | Q(archived_at__isnull=False),
                name="timetable_timetable_archived_at_check",
            ),
            models.UniqueConstraint("academic_year", Lower("name"), name="timetable_timetable_name_uniq"),
            _same_school_target("timetable"),
            # Target of the slot's composite foreign key (ON UPDATE CASCADE): dates and live flag.
            models.UniqueConstraint(
                fields=["id", "academic_year", "effective_from", "effective_to", "is_live", "school"],
                name="timetable_timetable_slot_key_uniq",
            ),
        ]
        indexes = [models.Index(fields=["school", "academic_year", "status"], name="timetable_status_idx")]

    def __str__(self) -> str:
        return self.name


class Period(TenantModel):
    """A numbered time block of a timetable's day (or a break). Periods of one timetable never overlap."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    timetable = models.ForeignKey(Timetable, on_delete=models.CASCADE, related_name="periods")
    number = models.PositiveSmallIntegerField()
    name = models.CharField(max_length=50, help_text='For example "Period 1" or "Lunch".')
    start_time = models.TimeField()
    end_time = models.TimeField()
    is_break = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "timetable_period"
        constraints = [
            models.CheckConstraint(
                condition=Q(start_time__lt=F("end_time")), name="timetable_period_times_check"
            ),
            models.CheckConstraint(condition=Q(number__gte=1), name="timetable_period_number_positive_check"),
            models.UniqueConstraint(fields=["timetable", "number"], name="timetable_period_number_uniq"),
            ExclusionConstraint(
                name="timetable_period_no_overlap",
                expressions=[("timetable", RangeOperators.EQUAL), (_times(), RangeOperators.OVERLAPS)],
            ),
            _same_school_target("period"),
            # Target of the slot's composite foreign key (ON UPDATE CASCADE): the period's times.
            models.UniqueConstraint(
                fields=["id", "timetable", "start_time", "end_time", "school"],
                name="timetable_period_slot_key_uniq",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.number}"


class SlotKind(models.TextChoices):
    LESSON = "lesson", "Lesson"
    ACTIVITY = "activity", "Activity"


class TimetableSlot(TenantModel):
    """One section's period on one weekday: a lesson (teacher assignment) or a named activity (assembly)."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    timetable = models.ForeignKey(Timetable, on_delete=models.CASCADE, related_name="slots")
    period = models.ForeignKey(Period, on_delete=models.RESTRICT, related_name="slots")
    weekday = models.PositiveSmallIntegerField(help_text="ISO weekday: 1 = Monday ... 7 = Sunday.")
    section = models.ForeignKey(Section, on_delete=models.PROTECT, related_name="timetable_slots")
    kind = models.CharField(max_length=16, choices=SlotKind.choices, default=SlotKind.LESSON)
    assignment = models.ForeignKey(
        TeacherAssignment, on_delete=models.PROTECT, null=True, blank=True, related_name="timetable_slots"
    )
    title = models.CharField(max_length=100, blank=True)
    room = models.ForeignKey(Room, on_delete=models.PROTECT, null=True, blank=True, related_name="slots")

    # Copies kept equal to their source by composite foreign keys (see the module docstring). Never set
    # them directly: the services copy them from the timetable, the period and the assignment.
    academic_year = models.ForeignKey(AcademicYear, on_delete=models.PROTECT, related_name="+")
    effective_from = models.DateField(editable=False)
    effective_to = models.DateField(editable=False)
    is_live = models.BooleanField(editable=False)
    start_time = models.TimeField(editable=False)
    end_time = models.TimeField(editable=False)
    staff = models.ForeignKey(
        StaffProfile, on_delete=models.PROTECT, null=True, blank=True, editable=False, related_name="slots"
    )
    subject = models.ForeignKey(
        Subject, on_delete=models.PROTECT, null=True, blank=True, editable=False, related_name="slots"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "timetable_slot"
        constraints = [
            models.CheckConstraint(
                condition=Q(weekday__gte=1, weekday__lte=7), name="timetable_slot_weekday_range_check"
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        kind=SlotKind.LESSON,
                        assignment__isnull=False,
                        staff__isnull=False,
                        subject__isnull=False,
                    )
                    | (
                        Q(
                            kind=SlotKind.ACTIVITY,
                            assignment__isnull=True,
                            staff__isnull=True,
                            subject__isnull=True,
                        )
                        & ~Q(title="")
                    )
                ),
                name="timetable_slot_kind_check",
            ),
            # Within one timetable (drafts included). Periods never overlap, so this is a time clash check.
            models.UniqueConstraint(
                fields=["period", "weekday", "section"], name="timetable_slot_section_uniq"
            ),
            models.UniqueConstraint(
                fields=["period", "weekday", "staff"],
                condition=Q(staff__isnull=False),
                name="timetable_slot_staff_uniq",
            ),
            models.UniqueConstraint(
                fields=["period", "weekday", "room"],
                condition=Q(room__isnull=False),
                name="timetable_slot_room_uniq",
            ),
            # Across every published timetable: same weekday, overlapping times and overlapping dates.
            ExclusionConstraint(
                name="timetable_slot_live_section_excl",
                expressions=[
                    ("section", RangeOperators.EQUAL),
                    ("weekday", RangeOperators.EQUAL),
                    (_times(), RangeOperators.OVERLAPS),
                    (_dates(), RangeOperators.OVERLAPS),
                ],
                condition=Q(is_live=True),
            ),
            ExclusionConstraint(
                name="timetable_slot_live_staff_excl",
                expressions=[
                    ("staff", RangeOperators.EQUAL),
                    ("weekday", RangeOperators.EQUAL),
                    (_times(), RangeOperators.OVERLAPS),
                    (_dates(), RangeOperators.OVERLAPS),
                ],
                condition=Q(is_live=True, staff__isnull=False),
            ),
            ExclusionConstraint(
                name="timetable_slot_live_room_excl",
                expressions=[
                    ("room", RangeOperators.EQUAL),
                    ("weekday", RangeOperators.EQUAL),
                    (_times(), RangeOperators.OVERLAPS),
                    (_dates(), RangeOperators.OVERLAPS),
                ],
                condition=Q(is_live=True, room__isnull=False),
            ),
            _same_school_target("slot"),
            # Target of the lesson's composite foreign key.
            models.UniqueConstraint(
                fields=["id", "section", "school"], name="timetable_slot_lesson_key_uniq"
            ),
        ]
        indexes = [
            models.Index(fields=["school", "timetable", "weekday"], name="timetable_slot_day_idx"),
            models.Index(fields=["school", "section", "weekday"], name="timetable_slot_section_idx"),
        ]

    def __str__(self) -> str:
        return str(self.id)


class LessonStatus(models.TextChoices):
    HELD = "held", "Held"
    CANCELLED = "cancelled", "Cancelled"


class Lesson(TenantModel):
    """One dated occurrence of a lesson slot, recorded by its teacher (or the school): held or cancelled.

    A lesson exists only once something is recorded about it; until then the schedule shows the slot alone.
    It keeps its own teacher and subject, so history survives later changes to the timetable.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    slot = models.ForeignKey(TimetableSlot, on_delete=models.PROTECT, related_name="lessons")
    date = models.DateField()
    section = models.ForeignKey(Section, on_delete=models.PROTECT, related_name="lessons")
    staff = models.ForeignKey(StaffProfile, on_delete=models.PROTECT, related_name="lessons")
    subject = models.ForeignKey(Subject, on_delete=models.PROTECT, related_name="lessons")
    status = models.CharField(max_length=16, choices=LessonStatus.choices, default=LessonStatus.HELD)
    topic = models.CharField(max_length=300, blank=True)
    cancellation_reason = models.CharField(max_length=200, blank=True)
    recorded_by = models.ForeignKey(Membership, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantQuerySet.as_manager()

    class Meta:
        db_table = "timetable_lesson"
        constraints = [
            models.UniqueConstraint(fields=["slot", "date"], name="timetable_lesson_slot_date_uniq"),
            models.CheckConstraint(
                condition=Q(status=LessonStatus.CANCELLED) | Q(cancellation_reason=""),
                name="timetable_lesson_reason_only_when_cancelled_check",
            ),
            _same_school_target("lesson"),
        ]
        indexes = [
            models.Index(fields=["school", "section", "date"], name="timetable_lesson_section_idx"),
            models.Index(fields=["school", "staff", "date"], name="timetable_lesson_staff_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.slot_id}@{self.date}"
