"""Read-only signals the rules, the risk engine, the scorecards and Ask EduFlow share. Everything is read from
the modules' own tables inside the current school's context; nothing here writes."""

from __future__ import annotations

import datetime
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from eduflow.attendance.models import AttendanceRecord, AttendanceSession
from eduflow.people.models import Enrollment, StaffProfile, Student, TeacherAssignment
from eduflow.tenancy import clock
from eduflow.timetable.models import TimetableSlot

from .models import MonitoringSettings

ABSENT = "absent"


def settings_for(school: Any) -> MonitoringSettings:
    return MonitoringSettings.objects.filter(school_id=school.pk).first() or MonitoringSettings(
        school_id=school.pk
    )


def rate(statuses: list[str]) -> float:
    """Share of days not absent (prototype ``attendanceRate``: late and half days count as present)."""
    return sum(1 for s in statuses if s != ABSENT) / len(statuses) if statuses else 1.0


def attendance_series(
    school: Any, window: int, students: Iterable[Any] | None = None
) -> dict[Any, list[str]]:
    """Per student: statuses of their last ``window`` registers, oldest first (a register = a school day)."""
    rows = AttendanceRecord.objects.filter(school_id=school.pk)
    if students is not None:
        rows = rows.filter(student_id__in=[getattr(s, "pk", s) for s in students])
    series: dict[Any, list[tuple[datetime.date, str]]] = defaultdict(list)
    for student_id, day, status in rows.order_by("date").values_list("student_id", "date", "status"):
        series[student_id].append((day, status))
    return {sid: [s for _, s in days[-window:]] for sid, days in series.items()}


def absent_streak(statuses: list[str]) -> int:
    n = 0
    for status in reversed(statuses):
        if status != ABSENT:
            break
        n += 1
    return n


@dataclass(frozen=True)
class Placement:
    student: Student
    section_id: Any
    section: str
    class_teacher_id: Any  # staff profile


def placements(school: Any) -> dict[Any, Placement]:
    """Active students with their current section and its class teacher."""
    class_teachers = dict(
        TeacherAssignment.objects.filter(
            school_id=school.pk, status="active", is_class_teacher=True
        ).values_list("section_id", "staff_id")
    )
    out: dict[Any, Placement] = {}
    for e in Enrollment.objects.filter(school_id=school.pk, status="active").select_related(
        "student", "section"
    ):
        out[e.student_id] = Placement(
            e.student, e.section_id, e.section.code, class_teachers.get(e.section_id)
        )
    return out


def teachers_of_section(school: Any) -> dict[Any, set[Any]]:
    out: dict[Any, set[Any]] = defaultdict(set)
    for section_id, staff_id in TeacherAssignment.objects.filter(
        school_id=school.pk, status="active"
    ).values_list("section_id", "staff_id"):
        out[section_id].add(staff_id)
    return out


def sections_due_today(school: Any, day: datetime.date) -> set[Any]:
    """Sections with a published timetable period on this weekday: the days a register is expected."""
    return set(
        TimetableSlot.objects.filter(
            school_id=school.pk,
            timetable__is_live=True,
            weekday=day.isoweekday(),
            effective_from__lte=day,
            effective_to__gte=day,
        ).values_list("section_id", flat=True)
    )


def registers_missing(school: Any, day: datetime.date) -> set[Any]:
    taken = set(
        AttendanceSession.objects.filter(school_id=school.pk, date=day).values_list("section_id", flat=True)
    )
    return sections_due_today(school, day) - taken


def staff_names(school: Any) -> dict[Any, str]:
    return {
        s.pk: s.membership.user.full_name
        for s in StaffProfile.objects.filter(school_id=school.pk).select_related("membership__user")
    }


def today(school: Any) -> datetime.date:
    return clock.today(school)
