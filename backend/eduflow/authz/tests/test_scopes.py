"""Data scopes (ADR-004): school, section (teacher), assigned, child (parent), self (student), own and
department.

The toy models in ``scopeapp`` stand in for the future academic modules.
"""

import pytest
from rest_framework.exceptions import NotFound, PermissionDenied

from eduflow.authz.grants import Actor, compute_grants
from eduflow.authz.models import MembershipRole
from eduflow.authz.scopes import ScopedResource
from eduflow.authz.tests.scopeapp.models import Guardianship, Mentorship, Section, Student, TeacherSection
from eduflow.authz.tests.scopeapp.resources import students

pytestmark = pytest.mark.django_db


def _actor(membership):
    return Actor(
        user=membership.user,
        school=membership.school,
        membership=membership,
        grants=compute_grants(membership),
    )


@pytest.fixture
def world(make_school, make_member, make_user):
    a, b = make_school("scope-a"), make_school("scope-b")
    s1 = Section.objects.create(school=a, name="9A", department="science")
    s2 = Section.objects.create(school=a, name="9B", department="arts")
    sb = Section.objects.create(school=b, name="9A")
    student_user = make_user()
    alice = Student.objects.create(school=a, section=s1, name="Alice", user=student_user)
    bob = Student.objects.create(school=a, section=s1, name="Bob")
    carol = Student.objects.create(school=a, section=s2, name="Carol")
    other = Student.objects.create(school=b, section=sb, name="Other school")
    return {
        "a": a,
        "b": b,
        "s1": s1,
        "s2": s2,
        "alice": alice,
        "bob": bob,
        "carol": carol,
        "other": other,
        "student_user": student_user,
        "make_member": make_member,
    }


def _names(qs):
    return sorted(qs.values_list("name", flat=True))


def test_school_scope_sees_the_whole_school_and_nothing_else(world):
    principal = world["make_member"](world["a"], roles=["principal"])
    assert _names(students.queryset(_actor(principal), "student.read")) == ["Alice", "Bob", "Carol"]


def test_teacher_sees_only_assigned_sections(world):
    teacher = world["make_member"](world["a"], roles=["teacher"])
    TeacherSection.objects.create(school=world["a"], membership=teacher, section=world["s1"])
    actor = _actor(teacher)
    assert _names(students.queryset(actor, "student.read")) == ["Alice", "Bob"]
    assert students.get(actor, "student.read", world["alice"].pk) == world["alice"]
    with pytest.raises(NotFound):
        students.get(actor, "student.read", world["carol"].pk)


def test_teacher_with_no_assignments_sees_nothing(world):
    teacher = world["make_member"](world["a"], roles=["teacher"])
    assert _names(students.queryset(_actor(teacher), "student.read")) == []


def test_assigned_scope_adds_individually_assigned_students(world):
    teacher = world["make_member"](world["a"], roles=["teacher"])
    TeacherSection.objects.create(school=world["a"], membership=teacher, section=world["s1"])
    Mentorship.objects.create(school=world["a"], membership=teacher, student=world["carol"])
    assert _names(students.queryset(_actor(teacher), "student.read")) == ["Alice", "Bob", "Carol"]


def test_parent_sees_only_own_children(world):
    parent = world["make_member"](world["a"], roles=["parent"])
    Guardianship.objects.create(school=world["a"], membership=parent, student=world["bob"])
    actor = _actor(parent)
    assert _names(students.queryset(actor, "student.read")) == ["Bob"]
    assert students.can(actor, "student.read", world["bob"])
    assert not students.can(actor, "student.read", world["alice"])


def test_parent_cannot_reach_a_child_in_another_school(world):
    parent = world["make_member"](world["a"], roles=["parent"])
    # A (corrupt) link to another school's student is still filtered out by the tenant filter.
    Guardianship.objects.create(school=world["a"], membership=parent, student=world["other"])
    assert _names(students.queryset(_actor(parent), "student.read")) == []


def test_student_sees_only_self(world):
    me = world["make_member"](world["a"], world["student_user"], roles=["student"])
    actor = _actor(me)
    assert _names(students.queryset(actor, "student.read")) == ["Alice"]
    with pytest.raises(NotFound):
        students.get(actor, "student.read", world["bob"].pk)


def test_own_scope_sees_records_the_actor_created(world):
    from eduflow.authz.models import Role
    from eduflow.authz.services import _set_grants, bump_rbac_version

    clerk = world["make_member"](world["a"], roles=[])
    role = Role.objects.create(school=world["a"], key="clerk", name="Clerk")
    _set_grants(role, {"student.read": ["own"]})
    MembershipRole.objects.create(school=world["a"], membership=clerk, role=role)
    bump_rbac_version(world["a"].pk)
    world["carol"].created_by = clerk.user
    world["carol"].save()
    assert _names(students.queryset(_actor(clerk), "student.read")) == ["Carol"]


def test_department_scope_uses_the_role_assignment_department(world):
    from eduflow.authz.models import Role
    from eduflow.authz.services import _set_grants

    head = world["make_member"](world["a"], roles=[])
    role = Role.objects.create(school=world["a"], key="hod", name="Head of department")
    _set_grants(role, {"student.read": ["department"]})
    MembershipRole.objects.create(school=world["a"], membership=head, role=role, department="arts")
    assert _names(students.queryset(_actor(head), "student.read")) == ["Carol"]


def test_union_of_roles_widens_scope(world):
    member = world["make_member"](world["a"], world["student_user"], roles=["student", "parent"])
    Guardianship.objects.create(school=world["a"], membership=member, student=world["carol"])
    assert _names(students.queryset(_actor(member), "student.read")) == ["Alice", "Carol"]


def test_no_grant_is_permission_denied(world):
    staff = world["make_member"](world["a"], roles=["staff"])
    with pytest.raises(PermissionDenied):
        students.queryset(_actor(staff), "student.read")
    assert not students.can(_actor(staff), "student.read", world["alice"])


def test_scope_without_a_rule_grants_nothing(world):
    unsupported: ScopedResource[Student] = ScopedResource("student", Student)  # no rules at all
    teacher = world["make_member"](world["a"], roles=["teacher"])
    TeacherSection.objects.create(school=world["a"], membership=teacher, section=world["s1"])
    assert list(unsupported.queryset(_actor(teacher), "student.read")) == []


def test_results_are_not_duplicated_by_to_many_rules(world):
    teacher = world["make_member"](world["a"], roles=["teacher"])
    TeacherSection.objects.create(school=world["a"], membership=teacher, section=world["s1"])
    Mentorship.objects.create(school=world["a"], membership=teacher, student=world["alice"])
    Mentorship.objects.create(school=world["a"], membership=teacher, student=world["alice"])
    assert students.queryset(_actor(teacher), "student.read").count() == 2


def test_get_with_garbage_id_is_404(world):
    principal = world["make_member"](world["a"], roles=["principal"])
    with pytest.raises(NotFound):
        students.get(_actor(principal), "student.read", "not-an-id")


def test_member_list_endpoint_respects_self_scope(world, client_for):
    teacher = world["make_member"](world["a"], roles=["teacher"])
    world["make_member"](world["a"], roles=["parent"])
    response = client_for(teacher.user, world["a"]).get("/api/v1/memberships")
    assert [m["id"] for m in response.json()] == [str(teacher.id)]
