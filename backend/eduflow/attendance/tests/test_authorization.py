"""Attendance authorization: tenant isolation, role reach, RLS and the catalogue.

``ATTENDANCE_MATRIX_PATHS`` is part of the coverage check in ``tenancy/tests/test_isolation.py``, so a new
attendance endpoint with an object ID cannot ship without a row here.
"""

import uuid
from typing import Any

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from eduflow.attendance.models import AttendanceCorrection, AttendanceRecord, AttendanceSession
from eduflow.authz.catalog import SYSTEM_ROLES
from eduflow.core import db_context
from eduflow.core.db_context import DbContext

from .conftest import YESTERDAY, absent, take

pytestmark = pytest.mark.django_db

ATTENDANCE_MATRIX: list[tuple[str, str, dict[str, Any] | None]] = [
    ("get", "/api/v1/classes/{section}/roster", None),
    ("post", "/api/v1/classes/{section}/attendance", {"entries": []}),
    ("get", "/api/v1/students/{student}/attendance", None),
    ("get", "/api/v1/attendance/sessions/{session}", None),
    ("get", "/api/v1/attendance/records/{record}", None),
    ("get", "/api/v1/attendance/corrections/{correction}", None),
    ("post", "/api/v1/attendance/corrections/{correction}/approve", {}),
    ("post", "/api/v1/attendance/corrections/{correction}/decline", {}),
]
ATTENDANCE_MATRIX_PATHS = {path for _, path, _ in ATTENDANCE_MATRIX}
KINDS = ("section", "student", "session", "record", "correction")


@pytest.fixture
def school_b(other_world, school_day, api_as):
    """School B's yesterday register (Asha absent) and a pending correction of it."""
    take(api_as(other_world.admin), other_world.section_a, day=YESTERDAY, entries=absent(other_world.student))
    record = AttendanceRecord.objects.get(student=other_world.student)
    api_as(other_world.teacher).post(
        "attendance/corrections",
        {"record_id": str(record.pk), "new_status": "present", "reason": "Late bus"},
        expect=201,
    )
    return {
        "section": other_world.section_a.pk,
        "student": other_world.student.pk,
        "session": record.session_id,
        "record": record.pk,
        "correction": AttendanceCorrection.objects.get(record=record).pk,
    }


def _fill(value, ids):
    if isinstance(value, dict):
        return {k: _fill(v, ids) for k, v in value.items()}
    return value.format(**ids) if isinstance(value, str) else value


def _call(client, method, path, body):
    if body is None:
        return getattr(client, method)(path)
    return getattr(client, method)(path, body, format="json")


# ------------------------------------------------------------------------------------------------ isolation
@pytest.mark.parametrize(("method", "path", "body"), ATTENDANCE_MATRIX)
def test_school_a_admin_cannot_touch_school_b(world, school_b, as_member, method, path, body):
    client = as_member(world.admin)
    foreign = _call(client, method, _fill(path, school_b), _fill(body, school_b))
    missing_ids = {k: uuid.uuid4() for k in KINDS}
    missing = _call(client, method, _fill(path, missing_ids), _fill(body, missing_ids))
    assert foreign.status_code == missing.status_code == 404, foreign.content
    assert foreign.json()["error"]["message"] == missing.json()["error"]["message"]


def test_school_b_is_unchanged_after_the_matrix(world, school_b, as_member):
    client = as_member(world.admin)
    for method, path, body in ATTENDANCE_MATRIX:
        _call(client, method, _fill(path, school_b), _fill(body, school_b))
    assert AttendanceCorrection.objects.get(pk=school_b["correction"]).status == "pending"
    assert AttendanceRecord.objects.get(pk=school_b["record"]).status == "absent"
    assert AttendanceSession.objects.filter(section_id=school_b["section"]).count() == 1


def test_a_correction_of_another_schools_record_answers_like_an_unknown_one(world, school_b, as_member):
    client = as_member(world.admin)

    def request(record_id):
        body = {"record_id": str(record_id), "new_status": "late", "reason": "x"}
        return client.post("/api/v1/attendance/corrections", body, format="json")

    foreign, missing = request(school_b["record"]), request(uuid.uuid4())
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["error"]["message"] == missing.json()["error"]["message"]


@pytest.mark.parametrize("path", ["attendance/sessions", "attendance/records", "attendance/corrections"])
def test_lists_never_include_another_school(world, class_a, school_b, api_as, path):
    admin = api_as(world.admin)
    take(admin, world.section_a, day=YESTERDAY, entries=absent(class_a.bala))
    record = AttendanceRecord.objects.get(student=class_a.bala)
    admin.post(
        "attendance/corrections",
        {"record_id": str(record.pk), "new_status": "late", "reason": "x"},
        expect=201,
    )
    rows = admin.get(f"{path}?page_size=200")["results"]
    assert rows
    assert not {r["id"] for r in rows} & {str(v) for v in school_b.values()}


def test_platform_admin_has_no_implicit_access(world, make_user, client_for):
    staff = make_user(is_platform_admin=True)
    for path in ("attendance/sessions", "attendance/records", f"classes/{world.section_a.pk}/roster"):
        response = client_for(staff, world.school).get(f"/api/v1/{path}")
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "tenant_forbidden"


def test_attendance_tables_are_under_rls(world, other_world, school_b):
    tables = ["attendance_session", "attendance_record", "attendance_correction"]
    with db_context.scoped(DbContext()):
        assert not AttendanceSession.objects.exists()
        assert not AttendanceRecord.objects.exists()
        assert not AttendanceCorrection.objects.exists()
    with db_context.scoped(DbContext(school_id=world.school.pk)):
        assert AttendanceRecord.objects.filter(pk=school_b["record"]).update(status="present") == 0
    with db_context.scoped(DbContext(school_id=other_world.school.pk)):
        assert AttendanceRecord.objects.count() == 1
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM pg_policies WHERE tablename = ANY(%s)", [tables])
        assert cursor.fetchone()[0] == len(tables)


# ------------------------------------------------------------------------------------------------ roles
def test_record_reads_follow_data_scopes(world, class_a, school_day, api_as, make_member):
    admin = api_as(world.admin)
    take(admin, world.section_a, day=YESTERDAY, entries=absent(class_a.bala))
    take(admin, world.section_b, day=YESTERDAY)

    def students(member):
        return {r["student"]["full_name"] for r in api_as(member).get("attendance/records")["results"]}

    assert students(world.admin) == {"Asha", "Bala", "Chitra", "Ravi"}
    assert students(world.teacher) == {"Asha", "Bala", "Chitra"}  # the section they teach
    assert students(world.parent) == {"Asha"}  # their child
    assert students(world.student_member) == {"Asha"}  # themselves
    assert students(make_member(world.school, roles=["teacher"])) == set()
    ravi = AttendanceRecord.objects.get(student=world.other_student)
    api_as(world.parent).get(f"attendance/records/{ravi.pk}", expect=404)
    bala = AttendanceRecord.objects.get(student=class_a.bala)
    assert api_as(world.teacher).get(f"attendance/records/{bala.pk}")["status"] == "absent"
    filtered = admin.get(f"attendance/records?status=absent&date={YESTERDAY}")["results"]
    assert [r["student"]["full_name"] for r in filtered] == ["Bala"]


def test_registers_are_for_the_classes_staff_and_the_school(world, class_a, school_day, api_as):
    admin = api_as(world.admin)
    a = take(admin, world.section_a, day=YESTERDAY, entries=absent(class_a.bala))["session_id"]
    b = take(admin, world.section_b, day=YESTERDAY)["session_id"]

    def ids(member):
        return {r["id"] for r in api_as(member).get("attendance/sessions")["results"]}

    assert ids(world.admin) == {a, b}
    assert ids(world.teacher) == {a}
    assert ids(world.parent) == set()  # a whole-class view: parents read their children's records
    assert ids(world.student_member) == set()
    session = admin.get(f"attendance/sessions/{a}")
    assert session["counts"] == {"present": 2, "absent": 1, "late": 0, "half_day": 0, "excused": 0}
    assert (session["locked"], session["date"]) == (True, str(YESTERDAY))
    by_section = admin.get(f"attendance/sessions?section_id={world.section_b.pk}")["results"]
    assert [r["id"] for r in by_section] == [b]
    api_as(world.teacher).get(f"attendance/sessions/{b}", expect=404)


def test_other_school_roles_have_no_attendance_access(world, api_as, make_member):
    for role in (
        "accountant",
        "hr_manager",
        "librarian",
        "transport_manager",
        "hostel_manager",
        "staff",
        "driver",
    ):
        member = api_as(make_member(world.school, roles=[role]))
        member.get("attendance/records", expect=403)
        member.get("attendance/sessions", expect=403)


def test_writes_to_registers_and_records_have_no_routes(world, class_a, school_day, api_as):
    admin = api_as(world.admin)
    session = take(admin, world.section_a, day=YESTERDAY)["session_id"]
    record = AttendanceRecord.objects.filter(session_id=session).earliest("id")
    client = admin.client
    assert client.post("/api/v1/attendance/sessions", {}, format="json").status_code == 403
    assert client.delete(f"/api/v1/attendance/sessions/{session}").status_code == 403
    assert (
        client.patch(
            f"/api/v1/attendance/records/{record.pk}", {"status": "absent"}, format="json"
        ).status_code
        == 403
    )
    assert AttendanceRecord.objects.get(pk=record.pk).status == "present"


def test_client_supplied_roles_and_scopes_are_ignored(world, class_a, school_day, as_member):
    client = as_member(world.parent)
    response = client.get(
        f"/api/v1/students/{class_a.bala.pk}/attendance", HTTP_X_ROLE="school_admin", HTTP_X_SCOPE="school"
    )
    assert response.status_code == 404


# ------------------------------------------------------------------------------------------------ catalogue
def test_only_the_school_admin_and_principal_approve_corrections():
    holders = {key for key, (_, grants) in SYSTEM_ROLES.items() if "attendance.approve" in grants}
    assert holders == {"school_admin", "principal"}
    assert SYSTEM_ROLES["principal"][1]["attendance.approve"] == ("school",)
    teacher = SYSTEM_ROLES["teacher"][1]
    assert (teacher["attendance.create"], teacher["attendance.update"]) == (("section",), ("section",))


# ------------------------------------------------------------------------------------------------ queries
def test_roster_and_register_queries_do_not_grow_with_the_class(world, class_a, school_day, api_as):
    from eduflow.people.models import Enrollment, Student

    admin = api_as(world.admin)
    roster = f"classes/{world.section_a.pk}/roster"
    take(admin, world.section_a, client_id="a")
    admin.get(roster)
    with CaptureQueriesContext(connection) as small:
        admin.get(roster)
    for n in range(10):
        student = Student.objects.create(school=world.school, admission_number=f"Q-{n}", first_name=f"Q{n}")
        Enrollment.objects.create(
            school=world.school,
            student=student,
            academic_year=world.year,
            grade=world.grade,
            section=world.section_a,
            start_date=world.year.start_date,
        )
    with CaptureQueriesContext(connection) as large:
        assert len(admin.get(roster)["students"]) == 13
    assert len(large) == len(small)
