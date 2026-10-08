"""Role -> permission enforcement, role management and escalation guards (ADR-004)."""

import pytest

from eduflow.audit.models import AuditEvent
from eduflow.authz.catalog import PERMISSIONS, SYSTEM_ROLES
from eduflow.authz.grants import compute_grants
from eduflow.authz.models import MembershipRole, Permission, Role, RolePermission

pytestmark = pytest.mark.django_db


@pytest.fixture
def school(make_school):
    return make_school("rbac-school")


@pytest.fixture
def admin(school, make_member, client_for):
    member = make_member(school, roles=["school_admin"])
    return member, client_for(member.user, school)


def test_catalog_is_synced_and_every_school_gets_every_system_role(school):
    assert set(Permission.objects.values_list("codename", flat=True)) == set(PERMISSIONS)
    assert set(Role.objects.filter(school=school, is_system=True).values_list("key", flat=True)) == set(
        SYSTEM_ROLES
    )


def test_system_role_matrix_is_seeded(school):
    teacher = Role.objects.get(school=school, key="teacher")
    grants = {g.permission_id: sorted(g.scopes) for g in RolePermission.objects.filter(role=teacher)}
    assert grants["attendance.create"] == ["section"]
    assert grants["student.read"] == ["assigned", "section"]
    assert "role.update" not in grants


def test_allowed_action(admin):
    _, client = admin
    assert client.get("/api/v1/roles").status_code == 200


@pytest.mark.parametrize(
    ("role", "method", "path"),
    [
        ("teacher", "get", "/api/v1/roles"),
        ("parent", "get", "/api/v1/audit-events"),
        ("student", "patch", "/api/v1/school"),
        ("accountant", "post", "/api/v1/memberships"),
        ("driver", "get", "/api/v1/permissions"),
    ],
)
def test_denied_action(school, make_member, client_for, role, method, path):
    member = make_member(school, roles=[role])
    response = getattr(client_for(member.user, school), method)(path, {}, format="json")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"


def test_member_without_roles_can_do_nothing(school, make_member, client_for):
    client = client_for(make_member(school, roles=[]).user, school)
    assert client.get("/api/v1/school").status_code == 403
    assert client.get("/api/v1/me/permissions").status_code == 403


def test_role_assignment_grants_permissions_immediately(school, admin, make_member, client_for):
    _, admin_client = admin
    member = make_member(school, roles=["staff"])
    member_client = client_for(member.user, school)
    assert member_client.get("/api/v1/roles").status_code == 403

    principal = Role.objects.get(school=school, key="principal")
    response = admin_client.post(
        f"/api/v1/memberships/{member.id}/roles",
        {"role_id": str(principal.id), "title": "Principal"},
        format="json",
    )
    assert response.status_code == 201, response.content
    assert {r["key"] for r in response.json()["roles"]} == {"staff", "principal"}
    assert member_client.get("/api/v1/roles").status_code == 200  # cache invalidated by rbac_version

    removed = admin_client.delete(f"/api/v1/memberships/{member.id}/roles/{principal.id}")
    assert removed.status_code == 204
    assert member_client.get("/api/v1/roles").status_code == 403

    actions = list(
        AuditEvent.objects.filter(target_id=str(member.id)).order_by("id").values_list("action", flat=True)
    )
    assert actions[-2:] == ["authz.role.assigned", "authz.role.unassigned"]


def test_assigning_the_same_role_twice_is_a_conflict(school, admin, make_member):
    _, client = admin
    member = make_member(school, roles=["teacher"])
    teacher = Role.objects.get(school=school, key="teacher")
    response = client.post(
        f"/api/v1/memberships/{member.id}/roles", {"role_id": str(teacher.id)}, format="json"
    )
    assert response.status_code == 409


def test_permission_assignment_changes_take_effect_and_are_audited(school, admin, make_member, client_for):
    _, admin_client = admin
    teacher_member = make_member(school, roles=["teacher"])
    teacher_client = client_for(teacher_member.user, school)
    teacher_role = Role.objects.get(school=school, key="teacher")
    assert teacher_client.get("/api/v1/audit-events").status_code == 403

    grants = {g.permission_id: g.scopes for g in RolePermission.objects.filter(role=teacher_role)}
    grants["audit.read"] = ["school"]
    del grants["report.read"]
    response = admin_client.patch(f"/api/v1/roles/{teacher_role.id}", {"permissions": grants}, format="json")

    assert response.status_code == 200, response.content
    assert response.json()["permissions"]["audit.read"] == ["school"]
    assert teacher_client.get("/api/v1/audit-events").status_code == 200
    event = AuditEvent.objects.filter(action="authz.role.updated").latest("id")
    assert event.metadata["added"] == ["audit.read"]
    assert event.metadata["removed"] == ["report.read"]


def test_invalid_grants_are_rejected(school, admin):
    _, client = admin
    role = Role.objects.get(school=school, key="teacher")
    invalid: list[dict[str, list[str]]] = [
        {"nope.read": ["school"]},
        {"school.read": []},
        {"school.read": ["galaxy"]},
    ]
    for permissions in invalid:
        assert (
            client.patch(f"/api/v1/roles/{role.id}", {"permissions": permissions}, format="json").status_code
            == 400
        )
    platform = client.patch(
        f"/api/v1/roles/{role.id}", {"permissions": {"school.read": ["platform"]}}, format="json"
    )
    assert platform.status_code == 400


def test_custom_role_lifecycle(school, admin, make_member):
    _, client = admin
    teacher = Role.objects.get(school=school, key="teacher")
    created = client.post(
        "/api/v1/roles", {"name": "Lab assistant", "based_on": str(teacher.id)}, format="json"
    )
    assert created.status_code == 201, created.content
    body = created.json()
    assert body["is_system"] is False
    assert body["permissions"]["attendance.create"] == ["section"]

    member = make_member(school, roles=[])
    client.post(f"/api/v1/memberships/{member.id}/roles", {"role_id": body["id"]}, format="json")
    assert client.delete(f"/api/v1/roles/{body['id']}").status_code == 409
    MembershipRole.objects.filter(role_id=body["id"]).delete()
    assert client.delete(f"/api/v1/roles/{body['id']}").status_code == 204
    assert {"authz.role.created", "authz.role.deleted"} <= set(
        AuditEvent.objects.values_list("action", flat=True)
    )


def test_system_roles_cannot_be_deleted_and_admin_role_is_locked(school, admin):
    _, client = admin
    teacher = Role.objects.get(school=school, key="teacher")
    admin_role = Role.objects.get(school=school, key="school_admin")
    assert client.delete(f"/api/v1/roles/{teacher.id}").status_code == 403
    assert (
        client.patch(f"/api/v1/roles/{admin_role.id}", {"permissions": {}}, format="json").status_code == 403
    )


def test_cannot_grant_permissions_one_does_not_hold(school, make_member, client_for):
    """Vertical escalation: a principal (no role.delete) cannot hand role.delete to anyone."""
    principal = make_member(school, roles=["principal"])
    client = client_for(principal.user, school)
    staff = Role.objects.get(school=school, key="staff")

    response = client.patch(
        f"/api/v1/roles/{staff.id}",
        {"permissions": {"school.read": ["school"], "role.delete": ["school"]}},
        format="json",
    )
    assert response.status_code == 403
    created = client.post(
        "/api/v1/roles", {"name": "Sneaky", "permissions": {"role.delete": ["school"]}}, format="json"
    )
    assert created.status_code == 403


def test_cannot_assign_a_role_more_powerful_than_oneself(school, make_member, client_for):
    principal = make_member(school, roles=["principal"])
    client = client_for(principal.user, school)
    admin_role = Role.objects.get(school=school, key="school_admin")

    to_self = client.post(
        f"/api/v1/memberships/{principal.id}/roles", {"role_id": str(admin_role.id)}, format="json"
    )
    other = make_member(school, roles=[])
    to_other = client.post(
        f"/api/v1/memberships/{other.id}/roles", {"role_id": str(admin_role.id)}, format="json"
    )

    assert to_self.status_code == 403
    assert to_other.status_code == 403
    assert not MembershipRole.objects.filter(role=admin_role, membership__in=[principal, other]).exists()


def test_last_school_admin_is_protected(school, admin, make_member, client_for):
    member, client = admin
    admin_role = Role.objects.get(school=school, key="school_admin")
    assert client.delete(f"/api/v1/memberships/{member.id}/roles/{admin_role.id}").status_code == 409

    second = make_member(school, roles=["school_admin"])
    second_client = client_for(second.user, school)
    assert (
        second_client.patch(
            f"/api/v1/memberships/{member.id}", {"is_active": False}, format="json"
        ).status_code
        == 200
    )
    # Now `second` is the last active admin.
    assert (
        client_for(second.user, school)
        .delete(f"/api/v1/memberships/{second.id}/roles/{admin_role.id}")
        .status_code
        == 409
    )


def test_cannot_deactivate_own_membership(admin):
    member, client = admin
    assert (
        client.patch(f"/api/v1/memberships/{member.id}", {"is_active": False}, format="json").status_code
        == 403
    )


def test_membership_deactivation_removes_access_and_is_audited(school, admin, make_member, client_for):
    _, admin_client = admin
    member = make_member(school, roles=["teacher"])
    member_client = client_for(member.user, school)
    assert member_client.get("/api/v1/school").status_code == 200

    response = admin_client.patch(f"/api/v1/memberships/{member.id}", {"is_active": False}, format="json")

    assert response.status_code == 200
    assert response.json()["is_active"] is False
    assert member_client.get("/api/v1/school").status_code == 403
    assert AuditEvent.objects.filter(
        action="tenancy.membership.deactivated", target_id=str(member.id)
    ).exists()


def test_effective_permissions_are_the_union_of_roles(school, make_member):
    member = make_member(school, roles=["teacher", "accountant"])
    grants = compute_grants(member)
    assert grants["student.read"] == {"section", "assigned", "school"}
    assert grants["fee.update"] == {"school"}
    assert grants["attendance.create"] == {"section"}


def test_inactive_membership_has_no_grants(school, make_member):
    member = make_member(school, roles=["school_admin"], is_active=False)
    assert compute_grants(member) == {}


def test_my_permissions_endpoint(school, make_member, client_for):
    member = make_member(school, roles=["parent"])
    body = client_for(member.user, school).get("/api/v1/me/permissions").json()
    assert body["school_id"] == str(school.id)
    assert body["permissions"]["student.read"] == ["child"]


def test_add_member_with_roles(school, admin):
    _, client = admin
    teacher = Role.objects.get(school=school, key="teacher")
    response = client.post(
        "/api/v1/memberships",
        {"full_name": "New Teacher", "phone": "9811111111", "role_ids": [str(teacher.id)]},
        format="json",
    )
    assert response.status_code == 201, response.content
    body = response.json()
    assert body["user"]["phone"] == "+919811111111"
    assert [r["key"] for r in body["roles"]] == ["teacher"]
    duplicate = client.post(
        "/api/v1/memberships", {"full_name": "Again", "phone": "+919811111111"}, format="json"
    )
    assert duplicate.status_code == 409


def test_add_member_with_roles_needs_role_assign(school, make_member, client_for):
    hr = make_member(school, roles=["hr_manager"])  # user.read/update but not user.create
    client = client_for(hr.user, school)
    assert (
        client.post(
            "/api/v1/memberships", {"full_name": "X", "email": "x@example.test"}, format="json"
        ).status_code
        == 403
    )


def test_add_member_attaches_existing_account_without_editing_it(school, admin, make_user):
    _, client = admin
    existing = make_user(email="existing@example.test", full_name="Real Name")
    response = client.post(
        "/api/v1/memberships", {"full_name": "Renamed", "email": "EXISTING@example.test"}, format="json"
    )
    assert response.status_code == 201
    existing.refresh_from_db()
    assert existing.full_name == "Real Name"
    assert response.json()["user"]["id"] == str(existing.id)


def test_new_member_with_temporary_password_must_change_it(school, admin):
    _, client = admin
    response = client.post(
        "/api/v1/memberships",
        {"full_name": "Temp", "email": "temp@example.test", "temporary_password": "Temporary-pass-42"},
        format="json",
    )
    assert response.status_code == 201
    from eduflow.identity.models import User

    assert User.objects.get(email="temp@example.test").must_change_password is True
