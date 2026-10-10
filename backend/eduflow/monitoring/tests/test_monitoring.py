"""Monitoring: rules over real data, one deduplicated alert per rule, owner notification, role-scoped views,
acknowledge / resolve / auto-resolve, escalation, thresholds, risk, scorecards, pulse, Ask EduFlow with
permission-aware answers, the scheduled job, isolation and RLS."""

import datetime
import uuid
from decimal import Decimal

import pytest
from django.utils import timezone

from eduflow.attendance.models import AttendanceRecord, AttendanceSession
from eduflow.core import db_context
from eduflow.core.db_context import DbContext
from eduflow.fees.models import FeePlan, Instalment, StudentFee
from eduflow.monitoring import engine, insights
from eduflow.monitoring.models import Alert
from eduflow.notifications.models import Notification
from eduflow.people.models import TeacherAssignment
from eduflow.tenancy import jobs

pytestmark = pytest.mark.django_db

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
TODAY = datetime.date(2026, 7, 14)


def _ok(response, expect=200):
    assert response.status_code == expect, response.content
    return response.json() if response.content else None


@pytest.fixture(autouse=True)
def _clock(time_machine):
    time_machine.move_to(datetime.datetime(2026, 7, 14, 10, 30, tzinfo=IST), tick=False)


@pytest.fixture
def class_teacher(world):
    TeacherAssignment.objects.filter(pk=world.assignment.pk).update(is_class_teacher=True)
    return world.teacher_staff


def _registers(world, statuses):
    """One register per day ending today for Asha's section; statuses oldest first."""
    days = [TODAY - datetime.timedelta(days=len(statuses) - 1 - i) for i in range(len(statuses))]
    now = timezone.now()
    for day, status in zip(days, statuses, strict=True):
        session = AttendanceSession.objects.create(
            school=world.school,
            section=world.section_a,
            academic_year=world.year,
            date=day,
            taken_by=world.teacher,
            submitted_at=now,
            locked_at=now,
        )
        AttendanceRecord.objects.create(
            school=world.school,
            session=session,
            student=world.student,
            enrollment=world.enrollment,
            status=status,
            section=world.section_a,
            date=day,
        )


def test_attendance_risk_alert_lifecycle(world, class_teacher, as_member):
    _registers(world, ["present"] * 4 + ["absent"] * 6)
    result = engine.evaluate(world.school)
    assert result["opened"] >= 1
    alert = Alert.objects.get(rule="attendance_risk")
    assert (alert.severity, alert.status, alert.items[0]["value"]) == ("critical", "open", "40%")
    assert Notification.objects.filter(recipient=world.teacher, kind="alert").exists()
    assert Notification.objects.filter(recipient=world.principal, kind="alert").exists()
    assert Alert.objects.filter(rule="absent_streak").exists()  # absent today and the two days before
    engine.evaluate(world.school)
    assert Alert.objects.filter(rule="attendance_risk").count() == 1
    assert Alert.objects.get(rule="attendance_risk").occurrences == 2
    teacher = as_member(world.teacher)
    mine = _ok(teacher.get("/api/v1/monitoring/alerts?rule=attendance_risk"))["results"]
    assert [i["label"] for i in mine[0]["items"]] == ["Asha"]
    _ok(teacher.post(f"/api/v1/monitoring/alerts/{alert.pk}/acknowledge"))
    _ok(teacher.post(f"/api/v1/monitoring/alerts/{alert.pk}/acknowledge"), 409)
    _ok(teacher.post(f"/api/v1/monitoring/alerts/{alert.pk}/resolve", {}, format="json"), 403)
    # Attendance recovers: the rule stops firing and the alert resolves itself.
    AttendanceRecord.objects.filter(student=world.student).update(status="present")
    engine.evaluate(world.school)
    alert.refresh_from_db()
    assert (alert.status, alert.auto_resolved) == ("resolved", True)
    assert as_member(world.parent).get("/api/v1/monitoring/alerts").status_code == 403


def test_unacknowledged_alerts_escalate_once(world, class_teacher):
    _registers(world, ["absent"] * 10)
    engine.evaluate(world.school)
    Notification.objects.all().delete()
    later = timezone.now() + datetime.timedelta(days=8)
    assert engine.evaluate(world.school, now=later)["escalated"] >= 1
    assert Alert.objects.get(rule="attendance_risk").escalated_at is not None
    assert Notification.objects.filter(recipient=world.principal, title__startswith="Escalated").exists()
    Notification.objects.all().delete()
    engine.evaluate(world.school, now=later + datetime.timedelta(hours=1))
    assert not Notification.objects.filter(title__startswith="Escalated: 1 student is at attendance").exists()


def test_thresholds_and_manual_resolution(world, class_teacher, as_member):
    _registers(world, ["present"] * 7 + ["absent"] * 3)
    principal = as_member(world.principal)
    engine.evaluate(world.school)
    assert Alert.objects.get(rule="attendance_risk").items[0]["value"] == "70%"  # below the default 75%
    _ok(principal.put("/api/v1/monitoring/settings", {"attendance_risk": "0.65"}, format="json"))
    _ok(principal.post("/api/v1/monitoring/evaluate"))
    assert not Alert.objects.filter(rule="attendance_risk").exclude(status="resolved").exists()
    _ok(principal.put("/api/v1/monitoring/settings", {"attendance_risk": "0.80"}, format="json"))
    _ok(principal.post("/api/v1/monitoring/evaluate"))
    alert = Alert.objects.exclude(status="resolved").get(rule="attendance_risk")
    resolved = _ok(
        principal.post(
            f"/api/v1/monitoring/alerts/{alert.pk}/resolve", {"note": "Spoke to the family"}, format="json"
        )
    )
    assert resolved["status"] == "resolved"
    _ok(principal.post("/api/v1/monitoring/evaluate"))
    assert Alert.objects.filter(rule="attendance_risk").count() == 3  # fires again: a new alert each time
    assert len(_ok(principal.get("/api/v1/monitoring/rules"))) >= 14
    _ok(
        as_member(world.teacher).put(
            "/api/v1/monitoring/settings", {"attendance_risk": "0.5"}, format="json"
        ),
        403,
    )


def _overdue_fees(world):
    plan = FeePlan.objects.create(school=world.school, name="Tuition", academic_year=world.year)
    Instalment.objects.create(
        school=world.school, plan=plan, label="T1", due_date=datetime.date(2026, 6, 15), amount=Decimal(30000)
    )
    StudentFee.objects.create(school=world.school, student=world.student, plan=plan)


def test_fees_and_risk(world, class_teacher, as_member):
    _overdue_fees(world)
    _registers(world, ["present"] * 7 + ["absent"] * 3)
    engine.evaluate(world.school)
    fees_alert = Alert.objects.get(rule="fees_overdue")
    assert fees_alert.items[0]["value"] == "30000.00"
    assert fees_alert.staff_ids == []  # finance is the office's, not the class teacher's
    risk = _ok(as_member(world.principal).get(f"/api/v1/students/{world.student.pk}/risk"))
    assert (risk["score"], risk["level"]) == (55, "at_risk")
    assert {f["key"] for f in risk["factors"]} == {"attendance", "fees"}
    teacher_view = _ok(as_member(world.teacher).get("/api/v1/monitoring/risk"))
    assert [r["student"]["full_name"] for r in teacher_view] == ["Asha"]
    assert as_member(world.teacher).get(f"/api/v1/students/{world.other_student.pk}/risk").status_code == 404


def test_scorecards_and_pulse(world, class_teacher, as_member):
    _registers(world, ["present", "absent"])
    principal = as_member(world.principal)
    cards = _ok(principal.get("/api/v1/monitoring/scorecards"))
    assert [c["name"] for c in cards] == [world.teacher.user.full_name]
    assert cards[0]["score"] == 94  # no data anywhere scores full, except the 0.6 baseline on results
    assert set(cards[0]["no_data"]) >= {"marks_on_time", "review_rate", "punctuality", "reply_hours"}
    own = _ok(as_member(world.teacher).get("/api/v1/monitoring/scorecards"))
    assert [c["staff_id"] for c in own] == [str(world.teacher_staff.pk)]
    pulse = _ok(principal.get("/api/v1/monitoring/pulse"))
    assert (pulse["marked_today"], pulse["present_today"], pulse["attendance_today"]) == (1, 0, 0.0)
    assert pulse["attendance_trend"][-1] == {"date": "2026-07-14", "rate": 0.0}
    assert as_member(world.teacher).get("/api/v1/monitoring/pulse").status_code == 403


def test_ask_answers_within_the_callers_permissions(world, class_teacher, as_member):
    _overdue_fees(world)
    _registers(world, ["present"] * 4 + ["absent"] * 6)
    principal, teacher = as_member(world.principal), as_member(world.teacher)

    def ask(client, q):
        return _ok(client.post("/api/v1/monitoring/ask", {"question": q}, format="json"))

    below = ask(principal, "Show students below 75% attendance")
    assert (below["intent"], below["rows"][0]["cells"][:3]) == ("attendance_below", ["Asha", "5a", "40%"])
    assert ask(principal, "students below 30% attendance")["rows"] == []
    assert ask(principal, "who is absent today?")["rows"][0]["cells"][0] == "Asha"
    fees = ask(principal, "Show unpaid fees above 20000")
    assert (fees["intent"], len(fees["rows"])) == ("fees_overdue", 1)
    assert ask(principal, "unpaid fees above 50k")["rows"] == []
    denied = ask(teacher, "Show unpaid fees above 20000")
    assert (denied["summary"], denied["rows"]) == ("You do not have access to that information.", [])
    assert ask(teacher, "who is absent today in 5a")["rows"][0]["cells"][0] == "Asha"
    assert ask(teacher, "absent today in 5b")["rows"] == []
    assert ask(principal, "students at risk")["rows"][0]["cells"][2] == "55"
    assert ask(principal, "teacher scorecards")["intent"] == "scorecards"
    assert ask(teacher, "teacher scorecards")["rows"] == []
    assert ask(principal, "what is the meaning of life")["intent"] == "unknown"
    assert (
        as_member(world.parent)
        .post("/api/v1/monitoring/ask", {"question": "fees"}, format="json")
        .status_code
        == 403
    )


def test_the_scheduled_job_evaluates_each_school(world, class_teacher):
    _registers(world, ["absent"] * 5)
    assert "monitoring.evaluate" in jobs.JOBS["frequent"]
    result = jobs.run("frequent", world.school)
    assert result["monitoring.evaluate"] != "failed"
    assert Alert.objects.filter(rule="attendance_risk").exists()


MONITORING_MATRIX = [
    ("get", "/api/v1/monitoring/alerts/{alert}", None),
    ("post", "/api/v1/monitoring/alerts/{alert}/acknowledge", None),
    ("post", "/api/v1/monitoring/alerts/{alert}/resolve", {"note": "x"}),
    ("get", "/api/v1/students/{student}/risk", None),
]
MONITORING_MATRIX_PATHS = {p for _, p, _ in MONITORING_MATRIX}


@pytest.mark.parametrize(("method", "path", "body"), MONITORING_MATRIX)
def test_another_schools_alerts_answer_like_unknown(world, other_world, as_member, method, path, body):
    now = timezone.now()
    alert = Alert.objects.create(
        school=other_world.school,
        rule="x",
        title="x",
        domain="students",
        severity="watch",
        headline="x",
        why="x",
        owner="x",
        escalates_to="x",
        first_seen_at=now,
        last_seen_at=now,
    )
    real = {"alert": alert.pk, "student": other_world.student.pk}
    missing = {k: uuid.uuid4() for k in real}
    client = as_member(world.principal)

    def call(values):
        url = path.format(**values)
        return getattr(client, method)(url, body, format="json") if body else getattr(client, method)(url)

    assert call(real).status_code == call(missing).status_code == 404
    assert Alert.objects.get(pk=alert.pk).status == "open"


def test_alerts_are_under_rls(world, other_world):
    now = timezone.now()
    Alert.objects.create(
        school=other_world.school,
        rule="x",
        title="x",
        domain="students",
        severity="watch",
        headline="x",
        why="x",
        owner="x",
        escalates_to="x",
        first_seen_at=now,
        last_seen_at=now,
    )
    with db_context.scoped(DbContext(school_id=world.school.pk)):
        assert not Alert.objects.exists()


def test_risk_weights(world):
    risk = insights.Risk(world.student)
    risk.add("attendance", "x", 40)
    risk.add("marks", "x", 30)
    risk.add("fees", "x", 15)
    risk.add("behaviour", "x", 15)
    risk.add("homework", "x", 10)
    assert (risk.score, risk.level) == (100, "at_risk")
