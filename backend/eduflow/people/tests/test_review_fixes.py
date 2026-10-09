"""Regression tests for the Phase 3 security review findings."""

import datetime

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from eduflow.academics.models import Department
from eduflow.audit.models import AuditEvent
from eduflow.authz.models import MembershipRole, Role
from eduflow.authz.services import _set_grants, bump_rbac_version
from eduflow.people.models import Enrollment, Guardian

pytestmark = pytest.mark.django_db


def _ids(response):
    assert response.status_code == 200, response.content
    return {row["id"] for row in response.json()["results"]}


def _custom_role(world, member, grants):
    role = Role.objects.create(
        school=world.school, key=f"custom-{len(grants)}-{member.pk.hex[:6]}", name="Custom"
    )
    _set_grants(role, grants)
    MembershipRole.objects.create(school=world.school, membership=member, role=role)
    bump_rbac_version(world.school.pk)


# ------------------------------------------------------------------------------------------------ finding 1
def test_closing_a_year_ends_enrollments_and_assignments(world, as_member):
    admin = as_member(world.admin)
    teacher = as_member(world.teacher)
    assert _ids(teacher.get("/api/v1/students")) == {str(world.student.id)}

    response = admin.patch(f"/api/v1/academic-years/{world.year.id}", {"status": "closed"}, format="json")

    assert response.status_code == 200
    world.enrollment.refresh_from_db()
    world.assignment.refresh_from_db()
    assert world.enrollment.status == "completed"
    assert world.enrollment.end_date <= world.year.end_date
    assert world.assignment.status == "ended"
    # The teacher no longer reaches the former students, their guardians or their enrollments.
    assert _ids(teacher.get("/api/v1/students")) == set()
    assert _ids(teacher.get("/api/v1/guardians")) == set()
    assert _ids(teacher.get("/api/v1/enrollments")) == set()
    assert AuditEvent.objects.filter(
        action="people.teacher_assignment.ended", metadata__reason="year_closed"
    ).exists()


def test_closed_year_records_are_read_only(world, as_member):
    admin = as_member(world.admin)
    world.year.status = "closed"
    world.year.is_current = False
    world.year.save()
    assert (
        admin.post(f"/api/v1/enrollments/{world.enrollment.id}/end", {"status": "withdrawn"}).status_code
        == 409
    )
    assert (
        admin.patch(
            f"/api/v1/enrollments/{world.enrollment.id}", {"roll_number": "3"}, format="json"
        ).status_code
        == 409
    )
    url = f"/api/v1/teacher-assignments/{world.assignment.id}"
    assert admin.patch(url, {"is_class_teacher": True}, format="json").status_code == 409
    assert admin.delete(url).status_code == 409
    assert admin.delete(f"/api/v1/sections/{world.section_b.id}").status_code == 409


# ------------------------------------------------------------------------------------------------ finding 2
def test_scoped_write_grants_do_not_authorise_school_wide_creates(world, as_member):
    _custom_role(world, world.teacher, {"enrollment.manage": ["section"], "guardian.manage": ["section"]})
    teacher = as_member(world.teacher)
    enroll = teacher.post(
        "/api/v1/enrollments",
        {"student_id": str(world.other_student.id), "section_id": str(world.section_a.id)},
    )
    assert enroll.status_code == 403
    assert teacher.post("/api/v1/guardians", {"full_name": "X"}).status_code == 403
    assert (
        teacher.post(f"/api/v1/enrollments/{world.enrollment.id}/end", {"status": "withdrawn"}).status_code
        == 403
    )


def _actor(membership):
    from eduflow.authz.grants import Actor, compute_grants

    return Actor(
        user=membership.user,
        school=membership.school,
        membership=membership,
        grants=compute_grants(membership),
    )


def test_student_and_guardian_apis_cannot_link_accounts(world, as_member, make_member):
    """ADR-025: only an accepted invitation links an account to a student or guardian record."""
    admin = as_member(world.admin)
    target = make_member(world.school, roles=["parent"])
    guardian = admin.post("/api/v1/guardians", {"full_name": "G", "membership_id": str(target.id)})
    student = admin.patch(
        f"/api/v1/students/{world.other_student.id}", {"membership_id": str(target.id)}, format="json"
    )
    assert guardian.status_code == student.status_code == 400
    assert guardian.json()["error"]["fields"] == {"membership_id": ["Unknown field."]}
    assert not Guardian.objects.filter(membership=target).exists()


def test_nobody_links_their_own_account_to_a_profile(world):
    from rest_framework.exceptions import ValidationError

    from eduflow.people.services import link_guardian_account

    unlinked = Guardian.objects.create(school=world.school, full_name="Unlinked")
    with pytest.raises(ValidationError):
        link_guardian_account(_actor(world.admin), unlinked, world.admin)
    unlinked.refresh_from_db()
    assert unlinked.membership_id is None


def test_linking_accounts_needs_school_wide_member_administration(world, make_member):
    from rest_framework.exceptions import PermissionDenied

    from eduflow.people.services import link_guardian_account, link_student_account

    clerk = make_member(world.school, roles=[])
    _custom_role(world, clerk, {"guardian.manage": ["school"], "student.update": ["school"]})
    target = make_member(world.school, roles=["parent"])
    unlinked = Guardian.objects.create(school=world.school, full_name="Unlinked")
    with pytest.raises(PermissionDenied):
        link_guardian_account(_actor(clerk), unlinked, target)
    with pytest.raises(PermissionDenied):
        link_student_account(_actor(clerk), world.other_student, target)


# ------------------------------------------------------------------------------------------------ finding 3
@pytest.mark.parametrize(("status", "expected"), [("left", "withdrawn"), ("graduated", "completed")])
def test_student_leaving_ends_their_enrollment(world, as_member, status, expected):
    admin = as_member(world.admin)
    response = admin.patch(f"/api/v1/students/{world.student.id}", {"status": status}, format="json")
    assert response.status_code == 200
    world.enrollment.refresh_from_db()
    assert world.enrollment.status == expected
    assert _ids(as_member(world.teacher).get("/api/v1/students")) == set()


# ------------------------------------------------------------------------------------------------ finding 4
def test_teacher_moved_to_non_teaching_loses_classes(world, as_member):
    admin = as_member(world.admin)
    response = admin.patch(
        f"/api/v1/staff/{world.teacher_staff.id}", {"staff_type": "non_teaching"}, format="json"
    )
    assert response.status_code == 200
    world.assignment.refresh_from_db()
    assert world.assignment.status == "ended"
    event = AuditEvent.objects.get(action="people.teacher_assignment.ended")
    assert event.target_id == str(world.assignment.id)


def test_teacher_on_leave_keeps_classes(world, as_member):
    as_member(world.admin).patch(
        f"/api/v1/staff/{world.teacher_staff.id}", {"status": "on_leave"}, format="json"
    )
    world.assignment.refresh_from_db()
    assert world.assignment.status == "active"


# ------------------------------------------------------------------------------------------------ finding 5
def test_capacity_change_locks_the_section(world, as_member):
    with CaptureQueriesContext(connection) as ctx:
        response = as_member(world.admin).patch(
            f"/api/v1/sections/{world.section_a.id}", {"capacity": 5}, format="json"
        )
    assert response.status_code == 200
    assert any(
        'FROM "academics_section"' in q["sql"] and "FOR UPDATE" in q["sql"] for q in ctx.captured_queries
    )


# ------------------------------------------------------------------------------------------------ finding 6
def test_archived_references_are_rejected(world, as_member):
    admin = as_member(world.admin)
    world.section_b.status = "archived"
    world.section_b.save()
    response = admin.post(
        "/api/v1/teacher-assignments",
        {
            "staff_id": str(world.teacher_staff.id),
            "section_id": str(world.section_b.id),
            "subject_id": str(world.subject.id),
        },
        format="json",
    )
    assert response.status_code == 400
    old = Department.objects.create(school=world.school, name="Old", code="old", status="archived")
    patch = admin.patch(
        f"/api/v1/staff/{world.teacher_staff.id}", {"department_id": str(old.id)}, format="json"
    )
    assert patch.status_code == 400


# ------------------------------------------------------------------------------------------------ finding 7
def test_enrollment_dates_stay_within_the_academic_year(world, as_member):
    admin = as_member(world.admin)
    Enrollment.objects.filter(pk=world.other_enrollment.pk).delete()
    outside = admin.post(
        "/api/v1/enrollments",
        {
            "student_id": str(world.other_student.id),
            "section_id": str(world.section_b.id),
            "start_date": "2025-01-01",
        },
    )
    assert outside.status_code == 400
    late_end = admin.post(
        f"/api/v1/enrollments/{world.enrollment.id}/end", {"status": "completed", "end_date": "2030-01-01"}
    )
    assert late_end.status_code == 400
    late_transfer = admin.post(
        f"/api/v1/enrollments/{world.enrollment.id}/transfer",
        {"section_id": str(world.section_b.id), "date": "2030-01-01"},
    )
    assert late_transfer.status_code == 400
    assert world.year.start_date == datetime.date(2026, 6, 1)
