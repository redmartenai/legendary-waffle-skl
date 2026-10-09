"""Schedules: the published timetables expanded into dated entries, with any recorded lessons attached.

A schedule is computed, not stored. For a date range (at most ``MAX_DAYS``), each live slot whose weekday and
effective dates match a date becomes an entry. Three queries at most: the slots (with every reference joined),
the lessons in the range, and for a student their enrollments.

Whose schedule a caller may see is decided by the views: the teacher, student or section must be visible to
them (``timetable/api/views.py``). The functions here take the school's slots and select the subject's.
"""

from __future__ import annotations

import datetime
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from django.db.models import Q, QuerySet

from eduflow.academics.models import Section
from eduflow.people.models import AssignmentStatus, Enrollment, StaffProfile, Student

from .models import Lesson, TimetableSlot

MAX_DAYS = 42


@dataclass(frozen=True)
class Entry:
    date: datetime.date
    slot: TimetableSlot
    lesson: Lesson | None
    role: str | None = None  # "teacher" or "student" in a personal schedule

    @property
    def teacher(self) -> StaffProfile | None:
        """The slot's teacher while their assignment lasts; ``None`` means the class needs a new teacher."""
        assignment = self.slot.assignment
        if assignment is None or assignment.status != AssignmentStatus.ACTIVE:
            return None
        return self.slot.staff


def _live(
    slots: QuerySet[TimetableSlot], start: datetime.date, end: datetime.date
) -> QuerySet[TimetableSlot]:
    return slots.filter(is_live=True, effective_from__lte=end, effective_to__gte=start).select_related(
        "period", "section", "subject", "room", "assignment", "staff__membership__user"
    )


def _dates(start: datetime.date, end: datetime.date) -> Iterable[datetime.date]:
    day = start
    while day <= end:
        yield day
        day += datetime.timedelta(days=1)


def _expand(
    slots: Iterable[TimetableSlot],
    start: datetime.date,
    end: datetime.date,
    *,
    section_on: dict[datetime.date, Any] | None = None,
    role: str | None = None,
) -> list[Entry]:
    by_weekday: dict[int, list[TimetableSlot]] = defaultdict(list)
    for slot in slots:
        by_weekday[slot.weekday].append(slot)
    pairs = [
        (day, slot)
        for day in _dates(start, end)
        for slot in by_weekday.get(day.isoweekday(), ())
        if slot.effective_from <= day <= slot.effective_to
        and (section_on is None or section_on.get(day) == slot.section_id)
    ]
    lessons = (
        {
            (lesson.slot_id, lesson.date): lesson
            for lesson in Lesson.objects.filter(
                slot_id__in={slot.pk for _, slot in pairs}, date__gte=start, date__lte=end
            )
        }
        if pairs
        else {}
    )
    return [Entry(day, slot, lessons.get((slot.pk, day)), role) for day, slot in pairs]


def sort(entries: list[Entry]) -> list[Entry]:
    return sorted(entries, key=lambda e: (e.date, e.slot.start_time, e.slot.section.code, str(e.slot.pk)))


def staff_schedule(
    slots: QuerySet[TimetableSlot], staff: StaffProfile, start: datetime.date, end: datetime.date, **kw: Any
) -> list[Entry]:
    """What a teacher teaches: slots of their *active* assignments."""
    slots = _live(slots.filter(staff=staff, assignment__status=AssignmentStatus.ACTIVE), start, end)
    return _expand(slots, start, end, **kw)


def section_schedule(
    slots: QuerySet[TimetableSlot], section: Section, start: datetime.date, end: datetime.date
) -> list[Entry]:
    return _expand(_live(slots.filter(section=section), start, end), start, end)


def student_schedule(
    slots: QuerySet[TimetableSlot], student: Student, start: datetime.date, end: datetime.date, **kw: Any
) -> list[Entry]:
    """The schedule of the section the student was enrolled in on each date (the latest start wins on the
    day of a transfer)."""
    enrollments = (
        Enrollment.objects.filter(student=student, start_date__lte=end)
        .filter(Q(end_date__isnull=True) | Q(end_date__gte=start))
        .order_by("start_date")
        .values_list("section_id", "start_date", "end_date")
    )
    section_on: dict[datetime.date, Any] = {}
    for section_id, first, last in enrollments:
        for day in _dates(max(first, start), min(last or end, end)):
            section_on[day] = section_id
    if not section_on:
        return []
    slots = _live(slots.filter(section_id__in=set(section_on.values())), start, end)
    return _expand(slots, start, end, section_on=section_on, **kw)
