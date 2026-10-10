"""Every rule fires on the data it describes, with the owner it names (one seeded school, one evaluation)."""

import datetime
from decimal import Decimal

import pytest
from django.utils import timezone

from eduflow.academics.models import Term
from eduflow.assessment.models import Exam, Mark, MarkSheet
from eduflow.attendance.models import AttendanceRecord, AttendanceSession
from eduflow.communication.models import Complaint, Thread
from eduflow.conduct.models import Incident
from eduflow.homework.models import Homework, Submission
from eduflow.hr.models import LeaveRequest, LeaveType, StaffAttendance
from eduflow.monitoring import engine, signals
from eduflow.monitoring.models import Alert, MonitoringSettings
from eduflow.people.models import TeacherAssignment
from eduflow.transport.models import Route, Trip, Vehicle

pytestmark = pytest.mark.django_db

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
TODAY = datetime.date(2026, 7, 14)


@pytest.fixture(autouse=True)
def _clock(time_machine):
    time_machine.move_to(datetime.datetime(2026, 7, 14, 10, 30, tzinfo=IST), tick=False)


def _day(n: int) -> datetime.date:
    return TODAY - datetime.timedelta(days=n)


def _seed(world, monkeypatch):
    school, teacher = world.school, world.teacher_staff
    TeacherAssignment.objects.filter(pk=world.assignment.pk).update(is_class_teacher=True)
    MonitoringSettings.objects.create(school=school, unreviewed_submissions=1, late_arrivals=1)
    now = timezone.now()
    # Attendance slipping: 20 days present, then 15 days with 5 early absences (no streak today).
    statuses = ["present"] * 20 + ["absent"] * 5 + ["present"] * 10
    for i, status in enumerate(statuses):
        day = _day(len(statuses) - i)
        session = AttendanceSession.objects.create(
            school=school,
            section=world.section_a,
            academic_year=world.year,
            date=day,
            taken_by=world.teacher,
            submitted_at=now,
            locked_at=now,
        )
        AttendanceRecord.objects.create(
            school=school,
            session=session,
            student=world.student,
            enrollment=world.enrollment,
            status=status,
            section=world.section_a,
            date=day,
        )
    # Falling marks: 80% then 60%.
    for name, start, got in [("UT1", datetime.date(2026, 6, 10), 40), ("UT2", datetime.date(2026, 7, 1), 30)]:
        exam = Exam.objects.create(
            school=school,
            name=name,
            academic_year=world.year,
            starts_on=start,
            ends_on=start,
            marks_deadline=start,
        )
        sheet = MarkSheet.objects.create(
            school=school,
            exam=exam,
            section=world.section_a,
            subject=world.subject,
            teacher=teacher,
            max_marks=50,
            status="submitted",
            submitted_at=now,
        )
        Mark.objects.create(school=school, sheet=sheet, student=world.student, marks=Decimal(got))
    # Marks overdue: a draft sheet past its deadline.
    late = Exam.objects.create(
        school=school,
        name="UT3",
        academic_year=world.year,
        starts_on=_day(10),
        ends_on=_day(9),
        marks_deadline=_day(5),
    )
    MarkSheet.objects.create(
        school=school,
        exam=late,
        section=world.section_a,
        subject=world.subject,
        teacher=teacher,
        max_marks=50,
    )
    # Behaviour: three incidents this term.
    Term.objects.create(
        school=school,
        academic_year=world.year,
        name="Term 1",
        code="t1",
        start_date=datetime.date(2026, 6, 1),
        end_date=datetime.date(2026, 9, 30),
    )
    for i in range(3):
        Incident.objects.create(
            school=school,
            student=world.student,
            occurred_on=_day(i + 1),
            category="x",
            description="x",
            severity="minor",
        )
    # Homework unreviewed (threshold lowered to 1).
    hw = Homework.objects.create(
        school=school,
        section=world.section_a,
        subject=world.subject,
        teacher=teacher,
        title="x",
        assigned_on=_day(5),
        due_date=_day(2),
    )
    Submission.objects.create(
        school=school, homework=hw, student=world.student, status="submitted", submitted_at=now
    )
    # Parent waiting 30 hours.
    Thread.objects.create(
        school=school,
        kind="parent_class_teacher",
        student=world.student,
        staff=teacher,
        guardian=world.guardian,
        awaiting_reply_since=now - datetime.timedelta(hours=30),
    )
    # Late arrivals (threshold lowered to 1); no check-in today -> absent without leave.
    StaffAttendance.objects.create(
        school=school, staff=teacher, date=_day(1), status="late", check_in=datetime.time(8, 30)
    )
    # Approvals waiting more than a day.
    lt = LeaveType.objects.create(school=school, name="Casual", days_per_year=5)
    leave = LeaveRequest.objects.create(
        school=school,
        staff=teacher,
        leave_type=lt,
        start_date=_day(-3),
        end_date=_day(-3),
        days=1,
        reason="x",
    )
    LeaveRequest.objects.filter(pk=leave.pk).update(created_at=now - datetime.timedelta(hours=30))
    # Bus delayed.
    vehicle = Vehicle.objects.create(school=school, registration_number="KA1", label="Bus 7", capacity=30)
    route = Route.objects.create(school=school, name="North", code="north", vehicle=vehicle)
    Trip.objects.create(
        school=school,
        route=route,
        date=TODAY,
        direction="morning",
        status="on_route",
        started_at=now,
        delay_minutes=15,
    )
    # Negative feedback.
    for _ in range(2):
        Complaint.objects.create(
            school=school,
            student=world.student,
            category="transport",
            text="late",
            sentiment="negative",
            raised_by=world.parent,
        )
    # A school day with no register taken yet: section A is due today (as if its timetable said so).
    monkeypatch.setattr(signals, "sections_due_today", lambda school, day: {world.section_a.pk})


def test_every_rule_fires_with_its_owner(world, monkeypatch, as_member):
    _seed(world, monkeypatch)
    engine.evaluate(world.school)
    fired = set(Alert.objects.values_list("rule", flat=True))
    assert fired >= {
        "attendance_slipping",
        "falling_marks",
        "behaviour",
        "register_missing",
        "marks_overdue",
        "homework_unreviewed",
        "parent_waiting",
        "staff_absent",
        "staff_late",
        "bus_delayed",
        "approvals_waiting",
        "negative_feedback",
    }
    assert "attendance_risk" not in fired  # 30 of 35 days: above 75%
    assert Alert.objects.get(rule="negative_feedback").severity == "high"  # 2+ about one category
    assert Alert.objects.get(rule="bus_delayed").headline == "Bus 7 is 15 min late"
    staff_id = str(world.teacher_staff.pk)
    for rule in (
        "falling_marks",
        "register_missing",
        "marks_overdue",
        "homework_unreviewed",
        "parent_waiting",
    ):
        assert staff_id in [i.get("staff_id") for i in Alert.objects.get(rule=rule).items], rule
    teacher_view = as_member(world.teacher).get("/api/v1/monitoring/alerts?page_size=50").json()["results"]
    assert {a["rule"] for a in teacher_view} >= {"register_missing", "parent_waiting", "falling_marks"}
    assert "bus_delayed" not in {a["rule"] for a in teacher_view}  # no row owned by the teacher
    # A register taken resolves "register not marked" at the next evaluation.
    now = timezone.now()
    AttendanceSession.objects.create(
        school=world.school,
        section=world.section_a,
        academic_year=world.year,
        date=TODAY,
        taken_by=world.teacher,
        submitted_at=now,
        locked_at=now,
    )
    engine.evaluate(world.school)
    assert Alert.objects.get(rule="register_missing").status == "resolved"


def test_rules_wait_for_the_register_time(world, monkeypatch, time_machine):
    _seed(world, monkeypatch)
    time_machine.move_to(datetime.datetime(2026, 7, 14, 8, 0, tzinfo=IST), tick=False)
    engine.evaluate(world.school)
    fired = set(Alert.objects.values_list("rule", flat=True))
    assert "register_missing" not in fired
    assert "staff_absent" not in fired


def test_the_beat_task_fans_out_one_tenant_task_per_school(world, other_world, monkeypatch):
    from eduflow.tenancy.tasks import run_school_jobs

    _seed(world, monkeypatch)
    assert run_school_jobs("frequent") >= 2  # eager in tests: each school's jobs run in its own context
    assert Alert.objects.filter(school=world.school, rule="bus_delayed").exists()
    assert not Alert.objects.filter(school=other_world.school, rule="bus_delayed").exists()
