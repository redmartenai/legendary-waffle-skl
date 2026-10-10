"""Reports: scoped to the caller, JSON or CSV (with formula neutralising), audited, permission-checked."""

import datetime
from decimal import Decimal

import pytest
from django.utils import timezone

from eduflow.attendance.models import AttendanceRecord, AttendanceSession
from eduflow.audit.models import AuditEvent
from eduflow.fees.models import FeePlan, Instalment, StudentFee

pytestmark = pytest.mark.django_db


def _ok(response, expect=200):
    assert response.status_code == expect, response.content
    return response


def _attendance(world, student, enrollment, section, day, status):
    now = timezone.now()
    session, _ = AttendanceSession.objects.get_or_create(
        school=world.school,
        section=section,
        date=day,
        defaults={
            "academic_year": world.year,
            "taken_by": world.admin,
            "submitted_at": now,
            "locked_at": now,
        },
    )
    AttendanceRecord.objects.create(
        school=world.school,
        session=session,
        student=student,
        enrollment=enrollment,
        status=status,
        section=section,
        date=day,
    )


def test_attendance_report_is_scoped_and_exports_csv(world, as_member):
    day = timezone.localdate()
    _attendance(world, world.student, world.enrollment, world.section_a, day, "present")
    _attendance(world, world.other_student, world.other_enrollment, world.section_b, day, "absent")
    principal = _ok(as_member(world.principal).get("/api/v1/reports/attendance")).json()
    assert [r[1] for r in principal["rows"]] == ["Asha", "Ravi"]
    teacher = _ok(as_member(world.teacher).get("/api/v1/reports/attendance")).json()
    assert [(r[1], r[-1]) for r in teacher["rows"]] == [("Asha", "100.0")]
    csv_response = _ok(as_member(world.principal).get("/api/v1/reports/attendance?export=csv"))
    assert csv_response["Content-Type"].startswith("text/csv")
    assert b"".join(csv_response).decode().splitlines()[0].startswith("Admission no.,Student,Class")
    assert AuditEvent.objects.filter(action="reports.attendance.exported").count() == 3
    _ok(as_member(world.principal).get("/api/v1/reports/attendance?from=2020-01-01&to=2026-01-01"), 400)


def test_fee_dues_and_permissions(world, as_member):
    plan = FeePlan.objects.create(school=world.school, name="T", academic_year=world.year)
    Instalment.objects.create(
        school=world.school, plan=plan, label="T1", due_date=datetime.date(2026, 6, 15), amount=Decimal(1000)
    )
    StudentFee.objects.create(school=world.school, student=world.student, plan=plan)
    rows = _ok(as_member(world.admin).get("/api/v1/reports/fee-dues")).json()["rows"]
    assert rows[0][1:4] == ["Asha", "5a", "1000.00"]
    _ok(as_member(world.teacher).get("/api/v1/reports/fee-dues"), 403)  # no fee.read
    parent = _ok(as_member(world.parent).get("/api/v1/reports/fee-dues")).json()
    assert [r[1] for r in parent["rows"]] == ["Asha"]


def test_csv_cells_cannot_become_formulas(world, as_member):
    from eduflow.people.models import Student

    Student.objects.filter(pk=world.student.pk).update(first_name="=HYPERLINK(1)")
    text = b"".join(as_member(world.principal).get("/api/v1/reports/attendance?export=csv")).decode()
    assert "'=HYPERLINK(1)" in text


def test_admissions_and_staff_reports(world, as_member):
    assert _ok(as_member(world.principal).get("/api/v1/reports/admissions")).json()["rows"][-1][0] == "all"
    _ok(as_member(world.teacher).get("/api/v1/reports/admissions"), 403)
    assert (
        _ok(as_member(world.principal).get("/api/v1/reports/staff-attendance")).json()["columns"][0]
        == "Employee ID"
    )
    _ok(as_member(world.principal).get("/api/v1/reports/exam-results"), 400)
