"""The rule set (prototype ``computeAlerts`` and README "First alert set"), over the school's real data.

Each rule returns a :class:`Finding` (or ``None`` when nothing is wrong). Thresholds come from the school's
``MonitoringSettings``. Owners and escalations are the README's. ``escalate_after_hours`` is set only where
the README gives a time ("after 7 days", "after 48h", "live"); other rules are visible to the principal
from the start and are not escalated automatically.

Not implemented, with the reason (docs/product/traceability.md): "Class without a teacher" needs
substitutions, which EduFlow does not record yet.
"""

from __future__ import annotations

import datetime
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from django.utils import timezone

from eduflow.approvals.registry import all_pending
from eduflow.assessment import services as assessment
from eduflow.communication import services as communication
from eduflow.communication.models import Complaint, ComplaintStatus
from eduflow.conduct import services as conduct
from eduflow.fees import services as fees
from eduflow.homework.models import HomeworkStatus, Submission, SubmissionStatus
from eduflow.hr import services as hr
from eduflow.people.models import Student
from eduflow.tenancy import clock
from eduflow.transport import services as transport

from . import signals
from .models import Domain, MonitoringSettings, Severity


@dataclass
class Finding:
    headline: str
    why: str
    items: list[dict[str, Any]]
    severity: str | None = None  # overrides the rule's default (complaints)
    staff_ids: set[Any] = field(default_factory=set)
    section_ids: set[Any] = field(default_factory=set)


@dataclass(frozen=True)
class Rule:
    key: str
    title: str
    domain: str
    severity: str
    owner: str
    escalates_to: str
    escalate_after_hours: int | None
    check: Callable[[Any, MonitoringSettings, datetime.datetime], Finding | None]


RULES: dict[str, Rule] = {}


def rule(
    key: str,
    title: str,
    domain: str,
    severity: str,
    owner: str,
    escalates_to: str,
    escalate_after_hours: int | None = None,
) -> Callable[[Callable[..., Finding | None]], Callable[..., Finding | None]]:
    def register(fn: Callable[..., Finding | None]) -> Callable[..., Finding | None]:
        RULES[key] = Rule(key, title, domain, severity, owner, escalates_to, escalate_after_hours, fn)
        return fn

    return register


def _pct(value: float) -> str:
    return f"{round(value * 100)}%"


def _student_item(p: signals.Placement, value: str, sub: str = "") -> dict[str, Any]:
    return {
        "id": str(p.student.pk),
        "label": p.student.full_name,
        "sub": sub or p.section,
        "value": value,
        "student_id": str(p.student.pk),
        "section_id": str(p.section_id),
        "staff_id": str(p.class_teacher_id) if p.class_teacher_id else None,
    }


def _finding(headline: str, why: str, items: list[dict[str, Any]], **extra: Any) -> Finding | None:
    if not items:
        return None
    staff = {i["staff_id"] for i in items if i.get("staff_id")}
    sections = {i["section_id"] for i in items if i.get("section_id")}
    return Finding(headline, why, items, staff_ids=staff, section_ids=sections, **extra)


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


# ------------------------------------------------------------------------------------------------ students
@rule(
    "attendance_risk",
    "Attendance below threshold",
    Domain.STUDENTS,
    Severity.CRITICAL,
    "Class teachers",
    "Principal after 7 days",
    7 * 24,
)
def attendance_risk(school: Any, conf: MonitoringSettings, now: datetime.datetime) -> Finding | None:
    places = signals.placements(school)
    series = signals.attendance_series(school, conf.attendance_window_days, places)
    rows = []
    for sid, statuses in series.items():
        r = signals.rate(statuses)
        if sid in places and r < float(conf.attendance_risk):
            rows.append((r, _student_item(places[sid], _pct(r))))
    items = [i for _, i in sorted(rows, key=lambda x: x[0])]
    return _finding(
        f"{len(items)} {_plural(len(items), 'student is', 'students are')} at attendance risk",
        f"Attendance across the last {conf.attendance_window_days} school days is below "
        f"{_pct(float(conf.attendance_risk))}, the eligibility line for exams.",
        items,
    )


@rule(
    "attendance_slipping",
    "Attendance slipping",
    Domain.STUDENTS,
    Severity.HIGH,
    "Class teachers",
    "Principal if a parent call is not logged in 3 days",
    72,
)
def attendance_slipping(school: Any, conf: MonitoringSettings, now: datetime.datetime) -> Finding | None:
    places = signals.placements(school)
    series = signals.attendance_series(school, conf.attendance_window_days, places)
    items = []
    for sid, statuses in series.items():
        recent, prior = statuses[-conf.slip_recent_days :], statuses[: -conf.slip_recent_days]
        if sid not in places or not prior or len(recent) < conf.slip_recent_days:
            continue
        drop = signals.rate(prior) - signals.rate(recent)
        if drop >= float(conf.slip_drop) and signals.rate(statuses) >= float(conf.attendance_risk):
            items.append(
                _student_item(
                    places[sid],
                    _pct(signals.rate(recent)),
                    f"{places[sid].section} · was {_pct(signals.rate(prior))}",
                )
            )
    return _finding(
        f"{len(items)} {_plural(len(items), 'student has', 'students have')} started missing school",
        f"Still above {_pct(float(conf.attendance_risk))} overall, but attendance in the last "
        f"{conf.slip_recent_days} school days dropped by {_pct(float(conf.slip_drop))} or more.",
        items,
    )


@rule(
    "falling_marks",
    "Falling marks",
    Domain.STUDENTS,
    Severity.HIGH,
    "Subject teachers",
    "Academic coordinator, then Principal",
)
def falling_marks(school: Any, conf: MonitoringSettings, now: datetime.datetime) -> Finding | None:
    places = signals.placements(school)
    rows = []
    for sid, series in assessment.exam_percentages(school).items():
        t = assessment.trend(series)
        if t is None or sid not in places:
            continue
        before, current, delta = t
        if delta <= -conf.marks_drop:
            rows.append(
                (
                    delta,
                    _student_item(
                        places[sid],
                        f"-{round(-delta)}",
                        f"{places[sid].section} · {round(before)} -> {round(current)}",
                    ),
                )
            )
    items = [i for _, i in sorted(rows, key=lambda x: x[0])]
    return _finding(
        f"{len(items)} {_plural(len(items), 'student', 'students')}' marks fell sharply in the latest exam",
        f"The average dropped by {conf.marks_drop}+ points against their earlier exams.",
        items,
    )


@rule(
    "absent_streak",
    "Absent days in a row",
    Domain.STUDENTS,
    Severity.HIGH,
    "Class teachers",
    "Principal by 11:00",
    0,
)
def absent_streak(school: Any, conf: MonitoringSettings, now: datetime.datetime) -> Finding | None:
    places = signals.placements(school)
    today = clock.today(school)
    series = signals.attendance_series(school, conf.absent_streak_days, places)
    from eduflow.attendance.models import AttendanceRecord

    absent_today = set(
        AttendanceRecord.objects.filter(school_id=school.pk, date=today, status=signals.ABSENT).values_list(
            "student_id", flat=True
        )
    )
    items = [
        _student_item(places[sid], f"{conf.absent_streak_days} days")
        for sid, statuses in series.items()
        if sid in places
        and sid in absent_today
        and signals.absent_streak(statuses) >= conf.absent_streak_days
    ]
    return _finding(
        f"{len(items)} {_plural(len(items), 'student', 'students')} absent for "
        f"{conf.absent_streak_days} school days running",
        "Consecutive absences: a call home today is the school's duty of care.",
        items,
    )


@rule(
    "behaviour",
    "Repeated behaviour notes",
    Domain.STUDENTS,
    Severity.WATCH,
    "Class teachers and counsellor",
    "Principal",
)
def behaviour(school: Any, conf: MonitoringSettings, now: datetime.datetime) -> Finding | None:
    term = conduct.current_term(school)
    if term is None:
        return None  # "this term": without a current term the rule has no period to count in
    places = signals.placements(school)
    counts = conduct.incident_counts(school, term.start_date, term.end_date)
    items = [
        _student_item(places[sid], f"{n} incidents")
        for sid, n in counts.items()
        if sid in places and n >= conf.behaviour_incidents
    ]
    return _finding(
        f"{len(items)} {_plural(len(items), 'student has', 'students have')} repeated behaviour notes",
        f"{conf.behaviour_incidents}+ incidents recorded this term ({term.name}).",
        items,
    )


# ------------------------------------------------------------------------------------------------ staff
@rule(
    "register_missing",
    "Register not marked",
    Domain.STAFF,
    Severity.CRITICAL,
    "Class teachers",
    "Principal (live)",
    0,
)
def register_missing(school: Any, conf: MonitoringSettings, now: datetime.datetime) -> Finding | None:
    local = clock.local(now, school)
    if local.time() < conf.register_by:
        return None
    from eduflow.academics.models import Section

    missing = signals.registers_missing(school, local.date())
    teachers = signals.teachers_of_section(school)
    names = signals.staff_names(school)
    from eduflow.people.models import TeacherAssignment

    class_teacher = dict(
        TeacherAssignment.objects.filter(
            school_id=school.pk, status="active", is_class_teacher=True
        ).values_list("section_id", "staff_id")
    )
    items = []
    for section in Section.objects.filter(pk__in=missing).order_by("code"):
        owner = class_teacher.get(section.pk) or next(iter(teachers.get(section.pk, [])), None)
        items.append(
            {
                "id": str(section.pk),
                "label": f"Class {section.code}",
                "sub": names.get(owner, "No teacher assigned"),
                "value": "not marked",
                "section_id": str(section.pk),
                "staff_id": str(owner) if owner else None,
            }
        )
    return _finding(
        f"{len(items)} {_plural(len(items), 'classroom has', 'classrooms have')} not taken the register",
        f"Registers should be in by {conf.register_by:%H:%M}. "
        "Until then parents have not been told their child is safe.",
        items,
    )


def _staff_item(staff_id: Any, names: dict[Any, str], value: str, sub: str = "") -> dict[str, Any]:
    return {
        "id": str(staff_id),
        "label": names.get(staff_id, ""),
        "sub": sub,
        "value": value,
        "staff_id": str(staff_id),
    }


@rule("marks_overdue", "Marks overdue", Domain.STAFF, Severity.CRITICAL, "Subject teachers", "Principal")
def marks_overdue(school: Any, conf: MonitoringSettings, now: datetime.datetime) -> Finding | None:
    by_teacher: dict[Any, list[str]] = defaultdict(list)
    for sheet in assessment.overdue_sheets(school, clock.today(school)):
        by_teacher[sheet.teacher_id].append(f"{sheet.exam.name} {sheet.section.code} {sheet.subject.name}")
    names = signals.staff_names(school)
    items = [
        _staff_item(tid, names, f"{len(sheets)} sheet(s)", "; ".join(sheets))
        if tid
        else {
            "id": "unassigned",
            "label": "No teacher assigned",
            "sub": "; ".join(sheets),
            "value": f"{len(sheets)} sheet(s)",
        }
        for tid, sheets in by_teacher.items()
    ]
    return _finding(
        f"{len(items)} {_plural(len(items), 'teacher has', 'teachers have')} "
        "not submitted marks by the deadline",
        "The marks deadline has passed. Report cards for these classes are blocked.",
        items,
    )


@rule(
    "homework_unreviewed",
    "Homework not reviewed",
    Domain.STAFF,
    Severity.HIGH,
    "Subject teachers",
    "Principal after 5 days",
    5 * 24,
)
def homework_unreviewed(school: Any, conf: MonitoringSettings, now: datetime.datetime) -> Finding | None:
    today = clock.today(school)
    counts = Counter(
        Submission.objects.filter(
            school_id=school.pk,
            status__in=[SubmissionStatus.SUBMITTED, SubmissionStatus.LATE],
            homework__status=HomeworkStatus.PUBLISHED,
            homework__due_date__lt=today,
            homework__teacher__isnull=False,
        ).values_list("homework__teacher_id", flat=True)
    )
    names = signals.staff_names(school)
    flagged = [(t, n) for t, n in counts.most_common() if n >= conf.unreviewed_submissions]
    items = [_staff_item(t, names, f"{n} waiting") for t, n in flagged]
    return _finding(
        f"{len(items)} {_plural(len(items), 'teacher has', 'teachers have')} "
        f"{sum(n for _, n in flagged)} submissions unreviewed",
        "Work past its due date still has no feedback. Parents notice this first.",
        items,
    )


@rule("parent_waiting", "Parent waiting", Domain.STAFF, Severity.HIGH, "Teachers", "Principal after 48h", 24)
def parent_waiting(school: Any, conf: MonitoringSettings, now: datetime.datetime) -> Finding | None:
    threads = list(communication.unanswered(school, conf.reply_hours))
    counts = Counter(t.staff_id for t in threads)
    names = signals.staff_names(school)
    items = [_staff_item(t, names, f"{n} waiting") for t, n in counts.most_common()]
    return _finding(
        f"{len(threads)} parent {_plural(len(threads), 'message has', 'messages have')} "
        f"waited more than {conf.reply_hours} hours",
        "A parent who waits longer usually calls the office next. "
        "Reply time is part of each teacher's scorecard.",
        items,
    )


@rule("staff_absent", "Absent without leave", Domain.STAFF, Severity.HIGH, "HR / Principal", "Principal")
def staff_absent(school: Any, conf: MonitoringSettings, now: datetime.datetime) -> Finding | None:
    local = clock.local(now, school)
    if local.time() < conf.register_by or not signals.sections_due_today(school, local.date()):
        return None  # only on school days, once the morning has started
    names = signals.staff_names(school)
    items = [
        _staff_item(s.pk, names, "No check-in", s.designation)
        for s in hr.absent_without_leave(school, local.date())
    ]
    return _finding(
        f"{len(items)} staff {_plural(len(items), 'member is', 'members are')} absent without approved leave",
        "No check-in and no approved leave. Their periods need cover.",
        items,
    )


@rule(
    "staff_late",
    "Frequent late arrivals",
    Domain.STAFF,
    Severity.WATCH,
    "Principal",
    "School owner (monthly review)",
)
def staff_late(school: Any, conf: MonitoringSettings, now: datetime.datetime) -> Finding | None:
    today = clock.today(school)
    counts = hr.late_counts(school, today - datetime.timedelta(days=conf.late_window_days), today)
    names = signals.staff_names(school)
    items = [
        _staff_item(t, names, f"{n} late")
        for t, n in sorted(counts.items(), key=lambda x: -x[1])
        if n >= conf.late_arrivals
    ]
    return _finding(
        f"{len(items)} staff arrived late {conf.late_arrivals}+ times recently",
        f"Checked in after the school's start time on several days in the last {conf.late_window_days} days.",
        items,
    )


# ------------------------------------------------------------------------------------------------ operations
@rule(
    "bus_delayed",
    "Bus delayed",
    Domain.OPERATIONS,
    Severity.CRITICAL,
    "Transport manager",
    "Principal (live)",
    0,
)
def bus_delayed(school: Any, conf: MonitoringSettings, now: datetime.datetime) -> Finding | None:
    trips = [
        t
        for t in transport.delayed_trips(school, clock.today(school))
        if t.delay_minutes >= conf.bus_delay_minutes
    ]
    items = [
        {
            "id": str(t.pk),
            "label": (t.route.vehicle.label if t.route.vehicle and t.route.vehicle.label else t.route.name),
            "sub": t.route.name,
            "value": f"+{t.delay_minutes} min",
        }
        for t in trips
    ]
    return _finding(
        " · ".join(f"{i['label']} is {i['value'][1:]} late" for i in items)[:300],
        "Families of the riders were notified when the delay was reported.",
        items,
    )


@rule("approvals_waiting", "Approvals waiting", Domain.OPERATIONS, Severity.HIGH, "Principal", "-")
def approvals_waiting(school: Any, conf: MonitoringSettings, now: datetime.datetime) -> Finding | None:
    cutoff = now - datetime.timedelta(hours=conf.approval_hours)
    pending = [i for i in all_pending(school) if i.requested_at <= cutoff]
    items = [
        {
            "id": str(i.id),
            "label": i.title,
            "sub": i.requested_by,
            "value": f"{round((now - i.requested_at).total_seconds() / 3600)}h",
            "kind": i.kind,
        }
        for i in sorted(pending, key=lambda i: i.requested_at)
    ]
    return _finding(
        f"{len(items)} {_plural(len(items), 'approval has', 'approvals have')} "
        f"waited more than {conf.approval_hours} hours",
        "Each pending request is somebody blocked.",
        items,
    )


@rule(
    "negative_feedback",
    "Open parent complaints",
    Domain.OPERATIONS,
    Severity.WATCH,
    "Admin office",
    "Principal",
)
def negative_feedback(school: Any, conf: MonitoringSettings, now: datetime.datetime) -> Finding | None:
    rows = list(
        Complaint.objects.filter(school_id=school.pk, sentiment="negative")
        .exclude(status=ComplaintStatus.RESOLVED)
        .order_by("created_at")
    )
    if not rows:
        return None
    by_category = Counter(c.category for c in rows)
    top, top_n = by_category.most_common(1)[0]
    items = [
        {"id": str(c.pk), "label": c.get_category_display(), "sub": c.text[:120], "value": c.status}
        for c in rows
    ]
    return _finding(
        f"{len(rows)} open negative parent complaints, {top_n} about {top}",
        "Negative feedback not yet resolved. Repeated themes point to a process problem, not a person.",
        items,
        severity=Severity.HIGH if top_n >= 2 else Severity.WATCH,
    )


# ------------------------------------------------------------------------------------------------ finance
@rule(
    "fees_overdue",
    "Fees overdue",
    Domain.FINANCE,
    Severity.CRITICAL,
    "Accounts",
    "Principal, then school owner",
)
def fees_overdue(school: Any, conf: MonitoringSettings, now: datetime.datetime) -> Finding | None:
    places = signals.placements(school)
    students = Student.objects.filter(pk__in=list(places))
    rows = [r for r in fees.defaulters(students, clock.today(school)) if r.overdue > conf.fee_overdue]
    items = [{**_student_item(places[r.student.pk], f"{r.overdue:.2f}"), "staff_id": None} for r in rows]
    total = sum((r.overdue for r in rows), Decimal(0))
    return _finding(
        f"{len(items)} {_plural(len(items), 'family owes', 'families owe')} more than {conf.fee_overdue:.0f}",
        f"Instalments past their due date are unpaid. Total overdue in this group: {total:.2f}.",
        items,
    )


def evaluate_rules(school: Any, now: datetime.datetime | None = None) -> dict[str, Finding | None]:
    now = now or timezone.now()
    conf = signals.settings_for(school)
    return {key: r.check(school, conf, now) for key, r in RULES.items()}
