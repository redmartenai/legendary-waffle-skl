"""Phase 5 authorization: tenant isolation, role reach, RLS, and the permission catalogue.

``PHASE5_MATRIX_PATHS`` is part of the coverage check in ``tenancy/tests/test_isolation.py``, so a new
Phase 5 endpoint with an object ID cannot ship without a row here.
"""

import uuid

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from eduflow.authz.catalog import PERMISSIONS, SYSTEM_ROLES
from eduflow.authz.models import MembershipRole, Role
from eduflow.authz.services import _set_grants, bump_rbac_version
from eduflow.core import db_context
from eduflow.core.db_context import DbContext
from eduflow.timetable.models import Lesson, Period, Timetable, TimetableSlot

from .conftest import MONDAY, week

pytestmark = pytest.mark.django_db

PHASE5_MATRIX = [
    ("get", "/api/v1/terms/{term}", None),
    ("patch", "/api/v1/terms/{term}", {"name": "Hijacked"}),
    ("delete", "/api/v1/terms/{term}", None),
    ("get", "/api/v1/rooms/{room}", None),
    ("patch", "/api/v1/rooms/{room}", {"name": "Hijacked"}),
    ("delete", "/api/v1/rooms/{room}", None),
    ("get", "/api/v1/timetables/{timetable}", None),
    ("patch", "/api/v1/timetables/{timetable}", {"name": "Hijacked"}),
    ("delete", "/api/v1/timetables/{timetable}", None),
    ("post", "/api/v1/timetables/{timetable}/publish", {}),
    ("post", "/api/v1/timetables/{timetable}/archive", {}),
    ("post", "/api/v1/timetables/{timetable}/copy", {"name": "Stolen"}),
    ("get", "/api/v1/timetable-periods/{period}", None),
    ("patch", "/api/v1/timetable-periods/{period}", {"name": "Hijacked"}),
    ("delete", "/api/v1/timetable-periods/{period}", None),
    ("get", "/api/v1/timetable-slots/{slot}", None),
    ("patch", "/api/v1/timetable-slots/{slot}", {"weekday": 2}),
    ("delete", "/api/v1/timetable-slots/{slot}", None),
    ("get", "/api/v1/lessons/{lesson}", None),
    ("patch", "/api/v1/lessons/{lesson}", {"topic": "Hijacked"}),
    ("get", "/api/v1/staff/{staff}/schedule", None),
    ("get", "/api/v1/students/{student}/schedule", None),
    ("get", "/api/v1/sections/{section}/schedule", None),
]
PHASE5_MATRIX_PATHS = {path for _, path, _ in PHASE5_MATRIX}
KINDS = ("term", "room", "timetable", "period", "slot", "lesson", "staff", "student", "section")


def _build_school(world, api):
    """A published timetable with one slot and one lesson in ``world``'s school, built through its API."""
    term = api.post(
        "terms",
        {
            "academic_year_id": str(world.year.pk),
            "name": "Term 1",
            "code": "t1",
            "start_date": "2026-06-01",
            "end_date": "2026-09-30",
        },
    )
    room = api.post("rooms", {"name": "Room 1", "code": "r1"})
    timetable = api.post(
        "timetables", {"academic_year_id": str(world.year.pk), "name": "Main", "term_id": term["id"]}
    )
    period = api.post(
        "timetable-periods",
        {
            "timetable_id": timetable["id"],
            "number": 1,
            "name": "P1",
            "start_time": "09:00",
            "end_time": "09:45",
        },
    )
    slot = api.post(
        "timetable-slots",
        {
            "timetable_id": timetable["id"],
            "period_id": period["id"],
            "weekday": 1,
            "section_id": str(world.section_a.pk),
            "assignment_id": str(world.assignment.pk),
            "room_id": room["id"],
        },
    )
    api.post(f"timetables/{timetable['id']}/publish", expect=200)
    lesson = api.post("lessons", {"slot_id": slot["id"], "date": str(MONDAY), "topic": "Secret"})
    return {
        "term": term["id"],
        "room": room["id"],
        "timetable": timetable["id"],
        "period": period["id"],
        "slot": slot["id"],
        "lesson": lesson["id"],
        "staff": world.teacher_staff.pk,
        "student": world.student.pk,
        "section": world.section_a.pk,
    }


@pytest.fixture
def school_b(other_world, api_as):
    return _build_school(other_world, api_as(other_world.admin))


def _fill(value, ids):
    if isinstance(value, dict):
        return {k: _fill(v, ids) for k, v in value.items()}
    return value.format(**ids) if isinstance(value, str) else value


def _call(client, method, path, body):
    if body is None:
        return getattr(client, method)(path)
    return getattr(client, method)(path, body, format="json")


# ------------------------------------------------------------------------------------------------ isolation
@pytest.mark.parametrize(("method", "path", "body"), PHASE5_MATRIX)
def test_school_a_admin_cannot_touch_school_b(world, school_b, as_member, method, path, body):
    client = as_member(world.admin)
    foreign = _call(client, method, _fill(path, school_b), _fill(body, school_b))
    missing_ids = {k: uuid.uuid4() for k in KINDS}
    missing = _call(client, method, _fill(path, missing_ids), _fill(body, missing_ids))
    assert foreign.status_code == missing.status_code == 404, foreign.content
    assert foreign.json()["error"]["message"] == missing.json()["error"]["message"]


def test_school_b_is_unchanged_after_the_matrix(world, school_b, as_member):
    client = as_member(world.admin)
    for method, path, body in PHASE5_MATRIX:
        _call(client, method, _fill(path, school_b), _fill(body, school_b))
    assert Timetable.objects.get(pk=school_b["timetable"]).status == "published"
    assert Timetable.objects.filter(name="Stolen").count() == 0
    assert TimetableSlot.objects.get(pk=school_b["slot"]).weekday == 1
    assert Lesson.objects.get(pk=school_b["lesson"]).topic == "Secret"


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("timetables", {"academic_year_id": "{year}", "name": "X"}),
        (
            "timetable-periods",
            {
                "timetable_id": "{timetable}",
                "number": 5,
                "name": "P",
                "start_time": "12:00",
                "end_time": "12:45",
            },
        ),
        (
            "timetable-slots",
            {
                "timetable_id": "{timetable}",
                "period_id": "{period}",
                "weekday": 1,
                "section_id": "{section}",
                "assignment_id": "{assignment}",
            },
        ),
        ("lessons", {"slot_id": "{slot}", "date": "2026-07-20"}),
        (
            "terms",
            {
                "academic_year_id": "{year}",
                "name": "T",
                "code": "t",
                "start_date": "2026-06-01",
                "end_date": "2026-07-01",
            },
        ),
        ("rooms", {"name": "R", "code": "r", "campus_id": "{campus}"}),
    ],
)
def test_references_to_another_school_answer_like_unknown_ids(
    world, other_world, school_b, as_member, path, body
):
    ids = {
        **school_b,
        "year": other_world.year.pk,
        "assignment": other_world.assignment.pk,
        "campus": other_world.campus.pk,
    }
    client = as_member(world.admin)
    foreign = client.post(f"/api/v1/{path}", _fill(body, ids), format="json")
    missing_ids = {k: uuid.uuid4() for k in ids}
    missing = client.post(f"/api/v1/{path}", _fill(body, missing_ids), format="json")
    assert foreign.status_code == missing.status_code, foreign.content
    assert foreign.status_code in (400, 404)
    assert foreign.json()["error"].get("fields") == missing.json()["error"].get("fields")


@pytest.mark.parametrize(
    "path", ["terms", "rooms", "timetables", "timetable-periods", "timetable-slots", "lessons"]
)
def test_lists_never_include_another_school(world, school_b, as_member, api_as, path):
    _build_school(world, api_as(world.admin))
    rows = as_member(world.admin).get(f"/api/v1/{path}?page_size=200").json()["results"]
    assert rows
    assert not {r["id"] for r in rows} & {str(v) for v in school_b.values()}


def test_platform_admin_has_no_implicit_access(world, make_user, client_for):
    staff = make_user(is_platform_admin=True)
    for path in ("timetables", "lessons", "schedule/me"):
        response = client_for(staff, world.school).get(f"/api/v1/{path}")
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "tenant_forbidden"


def test_timetable_tables_are_under_rls(world, other_world, school_b):
    tables = (
        "timetable_timetable",
        "timetable_period",
        "timetable_slot",
        "timetable_lesson",
        "academics_term",
        "academics_room",
    )
    with db_context.scoped(DbContext()):
        assert not Timetable.objects.exists()
        assert not TimetableSlot.objects.exists()
        assert not Lesson.objects.exists()
    with db_context.scoped(DbContext(school_id=world.school.pk)):
        assert not Period.objects.exists()
        assert TimetableSlot.objects.filter(pk=school_b["slot"]).update(weekday=3) == 0
    with db_context.scoped(DbContext(school_id=other_world.school.pk)):
        assert Lesson.objects.count() == 1
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM pg_policies WHERE tablename = ANY(%s)", [list(tables)])
        assert cursor.fetchone()[0] == len(tables)


# ------------------------------------------------------------------------------------------------ roles
@pytest.mark.parametrize("role", ["teacher", "parent", "student_member"])
def test_only_the_school_builds_timetables(world, plan, api_as, role):
    member = api_as(getattr(world, role))
    member.post("timetables", {"academic_year_id": str(world.year.pk), "name": "Mine"}, expect=403)
    member.post(f"timetables/{plan.id}/publish", expect=403)
    member.post("terms", {}, expect=403)
    member.post("rooms", {}, expect=403)


def test_principal_builds_and_publishes(world, api_as):
    principal = api_as(world.principal)
    ids = _build_school(world, principal)
    assert principal.get(f"timetables/{ids['timetable']}")["status"] == "published"


def test_other_school_roles_have_no_timetable_or_lesson_access(world, api_as, make_member):
    for role in ("accountant", "hr_manager", "librarian", "transport_manager", "hostel_manager", "driver"):
        member = api_as(make_member(world.school, roles=[role]))
        member.get("timetable-slots", expect=403)
        member.get("lessons", expect=403)
        member.get("schedule/me", expect=403)
    member = api_as(make_member(world.school, roles=["staff"]))  # office staff read the timetable
    assert member.get("timetable-slots")["results"] == []


def test_slot_reads_follow_data_scopes(world, plan, api_as, second_teacher):
    in_a = plan.slot(1, 1)["id"]
    in_b = plan.slot(3, 1, section=world.section_b, assignment=second_teacher.maths_b)["id"]
    plan.publish()

    def ids(member):
        return {row["id"] for row in api_as(member).get("timetable-slots")["results"]}

    assert ids(world.teacher) == {in_a, in_b}  # teachers read the whole timetable (Phase 2 default: school)
    assert ids(world.student_member) == {in_a}
    assert ids(world.parent) == {in_a}
    api_as(world.parent).get(f"timetable-slots/{in_b}", expect=404)
    assert len(api_as(world.parent).get("timetables")["results"]) == 1  # the plan itself: no personal data
    assert len(api_as(world.parent).get("timetable-periods")["results"]) == 3


def test_schedules_need_both_permissions_over_the_subject(world, plan, api_as, make_member):
    plan.slot(1, 1)
    plan.publish()
    clerk = make_member(world.school, roles=[])
    role = Role.objects.create(school=world.school, key="clerk", name="Clerk")
    _set_grants(role, {"student.read": ["school"], "timetable.read": ["self"]})
    MembershipRole.objects.create(school=world.school, membership=clerk, role=role)
    bump_rbac_version(world.school.pk)
    # The clerk may read every student, but timetables only for themselves: not this student's schedule.
    api_as(clerk).get(f"students/{world.student.pk}/schedule", expect=404)
    assert week(api_as(world.student_member), f"students/{world.student.pk}/schedule")


def test_client_supplied_roles_and_scopes_are_ignored(world, plan, as_member):
    plan.slot(1, 1)
    plan.publish()
    client = as_member(world.parent)
    response = client.get(
        f"/api/v1/students/{world.other_student.pk}/schedule",
        HTTP_X_ROLE="school_admin",
        HTTP_X_SCOPE="school",
    )
    assert response.status_code == 404


# ------------------------------------------------------------------------------------------------ catalogue
def test_phase5_permissions_and_default_grants():
    for codename in (
        "term.read",
        "term.manage",
        "room.read",
        "room.manage",
        "timetable.manage",
        "lesson.read",
        "lesson.manage",
    ):
        assert codename in PERMISSIONS
    grants = {key: dict(spec[1]) for key, spec in SYSTEM_ROLES.items()}
    assert grants["teacher"]["lesson.manage"] == ("self",)
    assert "timetable.manage" not in grants["teacher"]
    assert grants["parent"]["lesson.read"] == ("child",)
    assert grants["student"]["lesson.read"] == ("self",)
    assert "lesson.manage" not in grants["parent"]
    assert "lesson.manage" not in grants["student"]
    for perms in grants.values():  # terms and rooms are school structure: every role reads them
        assert perms["term.read"] == perms["room.read"] == ("school",)
    assert "role.delete" not in grants["principal"]
    assert grants["principal"]["timetable.manage"] == ("school",)


# ------------------------------------------------------------------------------------------------ queries
def test_slot_list_query_count_is_flat(world, plan, admin_api, second_teacher):
    plan.slot(1, 1)
    admin_api.get("timetable-slots")
    with CaptureQueriesContext(connection) as small:
        admin_api.get("timetable-slots")
    for weekday in range(2, 7):
        plan.slot(1, weekday)
        plan.slot(3, weekday, assignment=second_teacher.science_a)
    with CaptureQueriesContext(connection) as large:
        assert len(admin_api.get("timetable-slots")["results"]) == 11
    assert len(large) == len(small)
