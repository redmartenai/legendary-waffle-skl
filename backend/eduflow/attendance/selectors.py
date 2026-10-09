"""Attendance reads: who is on a section's roster on a date, and a student's month.

**Roster rule.** A student is on a section's roster for a date when one of their enrollments in that section
covers the date (``start_date <= date <= end_date``, or no end yet) and no *later-starting* enrollment of
theirs also covers it. On the day of a transfer both enrollments cover the date (the old one ends that day,
the new one starts it): the student belongs to the new section, as in the timetable schedules. A withdrawn or
completed student stays on the roster up to and including their last day, and never after it.
"""

from __future__ import annotations

import calendar
import datetime
from collections import Counter
from dataclasses import dataclass
from typing import Any

from django.db.models import Exists, OuterRef, Q, QuerySet

from eduflow.academics.models import Section
from eduflow.people.models import Enrollment, Student

from .models import AttendanceRecord, AttendanceSession, AttendanceStatus


def _covering(day: datetime.date) -> Q:
    return Q(start_date__lte=day) & (Q(end_date__isnull=True) | Q(end_date__gte=day))


def roster(section: Section, day: datetime.date) -> QuerySet[Enrollment]:
    """The enrollments that place a student in ``section`` on ``day`` (see the module docstring)."""
    superseded = Enrollment.objects.filter(
        _covering(day), student_id=OuterRef("student_id"), start_date__gt=OuterRef("start_date")
    )
    return (
        Enrollment.objects.filter(_covering(day), section=section)
        .exclude(Exists(superseded))
        .select_related("student")
        .order_by("roll_number", "student__first_name", "student__last_name", "student_id")
    )


def counts(statuses: list[str]) -> dict[str, int]:
    tally = Counter(statuses)
    return {status: tally.get(status, 0) for status in AttendanceStatus.values}


def session_for(section: Section, day: datetime.date) -> AttendanceSession | None:
    return (
        AttendanceSession.objects.filter(section=section, date=day).select_related("taken_by__user").first()
    )


# ------------------------------------------------------------------------------------------------ month view
NOT_MARKED, UPCOMING = "not_marked", "upcoming"


@dataclass(frozen=True)
class Day:
    date: datetime.date
    status: str | None  # an attendance status, "not_marked", "upcoming", or None when not enrolled
    section_id: Any


def month_days(month: str) -> tuple[datetime.date, datetime.date]:
    year, number = (int(part) for part in month.split("-"))
    return datetime.date(year, number, 1), datetime.date(year, number, calendar.monthrange(year, number)[1])


def student_month(
    student: Student, first: datetime.date, last: datetime.date, today: datetime.date
) -> list[Day]:
    """One entry per day of the range: the student's status in the section they belonged to that day."""
    section_on: dict[datetime.date, Any] = {}
    enrollments = (
        Enrollment.objects.filter(student=student, start_date__lte=last)
        .filter(Q(end_date__isnull=True) | Q(end_date__gte=first))
        .order_by("start_date")
        .values_list("section_id", "start_date", "end_date")
    )
    for enrolled_in, start, end in enrollments:  # later starts overwrite: the transfer-day rule
        day = max(start, first)
        while day <= min(end or last, last):
            section_on[day] = enrolled_in
            day += datetime.timedelta(days=1)
    marked = {
        (record.date, record.section_id): record.status
        for record in AttendanceRecord.objects.filter(student=student, date__gte=first, date__lte=last).only(
            "date", "section_id", "status"
        )
    }
    days = []
    day = first
    while day <= last:
        section_id = section_on.get(day)
        if section_id is not None and (day, section_id) in marked:
            status: str | None = marked[(day, section_id)]
        elif day > today:
            status = UPCOMING if section_id is not None else None
        else:
            status = NOT_MARKED if section_id is not None else None
        days.append(Day(day, status, section_id))
        day += datetime.timedelta(days=1)
    return days


def month_summary(days: list[Day]) -> dict[str, int]:
    statuses = [d.status for d in days if d.status is not None]
    summary = counts([s for s in statuses if s in AttendanceStatus.values])
    summary[NOT_MARKED] = statuses.count(NOT_MARKED)
    summary["marked_days"] = sum(summary[s] for s in AttendanceStatus.values)
    return summary
