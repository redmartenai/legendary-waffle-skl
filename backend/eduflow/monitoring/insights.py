"""Student risk, teacher scorecards and the school pulse: the prototype's formulas over real data.

Student risk (prototype ``studentRisk``): attendance below the risk line 40 (below 85%: 15); slipping 20;
marks fell by the drop threshold 30 (else latest average below 45%: 20); fees overdue above the threshold
15; behaviour incidents this term at the threshold 15; two or more homework missed 10. Capped at 100;
40+ at risk, 15+ watch.

Teacher scorecard (prototype ``teacherScorecard``)::

    100 * (0.20 punctuality + 0.25 marks_on_time + 0.25 review_rate
           + 0.15 max(0, 1 - reply_hours / 36) + 0.15 clamp(0.6 + class_avg_delta / 20, 0, 1))

Grade: Excellent >= 80, Good >= 65, otherwise Needs support. A component with no data counts as perfect
(as in the prototype) and the response says which components had no data.
"""

from __future__ import annotations

import datetime
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from django.db.models import Count, Q, Sum
from django.utils import timezone

from eduflow.approvals.registry import all_pending
from eduflow.assessment import services as assessment
from eduflow.assessment.models import Mark, MarkSheet, SheetStatus
from eduflow.attendance.models import AttendanceRecord
from eduflow.communication import services as communication
from eduflow.conduct import services as conduct
from eduflow.fees import services as fees
from eduflow.fees.models import Payment
from eduflow.homework import services as homework
from eduflow.homework.models import HomeworkStatus, Submission, SubmissionStatus
from eduflow.hr import services as hr
from eduflow.hr.models import StaffAttendance, StaffDayStatus
from eduflow.people.models import StaffProfile, Student
from eduflow.tenancy import clock
from eduflow.transport.models import Trip, TripStatus

from . import signals
from .models import Alert, AlertStatus


# ------------------------------------------------------------------------------------------------ risk
@dataclass
class Risk:
    student: Student
    score: int = 0
    factors: list[dict[str, Any]] = field(default_factory=list)

    @property
    def level(self) -> str:
        return "at_risk" if self.score >= 40 else "watch" if self.score >= 15 else "ok"

    def add(self, key: str, label: str, weight: int) -> None:
        self.factors.append({"key": key, "label": label, "weight": weight})
        self.score = min(100, self.score + weight)


def student_risk(school: Any, students: Iterable[Student]) -> list[Risk]:
    conf = signals.settings_for(school)
    students = list(students)
    today = clock.today(school)
    series = signals.attendance_series(school, conf.attendance_window_days, students)
    trends = assessment.exam_percentages(school)
    statements = {s.student.pk: s for s in (fees.statement(st, today) for st in students)}
    term = conduct.current_term(school)
    incidents = conduct.incident_counts(school, term.start_date, term.end_date) if term else {}
    missed = homework.missed_counts(
        school,
        today - datetime.timedelta(days=conf.attendance_window_days),
        today - datetime.timedelta(days=1),
    )
    out = []
    for st in students:
        risk = Risk(st)
        statuses = series.get(st.pk, [])
        if statuses:
            r = signals.rate(statuses)
            if r < float(conf.attendance_risk):
                risk.add("attendance", f"Attendance {round(r * 100)}%", 40)
            elif r < 0.85:
                risk.add("attendance", f"Attendance {round(r * 100)}%", 15)
            recent, prior = statuses[-conf.slip_recent_days :], statuses[: -conf.slip_recent_days]
            if prior and signals.rate(prior) - signals.rate(recent) >= float(conf.slip_drop):
                risk.add("attendance", f"Slipping: {round(signals.rate(recent) * 100)}% recently", 20)
        t = assessment.trend(trends.get(st.pk, []))
        if t is not None and t[2] <= -conf.marks_drop:
            risk.add("marks", f"Marks down {round(-t[2])} pts", 30)
        elif trends.get(st.pk) and trends[st.pk][-1][1] < 45:
            risk.add("marks", f"Average {round(trends[st.pk][-1][1])}%", 20)
        overdue = statements[st.pk].overdue
        if overdue > conf.fee_overdue:
            risk.add("fees", f"{overdue:.2f} overdue", 15)
        if incidents.get(st.pk, 0) >= conf.behaviour_incidents:
            risk.add("behaviour", f"{incidents[st.pk]} behaviour notes", 15)
        if missed.get(st.pk, 0) >= 2:
            risk.add("homework", f"{missed[st.pk]} homework missed", 10)
        out.append(risk)
    return sorted(out, key=lambda r: -r.score)


# ------------------------------------------------------------------------------------------------ scorecards
@dataclass
class Scorecard:
    staff: StaffProfile
    punctuality: float
    marks_on_time: float
    review_rate: float
    reply_hours: float | None
    class_avg_delta: float
    pending_marks: int
    unreviewed: int
    unanswered: int
    no_data: list[str]

    @property
    def score(self) -> int:
        reply = max(0.0, 1 - self.reply_hours / 36) if self.reply_hours is not None else 1.0
        result = max(0.0, min(1.0, 0.6 + self.class_avg_delta / 20))
        return round(
            100
            * (
                0.2 * self.punctuality
                + 0.25 * self.marks_on_time
                + 0.25 * self.review_rate
                + 0.15 * reply
                + 0.15 * result
            )
        )

    @property
    def grade(self) -> str:
        return "Excellent" if self.score >= 80 else "Good" if self.score >= 65 else "Needs support"


def scorecards(school: Any, staff: Iterable[StaffProfile], *, days: int = 30) -> list[Scorecard]:
    today = clock.today(school)
    since = today - datetime.timedelta(days=days)
    punctual = hr.punctuality(school, since, today)
    replies = communication.reply_hours(school, timezone.now() - datetime.timedelta(days=days))
    unanswered: dict[Any, int] = defaultdict(int)
    for thread in communication.unanswered(school):
        unanswered[thread.staff_id] += 1
    cards = []
    for member in staff:
        no_data: list[str] = []
        sheets = MarkSheet.objects.filter(school_id=school.pk, teacher=member, exam__marks_deadline__lt=today)
        total_sheets = sheets.count()
        pending = sheets.filter(status=SheetStatus.DRAFT).count()  # past the deadline, still not submitted
        if not total_sheets:
            no_data.append("marks_on_time")
        subs = Submission.objects.filter(
            school_id=school.pk,
            homework__teacher=member,
            homework__due_date__lt=today,
            homework__status=HomeworkStatus.PUBLISHED,
        )
        submitted = subs.count()
        reviewed = subs.filter(status=SubmissionStatus.REVIEWED).count()
        if not submitted:
            no_data.append("review_rate")
        if member.pk not in punctual:
            no_data.append("punctuality")
        if member.pk not in replies:
            no_data.append("reply_hours")
        delta = _class_avg_delta(school, member)
        if delta is None:
            no_data.append("class_avg_delta")
        cards.append(
            Scorecard(
                staff=member,
                punctuality=punctual.get(member.pk, 1.0),
                marks_on_time=1 - pending / total_sheets if total_sheets else 1.0,
                review_rate=reviewed / submitted if submitted else 1.0,
                reply_hours=replies.get(member.pk),
                class_avg_delta=delta or 0.0,
                pending_marks=pending,
                unreviewed=submitted - reviewed,
                unanswered=unanswered.get(member.pk, 0),
                no_data=no_data,
            )
        )
    return sorted(cards, key=lambda c: -c.score)


def _class_avg_delta(school: Any, staff: StaffProfile) -> float | None:
    """Mean change, latest exam versus earlier ones, of the marks this teacher's sheets hold."""
    sheets = MarkSheet.objects.filter(school_id=school.pk, teacher=staff).exclude(status=SheetStatus.DRAFT)
    per_student: dict[Any, list[tuple[datetime.date, Decimal]]] = defaultdict(list)
    for student_id, starts, value, out_of in Mark.objects.filter(sheet__in=sheets, absent=False).values_list(
        "student_id", "sheet__exam__starts_on", "marks", "sheet__max_marks"
    ):
        per_student[student_id].append((starts, (value or Decimal(0)) * 100 / out_of))
    deltas = []
    for values in per_student.values():
        values.sort()
        if len(values) >= 2:
            earlier = [v for _, v in values[:-1]]
            deltas.append(float(values[-1][1] - sum(earlier, Decimal(0)) / len(earlier)))
    return sum(deltas) / len(deltas) if deltas else None


# ------------------------------------------------------------------------------------------------ pulse
def pulse(school: Any) -> dict[str, Any]:
    today = clock.today(school)
    records = AttendanceRecord.objects.filter(school_id=school.pk, date=today)
    marked = records.count()
    present = records.exclude(status="absent").count()
    due = signals.sections_due_today(school, today)
    missing = signals.registers_missing(school, today)
    staff_total = StaffProfile.objects.filter(school_id=school.pk, status="active").count()
    staff_in = StaffAttendance.objects.filter(
        school_id=school.pk, date=today, status__in=[StaffDayStatus.PRESENT, StaffDayStatus.LATE]
    ).count()
    trips = Trip.objects.filter(school_id=school.pk, date=today)
    month_start = today.replace(day=1)
    collected = Payment.objects.filter(
        school_id=school.pk, paid_on__gte=month_start, paid_on__lte=today
    ).aggregate(t=Sum("amount"))["t"] or Decimal(0)
    pending = len(all_pending(school))
    alerts = Alert.objects.filter(school_id=school.pk).exclude(status=AlertStatus.RESOLVED)
    by_severity = {r["severity"]: r["n"] for r in alerts.values("severity").annotate(n=Count("id"))}
    trend = [
        {"date": d, "rate": round(n_present / n, 4) if n else None}
        for d, n, n_present in _daily_attendance(school, today - datetime.timedelta(days=60), today)
    ]
    critical_open = alerts.filter(severity="critical", status=AlertStatus.OPEN).count()
    line = f"{len(due) - len(missing)} of {len(due)} classrooms have taken the register"
    if marked:
        line += f", {round(100 * present / marked)}% of children are in"
    line += f", {staff_in} of {staff_total} staff checked in."
    return {
        "date": today,
        "students": Student.objects.filter(school_id=school.pk, status="active").count(),
        "registers_marked": len(due) - len(missing),
        "registers_due": len(due),
        "present_today": present,
        "marked_today": marked,
        "attendance_today": round(present / marked, 4) if marked else None,
        "staff_in": staff_in,
        "staff_total": staff_total,
        "trips_arrived": trips.filter(status=TripStatus.ARRIVED).count(),
        "trips_today": trips.count(),
        "pending_approvals": pending,
        "collected_this_month": collected,
        "open_alerts": {s: by_severity.get(s, 0) for s in ("critical", "high", "watch")},
        "attendance_trend": trend,
        "brief": line,
        "focus": (
            f"{critical_open} critical alert{'s' if critical_open != 1 else ''} need you."
            if critical_open
            else "Nothing urgent."
        ),
    }


def _daily_attendance(
    school: Any, since: datetime.date, until: datetime.date
) -> list[tuple[datetime.date, int, int]]:
    rows = (
        AttendanceRecord.objects.filter(school_id=school.pk, date__gte=since, date__lte=until)
        .values("date")
        .annotate(n=Count("id"), present=Count("id", filter=~Q(status="absent")))
        .order_by("date")
    )
    return [(r["date"], r["n"], r["present"]) for r in rows]
