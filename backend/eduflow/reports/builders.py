"""Report builders. Each takes the caller and validated parameters and returns ``(columns, rows)`` read
through the caller's data scopes; nothing here bypasses a permission the module's own endpoints enforce."""

from __future__ import annotations

import datetime
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

from django.db.models import Count

from eduflow.admissions.models import Application
from eduflow.assessment import policies as assessment_policies
from eduflow.assessment import services as assessment
from eduflow.assessment.models import Exam
from eduflow.attendance.models import AttendanceRecord
from eduflow.authz.catalog import DataScope
from eduflow.authz.grants import Actor
from eduflow.fees import services as fees
from eduflow.hr.models import StaffAttendance
from eduflow.people import policies as people_policies
from eduflow.people.models import StaffProfile, Student
from eduflow.tenancy import clock


@dataclass
class Table:
    columns: list[str]
    rows: list[list[Any]]


def _students(actor: Actor, permission: str, section_id: Any = None) -> Any:
    qs = people_policies.students.queryset(actor, permission, Student.objects.filter(status="active"))
    qs = people_policies.students.queryset(actor, "student.read", qs)
    if section_id:
        qs = qs.filter(enrollments__section_id=section_id, enrollments__status="active")
    return qs


def _sections(students: Any) -> dict[Any, str]:
    return dict(students.filter(enrollments__status="active").values_list("pk", "enrollments__section__code"))


def attendance(actor: Actor, *, since: datetime.date, until: datetime.date, section_id: Any = None) -> Table:
    students = _students(actor, "attendance.read", section_id)
    sections = _sections(students)
    counts: dict[Any, Counter[str]] = defaultdict(Counter)
    for sid, status in AttendanceRecord.objects.filter(
        student__in=students, date__gte=since, date__lte=until
    ).values_list("student_id", "status"):
        counts[sid][status] += 1
    rows = []
    for st in students.order_by("first_name", "last_name"):
        c = counts.get(st.pk, Counter())
        days = sum(c.values())
        rate = round(100 * (days - c["absent"]) / days, 1) if days else None
        rows.append(
            [
                st.admission_number,
                st.full_name,
                sections.get(st.pk, ""),
                days,
                c["present"],
                c["late"],
                c["half_day"],
                c["excused"],
                c["absent"],
                rate,
            ]
        )
    return Table(
        [
            "Admission no.",
            "Student",
            "Class",
            "Days",
            "Present",
            "Late",
            "Half day",
            "Excused",
            "Absent",
            "Attendance %",
        ],
        rows,
    )


def fee_dues(actor: Actor, *, section_id: Any = None) -> Table:
    students = _students(actor, "fee.read", section_id)
    sections = _sections(students)
    today = clock.today(actor.school)
    rows = []
    for st in students.order_by("first_name", "last_name"):
        s = fees.statement(st, today)
        rows.append(
            [
                st.admission_number,
                st.full_name,
                sections.get(st.pk, ""),
                s.total,
                s.net_paid,
                s.balance,
                s.overdue,
            ]
        )
    return Table(["Admission no.", "Student", "Class", "Total due", "Paid (net)", "Balance", "Overdue"], rows)


def exam_results(actor: Actor, *, exam_id: Any, section_id: Any = None) -> Table:
    exam = Exam.objects.filter(school_id=actor.school.pk, pk=exam_id).first()
    if exam is None:
        return Table(["Student"], [])
    students = _students(actor, "assessment.read", section_id)
    marks = assessment_policies.marks.queryset(actor, "assessment.read").filter(sheet__exam=exam)
    subjects = sorted(set(marks.values_list("sheet__subject__name", flat=True)))
    rows = []
    for st in students.filter(marks__in=marks).distinct().order_by("first_name"):
        card = assessment.report_card(st, exam, marks)
        by_subject = {
            line["subject"].name: ("AB" if line["absent"] else line["marks"]) for line in card["lines"]
        }
        rows.append(
            [
                st.admission_number,
                st.full_name,
                *[by_subject.get(s, "") for s in subjects],
                card["total"],
                card["max_total"],
                card["percent"],
                card["grade"] or "",
            ]
        )
    return Table(["Admission no.", "Student", *subjects, "Total", "Out of", "%", "Grade"], rows)


def staff_attendance(actor: Actor, *, since: datetime.date, until: datetime.date) -> Table:
    if DataScope.SCHOOL not in actor.scopes("staff_attendance.read"):
        staff = StaffProfile.objects.filter(membership=actor.membership)
    else:
        staff = StaffProfile.objects.filter(school_id=actor.school.pk, status="active")
    counts: dict[Any, Counter[str]] = defaultdict(Counter)
    for sid, status in StaffAttendance.objects.filter(
        staff__in=staff, date__gte=since, date__lte=until
    ).values_list("staff_id", "status"):
        counts[sid][status] += 1
    rows = [
        [
            s.employee_id,
            s.membership.user.full_name,
            s.designation,
            counts[s.pk]["present"],
            counts[s.pk]["late"],
            counts[s.pk]["leave"],
            counts[s.pk]["absent"],
        ]
        for s in staff.select_related("membership__user").order_by("employee_id")
    ]
    return Table(["Employee ID", "Name", "Designation", "Present", "Late", "Leave", "Absent"], rows)


def admissions_funnel(actor: Actor, *, since: datetime.date, until: datetime.date) -> Table:
    apps = Application.objects.filter(
        school_id=actor.school.pk, created_at__date__gte=since, created_at__date__lte=until
    )
    by = apps.values("source", "stage").annotate(n=Count("id"))
    table: dict[str, Counter[str]] = defaultdict(Counter)
    for row in by:
        table[row["source"]][row["stage"]] += row["n"]
    stages = ["enquiry", "visit", "assessment", "offer", "enrolled", "dropped"]
    rows = [[source, *[c[s] for s in stages], sum(c.values())] for source, c in sorted(table.items())]
    totals: Counter[str] = Counter()
    for c in table.values():
        totals.update(c)
    rows.append(["all", *[totals[s] for s in stages], sum(totals.values())])
    return Table(["Source", *stages, "Total"], rows)
