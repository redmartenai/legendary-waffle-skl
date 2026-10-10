"""HR: self check-in (late after the school's time), office records, leave with balances through the approvals
queue, payroll runs with entered deductions and frozen payslips, recruitment, monitoring inputs, isolation
and RLS."""

import datetime
import uuid
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from eduflow.core import db_context
from eduflow.core.db_context import DbContext
from eduflow.hr import services
from eduflow.hr.models import LeaveRequest, LeaveType, PayrollRun, Payslip, StaffAttendance
from eduflow.notifications.models import Notification

pytestmark = pytest.mark.django_db

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def _ok(response, expect=200):
    assert response.status_code == expect, response.content
    return response.json() if response.content else None


def _at(time_machine, hour, minute, day=14):
    time_machine.move_to(datetime.datetime(2026, 7, day, hour, minute, tzinfo=IST), tick=False)


def test_self_check_in_is_late_after_the_school_time(world, as_member, time_machine):
    _at(time_machine, 7, 55)
    day = _ok(as_member(world.teacher).post("/api/v1/staff-attendance/check-in"))
    assert (day["status"], day["check_in"]) == ("present", "07:55:00")
    _ok(as_member(world.teacher).post("/api/v1/staff-attendance/check-in"), 409)
    _at(time_machine, 16, 0)
    assert _ok(as_member(world.teacher).post("/api/v1/staff-attendance/check-out"))["check_out"] == "16:00:00"
    _at(time_machine, 8, 5, day=15)
    assert _ok(as_member(world.teacher).post("/api/v1/staff-attendance/check-in"))["status"] == "late"
    admin = as_member(world.admin)
    _ok(admin.put("/api/v1/hr/settings", {"late_after": "08:30"}, format="json"))
    _at(time_machine, 8, 5, day=16)
    assert _ok(as_member(world.teacher).post("/api/v1/staff-attendance/check-in"))["status"] == "present"
    # Someone without a staff profile cannot check in.
    _ok(as_member(world.admin).post("/api/v1/staff-attendance/check-in"), 403)
    assert services.late_counts(world.school, datetime.date(2026, 7, 1), datetime.date(2026, 7, 31)) == {
        world.teacher_staff.pk: 1
    }
    assert services.punctuality(world.school, datetime.date(2026, 7, 1), datetime.date(2026, 7, 31)) == {
        world.teacher_staff.pk: 2 / 3
    }


def test_staff_see_only_their_own_days(world, as_member, make_member, time_machine):
    from eduflow.people.models import StaffProfile

    _at(time_machine, 9, 0)
    other = make_member(world.school, roles=["teacher"])
    StaffProfile.objects.create(school=world.school, membership=other, employee_id="T-2")
    as_member(other).post("/api/v1/staff-attendance/check-in")
    as_member(world.teacher).post("/api/v1/staff-attendance/check-in")
    assert len(_ok(as_member(world.teacher).get("/api/v1/staff-attendance"))["results"]) == 1
    assert len(_ok(as_member(world.admin).get("/api/v1/staff-attendance"))["results"]) == 2
    absent = services.absent_without_leave(world.school, datetime.date(2026, 7, 14))
    assert absent == []
    assert [s.pk for s in services.absent_without_leave(world.school, datetime.date(2026, 7, 13))] == [
        world.teacher_staff.pk,
        StaffProfile.objects.get(employee_id="T-2").pk,
    ]


def test_office_records_a_day(world, as_member, time_machine):
    _at(time_machine, 12, 0)
    admin = as_member(world.admin)
    body = {
        "staff_id": str(world.teacher_staff.pk),
        "date": "2026-07-13",
        "status": "late",
        "check_in": "08:40",
    }
    assert _ok(admin.post("/api/v1/staff-attendance", body, format="json"), 201)["status"] == "late"
    _ok(admin.post("/api/v1/staff-attendance", {**body, "check_in": None}, format="json"), 400)
    _ok(admin.post("/api/v1/staff-attendance", {**body, "date": "2026-07-20"}, format="json"), 400)
    _ok(as_member(world.teacher).post("/api/v1/staff-attendance", body, format="json"), 403)


def test_leave_through_the_queue_with_balances(world, as_member, time_machine):
    _at(time_machine, 9, 0)
    admin, teacher = as_member(world.admin), as_member(world.teacher)
    casual = _ok(
        admin.post("/api/v1/leave-types", {"name": "Casual", "days_per_year": "3"}, format="json"), 201
    )
    assert [t["name"] for t in _ok(teacher.get("/api/v1/leave-types"))["results"]] == ["Casual"]
    body = {
        "leave_type_id": casual["id"],
        "start_date": "2026-07-20",
        "end_date": "2026-07-21",
        "reason": "Family function",
    }
    leave = _ok(teacher.post("/api/v1/leave-requests", body, format="json"), 201)
    assert leave["days"] == "2.0"
    _ok(teacher.post("/api/v1/leave-requests", body, format="json"), 409)  # overlapping
    later = {**body, "start_date": "2026-07-22", "end_date": "2026-07-23"}
    _ok(teacher.post("/api/v1/leave-requests", later, format="json"), 400)  # 2 pending + 2 > 3
    one = _ok(teacher.post("/api/v1/leave-requests", {**later, "end_date": "2026-07-22"}, format="json"), 201)
    queue = _ok(as_member(world.principal).get("/api/v1/approvals"))
    assert [i["kind"] for i in queue] == ["leave", "leave"]
    principal = as_member(world.principal)
    _ok(
        principal.post(
            f"/api/v1/approvals/leave/{leave['id']}/decision", {"decision": "approve"}, format="json"
        )
    )
    assert StaffAttendance.objects.filter(staff=world.teacher_staff, status="leave").count() == 2
    assert Notification.objects.filter(recipient=world.teacher, title="Leave approved").exists()
    assert _ok(teacher.get("/api/v1/leave-balances")) == [{"leave_type": casual, "remaining": "1.0"}]
    _ok(teacher.post(f"/api/v1/leave-requests/{one['id']}/cancel"))
    _ok(teacher.post(f"/api/v1/leave-requests/{leave['id']}/cancel"), 409)
    _ok(
        principal.post(
            f"/api/v1/approvals/leave/{one['id']}/decision", {"decision": "approve"}, format="json"
        ),
        409,
    )


def test_payroll_uses_entered_amounts(world, as_member, time_machine):
    _at(time_machine, 9, 0)
    admin = as_member(world.admin)
    salary = {
        "basic": "40000",
        "hra": "16000",
        "allowances": "4000",
        "pf": "4800",
        "esi": "0",
        "tds": "2500",
        "bank_account_last4": "1234",
    }
    _ok(admin.put(f"/api/v1/staff/{world.teacher_staff.pk}/salary", salary, format="json"))
    assert (
        _ok(as_member(world.teacher).get(f"/api/v1/staff/{world.teacher_staff.pk}/salary"))["basic"]
        == "40000.00"
    )
    run = _ok(admin.post("/api/v1/payroll-runs", {"month": "2026-07"}, format="json"), 201)
    _ok(admin.post("/api/v1/payroll-runs", {"month": "2026-07"}, format="json"), 409)
    _ok(admin.post(f"/api/v1/payroll-runs/{run['id']}/paid"), 409)
    processed = _ok(admin.post(f"/api/v1/payroll-runs/{run['id']}/process"))
    assert (
        processed["status"],
        processed["gross"],
        processed["deductions"],
        processed["net"],
        processed["staff_count"],
    ) == ("processed", "60000.00", "7300.00", "52700.00", 1)
    _ok(admin.post(f"/api/v1/payroll-runs/{run['id']}/process"), 409)
    # Later salary changes do not touch a processed payslip.
    _ok(
        admin.put(
            f"/api/v1/staff/{world.teacher_staff.pk}/salary", {**salary, "basic": "50000"}, format="json"
        )
    )
    slips = _ok(as_member(world.teacher).get("/api/v1/payslips"))["results"]
    assert [(p["month"], p["net"]) for p in slips] == [("2026-07", "52700.00")]
    assert _ok(admin.post(f"/api/v1/payroll-runs/{run['id']}/paid"))["status"] == "paid"
    assert Notification.objects.filter(recipient=world.teacher, title="Payslip for July 2026").exists()
    assert as_member(world.teacher).get("/api/v1/payroll-runs").status_code == 403
    assert as_member(world.parent).get("/api/v1/payslips").status_code == 403


def test_recruitment(world, as_member):
    admin = as_member(world.admin)
    opening = _ok(
        admin.post(
            "/api/v1/job-openings",
            {"title": "Physics teacher", "department_id": str(world.department.pk)},
            format="json",
        ),
        201,
    )
    candidate = _ok(
        admin.post(
            "/api/v1/candidates",
            {
                "opening_id": opening["id"],
                "full_name": "Meera",
                "resume": SimpleUploadedFile("cv.pdf", b"%PDF-1.4 cv"),
            },
            format="multipart",
        ),
        201,
    )
    assert b"".join(admin.get(f"/api/v1/candidates/{candidate['id']}/resume")) == b"%PDF-1.4 cv"
    assert (
        _ok(admin.patch(f"/api/v1/candidates/{candidate['id']}", {"stage": "hired"}, format="json"))["stage"]
        == "hired"
    )
    _ok(admin.patch(f"/api/v1/candidates/{candidate['id']}", {"stage": "interview"}, format="json"), 409)
    _ok(admin.patch(f"/api/v1/job-openings/{opening['id']}", {"status": "closed"}, format="json"))
    _ok(
        admin.post("/api/v1/candidates", {"opening_id": opening["id"], "full_name": "Late"}, format="json"),
        400,
    )
    assert as_member(world.teacher).get("/api/v1/candidates").status_code == 403


HR_MATRIX = [
    ("get", "/api/v1/leave-types/{leave_type}", None),
    ("patch", "/api/v1/leave-types/{leave_type}", {"name": "x"}),
    ("get", "/api/v1/leave-requests/{leave}", None),
    ("post", "/api/v1/leave-requests/{leave}/cancel", None),
    ("get", "/api/v1/staff/{staff}/salary", None),
    ("put", "/api/v1/staff/{staff}/salary", {"basic": "1"}),
    ("get", "/api/v1/payroll-runs/{run}", None),
    ("post", "/api/v1/payroll-runs/{run}/process", None),
    ("post", "/api/v1/payroll-runs/{run}/paid", None),
    ("get", "/api/v1/payslips/{payslip}", None),
    ("get", "/api/v1/job-openings/{opening}", None),
    ("patch", "/api/v1/job-openings/{opening}", {"title": "x"}),
    ("get", "/api/v1/candidates/{candidate}", None),
    ("patch", "/api/v1/candidates/{candidate}", {"notes": "x"}),
    ("get", "/api/v1/candidates/{candidate}/resume", None),
]
HR_MATRIX_PATHS = {p for _, p, _ in HR_MATRIX}


@pytest.mark.parametrize(("method", "path", "body"), HR_MATRIX)
def test_another_schools_hr_records_answer_like_unknown(world, other_world, as_member, method, path, body):
    s = other_world.school
    staff = other_world.teacher_staff
    leave_type = LeaveType.objects.create(school=s, name="Sick", days_per_year=5)
    leave = LeaveRequest.objects.create(
        school=s,
        staff=staff,
        leave_type=leave_type,
        start_date="2026-07-20",
        end_date="2026-07-20",
        days=1,
        reason="x",
    )
    from eduflow.hr.models import Candidate, JobOpening, SalaryStructure

    SalaryStructure.objects.create(school=s, staff=staff, basic=1)
    run = PayrollRun.objects.create(school=s, month="2026-07-01")
    slip = Payslip.objects.create(
        school=s,
        run=run,
        staff=staff,
        basic=1,
        hra=0,
        allowances=0,
        pf=0,
        esi=0,
        tds=0,
        gross=1,
        deductions=0,
        net=1,
    )
    opening = JobOpening.objects.create(school=s, title="x")
    candidate = Candidate.objects.create(school=s, opening=opening, full_name="x")
    real = {
        "leave_type": leave_type.pk,
        "leave": leave.pk,
        "staff": staff.pk,
        "run": run.pk,
        "payslip": slip.pk,
        "opening": opening.pk,
        "candidate": candidate.pk,
    }
    missing = {k: uuid.uuid4() for k in real}
    client = as_member(world.admin)

    def call(values):
        url = path.format(**values)
        return getattr(client, method)(url, body, format="json") if body else getattr(client, method)(url)

    assert call(real).status_code == call(missing).status_code == 404
    assert PayrollRun.objects.get(pk=run.pk).status == "draft"
    assert Decimal(1) == SalaryStructure.objects.get().basic


def test_hr_is_under_rls(world, other_world):
    LeaveType.objects.create(school=other_world.school, name="Sick", days_per_year=5)
    with db_context.scoped(DbContext(school_id=world.school.pk)):
        assert not LeaveType.objects.exists()
