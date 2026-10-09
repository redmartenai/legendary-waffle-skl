"""Phase 3 tenant isolation matrix: school A's admin against every school B object (ADR-003, ADR-013).

Every request must answer exactly like a random, non-existent ID. ``PHASE3_MATRIX_PATHS`` is part of the
coverage check in ``tenancy/tests/test_isolation.py``, so a new endpoint cannot ship without a row here.
"""

import uuid

import pytest

pytestmark = pytest.mark.django_db

# (method, path, body). {x} is replaced by school B's object of that kind (or a random UUID).
PHASE3_MATRIX = [
    ("get", "/api/v1/campuses/{campus}", None),
    ("patch", "/api/v1/campuses/{campus}", {"name": "Hijacked"}),
    ("delete", "/api/v1/campuses/{campus}", None),
    ("get", "/api/v1/academic-years/{year}", None),
    ("patch", "/api/v1/academic-years/{year}", {"name": "Hijacked"}),
    ("delete", "/api/v1/academic-years/{year}", None),
    ("get", "/api/v1/departments/{department}", None),
    ("patch", "/api/v1/departments/{department}", {"name": "Hijacked"}),
    ("delete", "/api/v1/departments/{department}", None),
    ("get", "/api/v1/grades/{grade}", None),
    ("patch", "/api/v1/grades/{grade}", {"name": "Hijacked"}),
    ("delete", "/api/v1/grades/{grade}", None),
    ("get", "/api/v1/sections/{section}", None),
    ("patch", "/api/v1/sections/{section}", {"name": "Hijacked"}),
    ("delete", "/api/v1/sections/{section}", None),
    ("get", "/api/v1/subjects/{subject}", None),
    ("patch", "/api/v1/subjects/{subject}", {"name": "Hijacked"}),
    ("delete", "/api/v1/subjects/{subject}", None),
    ("get", "/api/v1/staff/{staff}", None),
    ("patch", "/api/v1/staff/{staff}", {"designation": "Hijacked"}),
    ("get", "/api/v1/students/{student}", None),
    ("patch", "/api/v1/students/{student}", {"first_name": "Hijacked"}),
    ("get", "/api/v1/guardians/{guardian}", None),
    ("patch", "/api/v1/guardians/{guardian}", {"full_name": "Hijacked"}),
    ("get", "/api/v1/student-guardians/{link}", None),
    ("patch", "/api/v1/student-guardians/{link}", {"is_primary": False}),
    ("delete", "/api/v1/student-guardians/{link}", None),
    ("get", "/api/v1/enrollments/{enrollment}", None),
    ("patch", "/api/v1/enrollments/{enrollment}", {"roll_number": "9"}),
    ("post", "/api/v1/enrollments/{enrollment}/end", {"status": "withdrawn"}),
    ("post", "/api/v1/enrollments/{enrollment}/transfer", {"section_id": "{section}"}),
    ("get", "/api/v1/teacher-assignments/{assignment}", None),
    ("patch", "/api/v1/teacher-assignments/{assignment}", {"status": "ended"}),
    ("delete", "/api/v1/teacher-assignments/{assignment}", None),
]
PHASE3_MATRIX_PATHS = {path for _, path, _ in PHASE3_MATRIX}

KINDS = (
    "campus",
    "year",
    "department",
    "grade",
    "section",
    "subject",
    "staff",
    "student",
    "guardian",
    "link",
    "enrollment",
    "assignment",
)


def _ids(w):
    return {
        "campus": w.campus.id,
        "year": w.year.id,
        "department": w.department.id,
        "grade": w.grade.id,
        "section": w.section_b.id,
        "subject": w.subject.id,
        "staff": w.teacher_staff.id,
        "student": w.student.id,
        "guardian": w.guardian.id,
        "link": w.link.id,
        "enrollment": w.enrollment.id,
        "assignment": w.assignment.id,
    }


def _fill(value, ids):
    if isinstance(value, dict):
        return {k: _fill(v, ids) for k, v in value.items()}
    return value.format(**ids) if isinstance(value, str) else value


def _call(client, method, path, body):
    if body is None:
        return getattr(client, method)(path)
    return getattr(client, method)(path, body, format="json")


@pytest.mark.parametrize(("method", "path", "body"), PHASE3_MATRIX)
def test_school_a_admin_cannot_touch_school_b(world, other_world, as_member, method, path, body):
    client = as_member(world.admin)
    foreign = _call(client, method, _fill(path, _ids(other_world)), _fill(body, _ids(other_world)))
    missing_ids = {k: uuid.uuid4() for k in KINDS}
    missing = _call(client, method, _fill(path, missing_ids), _fill(body, missing_ids))
    assert foreign.status_code == missing.status_code == 404, foreign.content
    assert foreign.json()["error"] == {
        **missing.json()["error"],
        "request_id": foreign.json()["error"]["request_id"],
    }


def test_school_b_is_unchanged_after_the_matrix(world, other_world, as_member):
    client = as_member(world.admin)
    for method, path, body in PHASE3_MATRIX:
        _call(client, method, _fill(path, _ids(other_world)), _fill(body, _ids(other_world)))
    other_world.student.refresh_from_db()
    other_world.enrollment.refresh_from_db()
    other_world.assignment.refresh_from_db()
    assert other_world.student.first_name == "Asha"
    assert other_world.enrollment.status == "active"
    assert other_world.assignment.status == "active"
    from eduflow.academics.models import Campus

    assert Campus.objects.filter(pk=other_world.campus.pk).exists()


LISTS = [
    "/api/v1/campuses",
    "/api/v1/academic-years",
    "/api/v1/departments",
    "/api/v1/grades",
    "/api/v1/sections",
    "/api/v1/subjects",
    "/api/v1/staff",
    "/api/v1/students",
    "/api/v1/guardians",
    "/api/v1/student-guardians",
    "/api/v1/enrollments",
    "/api/v1/teacher-assignments",
]


@pytest.mark.parametrize("path", LISTS)
def test_lists_never_include_another_school(world, other_world, as_member, path):
    rows = as_member(world.admin).get(path + "?page_size=200").json()["results"]
    foreign = {str(v) for v in _ids(other_world).values()} | {
        str(other_world.section_a.id),
        str(other_world.other_student.id),
        str(other_world.other_enrollment.id),
    }
    assert rows
    assert not {r["id"] for r in rows} & foreign


def test_filtering_by_another_schools_id_returns_nothing(world, other_world, as_member):
    client = as_member(world.admin)
    assert client.get(f"/api/v1/sections?academic_year_id={other_world.year.id}").json()["results"] == []
    assert client.get(f"/api/v1/students?section_id={other_world.section_a.id}").json()["results"] == []


def test_switching_header_to_school_b_is_forbidden(world, other_world, client_for):
    client = client_for(world.admin.user, other_world.school)
    assert client.get("/api/v1/students").status_code == 403
