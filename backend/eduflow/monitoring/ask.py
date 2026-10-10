"""Ask EduFlow: questions in plain language, answered from data the caller is authorised to see.

Deterministic intent matching (prototype ``engine/ask.ts``); no language model is involved and nothing is
generated. Every intent reads through the same data scopes as the API: a teacher asking "who is absent
today" sees their students, a principal the school. An intent the caller has no permission for answers
that they cannot see that information, without revealing whether any exists.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from django.db.models import QuerySet

from eduflow.approvals.registry import pending_for
from eduflow.assessment import services as assessment
from eduflow.attendance.models import AttendanceRecord
from eduflow.authz.catalog import DataScope
from eduflow.authz.grants import Actor
from eduflow.communication import services as communication
from eduflow.fees import services as fees
from eduflow.hr.models import StaffAttendance
from eduflow.people import policies as people_policies
from eduflow.people.models import StaffProfile, Student
from eduflow.tenancy import clock
from eduflow.transport import policies as transport_policies
from eduflow.transport.models import Trip

from . import insights, signals

SUGGESTIONS = [
    "Show students below 75% attendance",
    "Who is absent today?",
    "Show unpaid fees above 20000",
    "Which teachers have pending marks?",
    "Students whose marks are falling in 5A",
    "Show pending approvals",
    "Teacher scorecards",
    "Parents waiting for a reply",
    "Top performers in 5A",
    "Where are the buses?",
    "Students at risk",
]


@dataclass
class Answer:
    intent: str
    summary: str
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    follow_ups: list[str] = field(default_factory=list)


def _row(row_id: Any, cells: list[Any], tone: str | None = None) -> dict[str, Any]:
    return {"id": str(row_id), "cells": [str(c) for c in cells], "tone": tone}


def _number(q: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*(k|l|lakh|%)?", q.replace(",", ""), re.IGNORECASE)
    if not m:
        return None
    n = float(m.group(1))
    unit = (m.group(2) or "").lower()
    return n * 1000 if unit == "k" else n * 100000 if unit in ("l", "lakh") else n


def _n(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _denied(intent: str) -> Answer:
    return Answer(intent, "You do not have access to that information.")


def _students(actor: Actor, *permissions: str) -> QuerySet[Student] | None:
    """Active students visible under ``student.read`` and every given permission (or None: no access)."""
    qs: QuerySet[Student] = Student.objects.filter(school_id=actor.school.pk, status="active")
    for permission in ("student.read", *permissions):
        if not actor.scopes(permission):
            return None
        qs = people_policies.students.queryset(actor, permission, base=qs)
    return qs


def _school_wide(actor: Actor, permission: str) -> bool:
    return DataScope.SCHOOL in actor.scopes(permission)


def _in_class(q: str, students: QuerySet[Student]) -> tuple[QuerySet[Student], str]:
    m = re.search(r"\b(?:in|of|class)\s+([a-z0-9][a-z0-9-]{0,15})\b", q)
    if not m:
        return students, ""
    code = m.group(1)
    filtered = students.filter(
        enrollments__status="active", enrollments__section__code__iexact=code
    ).distinct()
    return filtered, f" in {code.upper()}"


def _section_of(students: QuerySet[Student]) -> dict[Any, str]:
    rows = students.filter(enrollments__status="active").values_list("pk", "enrollments__section__code")
    return dict(rows)


# ------------------------------------------------------------------------------------------------ intents
def attendance_below(actor: Actor, q: str) -> Answer:
    students = _students(actor, "attendance.read")
    if students is None:
        return _denied("attendance_below")
    students, where = _in_class(q, students)
    n = _number(q)
    threshold = n / 100 if n and n <= 100 else float(signals.settings_for(actor.school).attendance_risk)
    conf = signals.settings_for(actor.school)
    series = signals.attendance_series(actor.school, conf.attendance_window_days, students)
    sections = _section_of(students)
    rows = sorted(
        (
            (sid, signals.rate(s))
            for sid, s in series.items()
            if sid in sections and signals.rate(s) < threshold
        ),
        key=lambda x: x[1],
    )
    full = {s.pk: s.full_name for s in students.filter(pk__in=[r[0] for r in rows])}
    return Answer(
        "attendance_below",
        f"{_n(len(rows), 'student')}{where} below {round(threshold * 100)}% attendance.",
        ["Student", "Class", "Attendance"],
        [
            _row(sid, [full[sid], sections[sid], f"{round(r * 100)}%"], "bad" if r < 0.7 else "warn")
            for sid, r in rows
        ],
        ["Who is absent today?", "Students at risk"],
    )


def absent_today(actor: Actor, q: str) -> Answer:
    students = _students(actor, "attendance.read")
    if students is None:
        return _denied("absent_today")
    students, where = _in_class(q, students)
    today = clock.today(actor.school)
    absent = AttendanceRecord.objects.filter(
        student__in=students, date=today, status="absent"
    ).select_related("student", "section")
    missing = signals.registers_missing(actor.school, today)
    open_codes = ""
    if missing and _school_wide(actor, "attendance.read"):
        from eduflow.academics.models import Section

        open_codes = ", ".join(
            Section.objects.filter(pk__in=missing).order_by("code").values_list("code", flat=True)
        )
    summary = f"{absent.count()} student{'s' if absent.count() != 1 else ''}{where} marked absent so far."
    if open_codes:
        summary += f" Registers still open: {open_codes}."
    return Answer(
        "absent_today",
        summary,
        ["Student", "Class"],
        [_row(r.student_id, [r.student.full_name, r.section.code], "warn") for r in absent],
        ["Show students below 75% attendance"],
    )


def staff_today(actor: Actor, q: str) -> Answer:
    if not _school_wide(actor, "staff_attendance.read"):
        return _denied("staff_today")
    today = clock.today(actor.school)
    days = {d.staff_id: d for d in StaffAttendance.objects.filter(school_id=actor.school.pk, date=today)}
    rows = []
    for staff in StaffProfile.objects.filter(school_id=actor.school.pk, status="active").select_related(
        "membership__user"
    ):
        day = days.get(staff.pk)
        status = day.status if day else "no check-in"
        if status != "present":
            rows.append(
                _row(
                    staff.pk,
                    [
                        staff.membership.user.full_name,
                        staff.designation,
                        status,
                        day.check_in if day and day.check_in else "-",
                    ],
                    "bad" if status == "no check-in" else "warn",
                )
            )
    return Answer(
        "staff_today",
        f"{len(rows)} staff are not fully in today.",
        ["Name", "Role", "Status", "Check-in"],
        rows,
        ["Teacher scorecards"],
    )


def fees_overdue(actor: Actor, q: str) -> Answer:
    students = _students(actor, "fee.read")
    if students is None:
        return _denied("fees_overdue")
    students, where = _in_class(q, students)
    minimum = Decimal(str(_number(q) or 0))
    rows = [r for r in fees.defaulters(students, clock.today(actor.school)) if r.overdue >= minimum]
    total = sum((r.overdue for r in rows), Decimal(0))
    sections = _section_of(students)
    return Answer(
        "fees_overdue",
        f"{len(rows)} famil{'ies' if len(rows) != 1 else 'y'}{where} with overdue fees"
        + (f" above {minimum:.0f}" if minimum else "")
        + f": {total:.2f} in total.",
        ["Student", "Class", "Overdue", "Balance"],
        [
            _row(
                r.student.pk,
                [r.student.full_name, sections.get(r.student.pk, ""), f"{r.overdue:.2f}", f"{r.balance:.2f}"],
                "bad",
            )
            for r in rows
        ],
        ["Students at risk"],
    )


def pending_marks(actor: Actor, q: str) -> Answer:
    if not (_school_wide(actor, "exam.manage") or _school_wide(actor, "monitoring.read")):
        return _denied("pending_marks")
    sheets = list(assessment.overdue_sheets(actor.school, clock.today(actor.school)))
    teachers = {s.teacher_id for s in sheets}
    return Answer(
        "pending_marks",
        f"{_n(len(teachers), 'teacher')} have {_n(len(sheets), 'mark sheet')} past the deadline.",
        ["Teacher", "Class", "Subject", "Exam", "Deadline"],
        [
            _row(
                s.pk,
                [
                    s.teacher.membership.user.full_name if s.teacher else "-",
                    s.section.code,
                    s.subject.name,
                    s.exam.name,
                    s.exam.marks_deadline,
                ],
                "bad",
            )
            for s in sheets
        ],
        ["Teacher scorecards"],
    )


def falling_marks(actor: Actor, q: str) -> Answer:
    students = _students(actor, "assessment.read")
    if students is None:
        return _denied("falling_marks")
    students, where = _in_class(q, students)
    visible = set(students.values_list("pk", flat=True))
    sections = _section_of(students)
    names = {s.pk: s.full_name for s in students}
    rows = []
    for sid, series in assessment.exam_percentages(actor.school).items():
        t = assessment.trend(series)
        if sid in visible and t is not None and t[2] <= -10:  # prototype: "dropped 10+ points"
            rows.append(
                (t[2], _row(sid, [names[sid], sections.get(sid, ""), round(t[0]), round(t[1])], "bad"))
            )
    rows.sort(key=lambda x: x[0])
    return Answer(
        "falling_marks",
        f"{len(rows)} student{'s' if len(rows) != 1 else ''}{where} dropped 10+ points in the latest exam.",
        ["Student", "Class", "Before", "Now"],
        [r for _, r in rows],
        ["Students at risk"],
    )


def top_performers(actor: Actor, q: str) -> Answer:
    students = _students(actor, "assessment.read")
    if students is None:
        return _denied("top_performers")
    students, where = _in_class(q, students)
    visible = {s.pk: s for s in students}
    sections = _section_of(students)
    averages = [
        (sum(p for _, p in series) / len(series), sid)
        for sid, series in assessment.exam_percentages(actor.school).items()
        if sid in visible and series
    ]
    top = sorted(averages, reverse=True)[:10]
    return Answer(
        "top_performers",
        f"Top {len(top)} students{where} by average across exams.",
        ["Student", "Class", "Average"],
        [
            _row(sid, [visible[sid].full_name, sections.get(sid, ""), f"{round(avg)}%"], "good")
            for avg, sid in top
        ],
        ["Students whose marks are falling"],
    )


def approvals(actor: Actor, q: str) -> Answer:
    if not actor.scopes("approval.read"):
        return _denied("approvals")
    items = pending_for(actor)
    return Answer(
        "approvals",
        f"{len(items)} approval{'s' if len(items) != 1 else ''} waiting for your decision.",
        ["Request", "From", "Type", "Raised"],
        [_row(i.id, [i.title, i.requested_by, i.kind, i.requested_at.date()]) for i in items],
        ["Teacher scorecards"],
    )


def scorecards(actor: Actor, q: str) -> Answer:
    if not _school_wide(actor, "monitoring.read"):
        return _denied("scorecards")
    teachers = StaffProfile.objects.filter(
        school_id=actor.school.pk, status="active", staff_type="teaching"
    ).select_related("membership__user")
    cards = sorted(insights.scorecards(actor.school, teachers), key=lambda c: c.score)
    tone = {"Needs support": "bad", "Good": "warn", "Excellent": "good"}
    return Answer(
        "scorecards",
        "Teacher scorecards, lowest first. "
        f"{sum(1 for c in cards if c.grade == 'Needs support')} need support.",
        ["Teacher", "Score", "Marks on time", "Reviewed", "Reply (h)"],
        [
            _row(
                c.staff.pk,
                [
                    c.staff.membership.user.full_name,
                    c.score,
                    f"{round(c.marks_on_time * 100)}%",
                    f"{round(c.review_rate * 100)}%",
                    "-" if c.reply_hours is None else round(c.reply_hours, 1),
                ],
                tone[c.grade],
            )
            for c in cards
        ],
        ["Which teachers have pending marks?", "Parents waiting for a reply"],
    )


def parent_replies(actor: Actor, q: str) -> Answer:
    threads = communication.unanswered(actor.school, signals.settings_for(actor.school).reply_hours)
    if not _school_wide(actor, "monitoring.read"):
        if not actor.scopes("message.read"):
            return _denied("parent_replies")
        threads = threads.filter(staff__membership=actor.membership)
    threads = threads.select_related("guardian", "student", "staff__membership__user")
    return Answer(
        "parent_replies",
        f"{_n(threads.count(), 'parent message')} waiting more than "
        f"{signals.settings_for(actor.school).reply_hours} hours.",
        ["Parent", "Student", "Teacher", "Waiting since"],
        [
            _row(
                t.pk,
                [
                    t.guardian.full_name if t.guardian else "-",
                    t.student.full_name,
                    t.staff.membership.user.full_name,
                    t.awaiting_reply_since,
                ],
                "warn",
            )
            for t in threads
        ],
        ["Teacher scorecards"],
    )


def buses(actor: Actor, q: str) -> Answer:
    if not actor.scopes("transport.read"):
        return _denied("buses")
    trips = transport_policies.trips.queryset(
        actor, "transport.read", Trip.objects.select_related("route__vehicle")
    ).filter(date=clock.today(actor.school))
    arrived = trips.filter(status="arrived").count()
    return Answer(
        "buses",
        f"{arrived} of {trips.count()} trips today have arrived.",
        ["Route", "Vehicle", "Status", "Delay"],
        [
            _row(
                t.pk,
                [
                    t.route.name,
                    t.route.vehicle.label if t.route.vehicle else "-",
                    t.get_status_display(),
                    f"+{t.delay_minutes} min" if t.delay_minutes else "-",
                ],
                "bad" if t.delay_minutes >= 10 else "good",
            )
            for t in trips
        ],
        ["Who is absent today?"],
    )


def at_risk(actor: Actor, q: str) -> Answer:
    students = _students(actor, "monitoring.read")
    if students is None:
        return _denied("at_risk")
    students, where = _in_class(q, students)
    sections = _section_of(students)
    rows = [r for r in insights.student_risk(actor.school, students) if r.level == "at_risk"]
    return Answer(
        "at_risk",
        f"{_n(len(rows), 'student')}{where} at risk across attendance, marks, fees and behaviour.",
        ["Student", "Class", "Score", "Why"],
        [
            _row(
                r.student.pk,
                [
                    r.student.full_name,
                    sections.get(r.student.pk, ""),
                    r.score,
                    " · ".join(f["label"] for f in r.factors),
                ],
                "bad",
            )
            for r in rows
        ],
        ["Show students below 75% attendance", "Students whose marks are falling"],
    )


INTENTS: list[tuple[Callable[[str], bool], Callable[[Actor, str], Answer]]] = [
    (lambda q: "attendance" in q and bool(re.search(r"below|under|less|<|risk", q)), attendance_below),
    (
        lambda q: (
            bool(re.search(r"absent|not in|missing", q))
            and bool(re.search(r"today|now", q))
            and not re.search(r"staff|teacher", q)
        ),
        absent_today,
    ),
    (
        lambda q: bool(re.search(r"staff|teacher", q)) and bool(re.search(r"absent|late|leave|not in", q)),
        staff_today,
    ),
    (lambda q: bool(re.search(r"fee|unpaid|owe|defaulter|overdue|outstanding", q)), fees_overdue),
    (
        lambda q: bool(re.search(r"marks?", q)) and bool(re.search(r"pending|not submitted|overdue|late", q)),
        pending_marks,
    ),
    (lambda q: bool(re.search(r"falling|dropp|declin|worse", q)), falling_marks),
    (lambda q: bool(re.search(r"\btop\b|best|highest", q)), top_performers),
    (lambda q: bool(re.search(r"approval|pending request|leave request", q)), approvals),
    (lambda q: bool(re.search(r"scorecard|performance|teacher.*(rank|score)|which teacher", q)), scorecards),
    (lambda q: bool(re.search(r"parent|message|reply|waiting", q)), parent_replies),
    (lambda q: bool(re.search(r"\bbus|transport|route|trip", q)), buses),
    (lambda q: bool(re.search(r"risk|attention|worry|concern|struggl", q)), at_risk),
]


def ask(actor: Actor, question: str) -> Answer:
    q = question.strip().lower()
    for matches, answer in INTENTS:
        if matches(q):
            return answer(actor, q)
    return Answer(
        "unknown",
        "I did not understand that question. Try one of the suggestions.",
        follow_ups=SUGGESTIONS[:5],
    )
