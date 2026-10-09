"""Academic structure: creation, validation, uniqueness, lifecycle and the school profile."""

import datetime

import pytest
from django.db import IntegrityError, transaction

from eduflow.academics.models import AcademicYear, Grade, Section
from eduflow.audit.models import AuditEvent

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin(world, as_member):
    return as_member(world.admin)


def _body(response):
    return response.json()


# ------------------------------------------------------------------------------------------------ school
def test_school_admin_updates_the_school_profile(world, admin):
    response = admin.patch(
        "/api/v1/school",
        {
            "legal_name": "Green Valley Trust School",
            "city": "Pune",
            "timezone": "Asia/Kolkata",
            "country": "IN",
        },
        format="json",
    )
    assert response.status_code == 200, response.content
    assert response.json()["city"] == "Pune"
    assert AuditEvent.objects.filter(action="tenancy.school.updated", school_id=world.school.id).exists()


def test_school_profile_validates_timezone_and_rejects_platform_fields(admin):
    assert admin.patch("/api/v1/school", {"timezone": "Mars/Olympus"}, format="json").status_code == 400
    assert admin.patch("/api/v1/school", {"code": "hijack"}, format="json").status_code == 400
    assert admin.patch("/api/v1/school", {"is_active": False}, format="json").status_code == 400


def test_teacher_cannot_update_the_school(world, as_member):
    assert as_member(world.teacher).patch("/api/v1/school", {"city": "X"}, format="json").status_code == 403


def test_platform_admin_edits_any_school_profile(world, make_user, client_for):
    staff = client_for(make_user(is_platform_admin=True))
    response = staff.patch(f"/api/v1/platform/schools/{world.school.id}", {"city": "Mumbai"}, format="json")
    assert response.status_code == 200
    assert response.json()["city"] == "Mumbai"


# ------------------------------------------------------------------------------------------------ years
def test_create_academic_year(admin):
    response = admin.post(
        "/api/v1/academic-years", {"name": "2027-28", "start_date": "2027-06-01", "end_date": "2028-03-31"}
    )
    assert response.status_code == 201, response.content
    assert response.json()["status"] == "planned"
    assert response.json()["is_current"] is False


@pytest.mark.parametrize(
    ("start", "end", "field"),
    [("2027-06-01", "2027-06-01", "end_date"), ("2027-06-01", "2027-01-01", "end_date")],
)
def test_year_dates_must_be_ordered(admin, start, end, field):
    response = admin.post("/api/v1/academic-years", {"name": "Bad", "start_date": start, "end_date": end})
    assert response.status_code == 400
    assert field in response.json()["error"]["fields"]


def test_years_cannot_overlap(admin):
    response = admin.post(
        "/api/v1/academic-years", {"name": "Overlap", "start_date": "2027-01-01", "end_date": "2027-12-31"}
    )
    assert response.status_code == 400
    assert "start_date" in response.json()["error"]["fields"]


def test_overlap_is_also_refused_by_the_database(world):
    with pytest.raises(IntegrityError), transaction.atomic():
        AcademicYear.objects.create(
            school=world.school,
            name="Sneaky",
            start_date=datetime.date(2026, 12, 1),
            end_date=datetime.date(2027, 12, 1),
        )


def test_other_schools_years_do_not_count_as_overlap(world, other_world):
    assert AcademicYear.objects.filter(start_date=world.year.start_date).count() == 2


def test_year_lifecycle_and_current_flag(world, admin):
    created = admin.post(
        "/api/v1/academic-years", {"name": "2027-28", "start_date": "2027-06-01", "end_date": "2028-03-31"}
    ).json()
    url = f"/api/v1/academic-years/{created['id']}"
    assert admin.patch(url, {"is_current": True}, format="json").status_code == 400  # planned
    assert admin.patch(url, {"status": "closed"}, format="json").status_code == 400  # skips active
    assert admin.patch(url, {"status": "active", "is_current": True}, format="json").status_code == 200
    world.year.refresh_from_db()
    assert world.year.is_current is False  # only one current year
    assert admin.patch(url, {"status": "closed"}, format="json").json()["is_current"] is False
    closed = admin.patch(url, {"name": "Renamed"}, format="json")
    assert closed.status_code == 409


def test_only_planned_years_can_be_deleted(world, admin):
    assert admin.delete(f"/api/v1/academic-years/{world.year.id}").status_code == 409
    planned = admin.post(
        "/api/v1/academic-years", {"name": "2030-31", "start_date": "2030-06-01", "end_date": "2031-03-31"}
    ).json()
    assert admin.delete(f"/api/v1/academic-years/{planned['id']}").status_code == 204
    assert AuditEvent.objects.filter(
        action="academics.academic_year.deleted", target_id=planned["id"]
    ).exists()


def test_year_filters(world, admin):
    admin.post(
        "/api/v1/academic-years", {"name": "2030-31", "start_date": "2030-06-01", "end_date": "2031-03-31"}
    )
    current = admin.get("/api/v1/academic-years?is_current=true").json()["results"]
    assert [y["id"] for y in current] == [str(world.year.id)]
    assert len(admin.get("/api/v1/academic-years?status=planned").json()["results"]) == 1
    assert admin.get("/api/v1/academic-years?status=galaxy").status_code == 400


# ------------------------------------------------------------------------------------------------ grades
def test_grade_crud_and_uniqueness(world, admin):
    created = admin.post("/api/v1/grades", {"name": "Class X", "code": "x", "display_order": 10})
    assert created.status_code == 201, created.content
    assert admin.post("/api/v1/grades", {"name": "Another", "code": "x"}).status_code == 409
    assert (
        admin.post("/api/v1/grades", {"name": "class x", "code": "x2"}).status_code == 409
    )  # case-insensitive
    url = f"/api/v1/grades/{created.json()['id']}"
    assert admin.patch(url, {"status": "archived"}, format="json").json()["status"] == "archived"
    assert admin.delete(url).status_code == 204


def test_grade_in_use_cannot_be_deleted(world, admin):
    assert admin.delete(f"/api/v1/grades/{world.grade.id}").status_code == 409


def test_grade_names_are_the_schools_own(world, other_world, admin):
    # The same code in another school is fine.
    assert admin.post("/api/v1/grades", {"name": "KG-1", "code": "kg1"}).status_code == 201
    assert Grade.objects.filter(code="g5").count() == 2


# ------------------------------------------------------------------------------------------------ sections
def _section(admin, world, **extra):
    body = {
        "academic_year_id": str(world.year.id),
        "grade_id": str(world.grade.id),
        "name": "C",
        "code": "5c",
    }
    return admin.post("/api/v1/sections", {**body, **extra}, format="json")


def test_create_section(world, admin):
    response = _section(admin, world, campus_id=str(world.campus.id), capacity=30)
    assert response.status_code == 201, response.content
    body = response.json()
    assert body["grade"] == {"id": str(world.grade.id), "name": "Grade 5", "code": "g5"}
    assert body["academic_year"]["id"] == str(world.year.id)
    assert body["campus"]["id"] == str(world.campus.id)


def test_section_uniqueness_within_year_and_grade(world, admin):
    assert _section(admin, world, code="5a").status_code == 409
    assert _section(admin, world, name="a", code="5z").status_code == 409


def test_section_rejects_other_school_references(world, other_world, admin):
    foreign = _section(admin, world, academic_year_id=str(other_world.year.id))
    unknown = _section(admin, world, academic_year_id="01900000-0000-7000-8000-000000000000")
    assert foreign.status_code == unknown.status_code == 400
    assert foreign.json()["error"]["fields"] == unknown.json()["error"]["fields"]
    assert _section(admin, world, grade_id=str(other_world.grade.id)).status_code == 400
    assert _section(admin, world, campus_id=str(other_world.campus.id)).status_code == 400


def test_section_and_year_school_mismatch_is_refused_by_the_database(world, other_world):
    with pytest.raises(IntegrityError), transaction.atomic():
        Section.objects.create(
            school=world.school, academic_year=other_world.year, grade=world.grade, name="X", code="x"
        )


def test_no_sections_in_a_closed_year(world, admin):
    AcademicYear.objects.filter(pk=world.year.pk).update(status="closed", is_current=False)
    assert _section(admin, world).status_code == 400


def test_section_capacity_cannot_drop_below_enrolled(world, admin):
    response = admin.patch(f"/api/v1/sections/{world.section_a.id}", {"capacity": 1}, format="json")
    assert response.status_code == 200  # one student enrolled
    world.section_a.refresh_from_db()
    assert world.section_a.capacity == 1
    from eduflow.people.models import Enrollment

    Enrollment.objects.create(
        school=world.school,
        student=world.other_student,
        academic_year=world.year,
        grade=world.grade,
        section=world.section_a,
        start_date=world.year.start_date,
        status="withdrawn",
        end_date=world.year.start_date,
    )
    assert (
        admin.patch(f"/api/v1/sections/{world.section_a.id}", {"capacity": 1}, format="json").status_code
        == 200
    )
    world.other_enrollment.delete()
    assert (
        admin.post(
            "/api/v1/enrollments",
            {"student_id": str(world.other_student.id), "section_id": str(world.section_a.id)},
        ).status_code
        == 409
    )  # full


def test_section_filters(world, admin):
    rows = admin.get(f"/api/v1/sections?grade_id={world.grade.id}&campus_id={world.campus.id}").json()[
        "results"
    ]
    assert [r["id"] for r in rows] == [str(world.section_a.id)]
    assert admin.get("/api/v1/sections?grade_id=nope").status_code == 400


# ------------------------------------------------------------------------------------------- subjects etc.
def test_subject_creation_and_uniqueness(world, admin):
    created = admin.post(
        "/api/v1/subjects",
        {"name": "Physics", "code": "phy", "category": "core", "department_id": str(world.department.id)},
        format="json",
    )
    assert created.status_code == 201
    assert created.json()["department"]["id"] == str(world.department.id)
    assert admin.post("/api/v1/subjects", {"name": "physics", "code": "phy2"}).status_code == 409
    filtered = admin.get(f"/api/v1/subjects?department_id={world.department.id}").json()["results"]
    assert {s["code"] for s in filtered} == {"maths", "phy"}


def test_subject_department_must_be_in_school(world, other_world, admin):
    response = admin.post(
        "/api/v1/subjects", {"name": "Bio", "code": "bio", "department_id": str(other_world.department.id)}
    )
    assert response.status_code == 400


def test_campus_and_department_crud(admin):
    campus = admin.post("/api/v1/campuses", {"name": "North", "code": "north", "city": "Pune"})
    assert campus.status_code == 201
    assert admin.post("/api/v1/campuses", {"name": "north", "code": "n2"}).status_code == 409
    dept = admin.post("/api/v1/departments", {"name": "Administration", "code": "admin"})
    assert dept.status_code == 201
    assert admin.delete(f"/api/v1/departments/{dept.json()['id']}").status_code == 204


def test_structure_changes_are_audited(world, admin):
    created = admin.post("/api/v1/grades", {"name": "Grade 6", "code": "g6"}).json()
    admin.patch(f"/api/v1/grades/{created['id']}", {"display_order": 6}, format="json")
    _section(admin, world)
    admin.post("/api/v1/subjects", {"name": "Art", "code": "art"})
    actions = set(AuditEvent.objects.filter(school_id=world.school.id).values_list("action", flat=True))
    assert {
        "academics.grade.created",
        "academics.grade.updated",
        "academics.section.created",
        "academics.subject.created",
    } <= actions
    update = AuditEvent.objects.get(action="academics.grade.updated")
    assert update.metadata == {"fields": ["display_order"]}
    assert update.actor_id == world.admin.user_id


def test_unknown_fields_are_rejected(admin):
    response = admin.post("/api/v1/grades", {"name": "G", "code": "g", "school_id": "x"})
    assert response.status_code == 400
    assert response.json()["error"]["fields"] == {"school_id": ["Unknown field."]}
