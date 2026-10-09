"""Phase 3 authorization: roles, data scopes and denials over the real domain models.

Scopes come from Phase 2 grants (``authz.catalog``) and the rules in ``people.policies``; no view
hard-codes a role.
"""

import pytest

pytestmark = pytest.mark.django_db


def _ids(response):
    assert response.status_code == 200, response.content
    return {row["id"] for row in response.json()["results"]}


# --------------------------------------------------------------------------------------- school-wide roles
@pytest.mark.parametrize("role", ["admin", "principal"])
def test_school_wide_roles_see_everything_in_their_school(world, other_world, as_member, role):
    client = as_member(getattr(world, role))
    assert _ids(client.get("/api/v1/students")) == {str(world.student.id), str(world.other_student.id)}
    assert len(_ids(client.get("/api/v1/sections"))) == 2
    assert len(_ids(client.get("/api/v1/enrollments"))) == 2
    assert len(_ids(client.get("/api/v1/guardians"))) == 1


def test_principal_can_manage_structure_and_people(world, as_member):
    client = as_member(world.principal)
    assert client.post("/api/v1/grades", {"name": "Grade 7", "code": "g7"}).status_code == 201
    assert client.post("/api/v1/students", {"admission_number": "P-1", "first_name": "X"}).status_code == 201


def test_platform_admin_has_no_implicit_access_to_school_domain(world, make_user, client_for):
    staff = make_user(is_platform_admin=True)
    response = client_for(staff, world.school).get("/api/v1/students")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "tenant_forbidden"


# ------------------------------------------------------------------------------------------------ teacher
def test_teacher_sees_only_assigned_sections_and_their_students(world, as_member):
    client = as_member(world.teacher)
    assert _ids(client.get("/api/v1/sections")) == {str(world.section_a.id)}
    assert _ids(client.get("/api/v1/students")) == {str(world.student.id)}
    assert _ids(client.get("/api/v1/enrollments")) == {str(world.enrollment.id)}
    assert _ids(client.get("/api/v1/guardians")) == {str(world.guardian.id)}
    assert client.get(f"/api/v1/students/{world.other_student.id}").status_code == 404
    assert client.get(f"/api/v1/sections/{world.section_b.id}").status_code == 404


def test_teacher_scope_follows_assignment_status(world, as_member):
    world.assignment.status = "ended"
    world.assignment.save()
    client = as_member(world.teacher)
    assert _ids(client.get("/api/v1/students")) == set()
    assert _ids(client.get("/api/v1/sections")) == set()


def test_teacher_sees_own_assignments_and_co_teachers(world, as_member, make_member):
    from eduflow.people.models import StaffProfile, TeacherAssignment

    other = StaffProfile.objects.create(
        school=world.school, membership=make_member(world.school), employee_id="T-3"
    )
    co = TeacherAssignment.objects.create(
        school=world.school,
        staff=other,
        academic_year=world.year,
        section=world.section_a,
        is_class_teacher=True,
    )
    elsewhere = TeacherAssignment.objects.create(
        school=world.school,
        staff=other,
        academic_year=world.year,
        section=world.section_b,
        subject=world.subject,
    )
    ids = _ids(as_member(world.teacher).get("/api/v1/teacher-assignments"))
    assert ids == {str(world.assignment.id), str(co.id)}
    assert str(elsewhere.id) not in ids


def test_teacher_sees_only_own_staff_record(world, as_member):
    assert _ids(as_member(world.teacher).get("/api/v1/staff")) == {str(world.teacher_staff.id)}


def test_teacher_cannot_write(world, as_member):
    client = as_member(world.teacher)
    assert client.post("/api/v1/students", {"admission_number": "T", "first_name": "T"}).status_code == 403
    assert (
        client.patch(f"/api/v1/students/{world.student.id}", {"first_name": "X"}, format="json").status_code
        == 403
    )
    assert (
        client.post(f"/api/v1/enrollments/{world.enrollment.id}/end", {"status": "completed"}).status_code
        == 403
    )
    assert client.post("/api/v1/sections", {}).status_code == 403
    assert client.get("/api/v1/academic-years").status_code == 200  # structure is readable


def test_filters_cannot_widen_the_scope(world, as_member):
    client = as_member(world.teacher)
    assert _ids(client.get(f"/api/v1/students?section_id={world.section_b.id}")) == set()
    assert _ids(client.get(f"/api/v1/enrollments?student_id={world.other_student.id}")) == set()


# ------------------------------------------------------------------------------------------------ parent
def test_parent_sees_only_own_children(world, as_member):
    client = as_member(world.parent)
    assert _ids(client.get("/api/v1/students")) == {str(world.student.id)}
    assert _ids(client.get("/api/v1/enrollments")) == {str(world.enrollment.id)}
    assert _ids(client.get("/api/v1/sections")) == {str(world.section_a.id)}
    assert _ids(client.get("/api/v1/teacher-assignments")) == {str(world.assignment.id)}
    assert _ids(client.get("/api/v1/guardians")) == {str(world.guardian.id)}
    assert client.get(f"/api/v1/students/{world.other_student.id}").status_code == 404
    assert client.get("/api/v1/staff").status_code == 403


def test_parent_with_two_children(world, as_member):
    from eduflow.people.models import StudentGuardian

    StudentGuardian.objects.create(
        school=world.school, student=world.other_student, guardian=world.guardian, relationship="mother"
    )
    assert len(_ids(as_member(world.parent).get("/api/v1/students"))) == 2


# ------------------------------------------------------------------------------------------------ student
def test_student_sees_only_self(world, as_member):
    client = as_member(world.student_member)
    assert _ids(client.get("/api/v1/students")) == {str(world.student.id)}
    assert _ids(client.get("/api/v1/enrollments")) == {str(world.enrollment.id)}
    assert _ids(client.get("/api/v1/sections")) == {str(world.section_a.id)}
    assert _ids(client.get("/api/v1/teacher-assignments")) == {str(world.assignment.id)}
    assert _ids(client.get("/api/v1/guardians")) == {str(world.guardian.id)}
    assert client.get(f"/api/v1/students/{world.other_student.id}").status_code == 404
    assert client.get("/api/v1/student-guardians").status_code == 200


# ------------------------------------------------------------------------------------------------ other roles
def test_accountant_reads_school_wide_but_cannot_change_structure(world, as_member, make_member):
    accountant = make_member(world.school, roles=["accountant"])
    client = as_member(accountant)
    assert len(_ids(client.get("/api/v1/students"))) == 2
    assert client.post("/api/v1/grades", {"name": "G", "code": "g"}).status_code == 403
    assert client.get("/api/v1/staff").status_code == 403


def test_hr_manager_creates_staff(world, as_member, make_member):
    hr = as_member(make_member(world.school, roles=["hr_manager"]))
    member = make_member(world.school, roles=["staff"])
    response = hr.post(
        "/api/v1/staff", {"membership_id": str(member.id), "employee_id": "N-1", "staff_type": "non_teaching"}
    )
    assert response.status_code == 201
    assert hr.get("/api/v1/students").status_code == 403


def test_campus_scope_for_custom_role(world, as_member, make_member):
    from eduflow.authz.models import MembershipRole, Role
    from eduflow.authz.services import _set_grants, bump_rbac_version
    from eduflow.people.models import StaffProfile

    member = make_member(world.school, roles=[])
    StaffProfile.objects.create(
        school=world.school, membership=member, employee_id="C-9", campus=world.campus
    )
    role = Role.objects.create(school=world.school, key="campus-head", name="Campus head")
    _set_grants(role, {"section.read": ["campus"], "student.read": ["campus"]})
    MembershipRole.objects.create(school=world.school, membership=member, role=role)
    bump_rbac_version(world.school.pk)
    client = as_member(member)
    assert _ids(client.get("/api/v1/sections")) == {str(world.section_a.id)}  # section B has no campus
    assert _ids(client.get("/api/v1/students")) == {str(world.student.id)}


def test_undeclared_methods_are_refused(world, as_member):
    """Students and enrollments are never hard-deleted through the API: the method has no permission."""
    from eduflow.people.models import Enrollment, Student

    client = as_member(world.admin)
    assert client.delete(f"/api/v1/students/{world.student.id}").status_code == 403
    assert client.delete(f"/api/v1/enrollments/{world.enrollment.id}").status_code == 403
    assert Student.objects.filter(pk=world.student.pk).exists()
    assert Enrollment.objects.filter(pk=world.enrollment.pk).exists()
