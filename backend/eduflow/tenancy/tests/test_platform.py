"""Platform administration and public school lookup."""

import pytest

from conftest import PASSWORD
from eduflow.audit.models import AuditEvent
from eduflow.authz.catalog import SYSTEM_ROLES
from eduflow.authz.models import MembershipRole, Role
from eduflow.identity.models import AuthSession, User
from eduflow.tenancy.models import School

pytestmark = pytest.mark.django_db


@pytest.fixture
def staff_client(make_user, client_for):
    return client_for(make_user(is_platform_admin=True))


def test_create_school_with_first_admin(staff_client, api_client):
    response = staff_client.post(
        "/api/v1/platform/schools",
        {
            "code": "green-valley",
            "name": "Green Valley School",
            "admin": {
                "full_name": "Asha",
                "email": "asha@example.test",
                "temporary_password": "Temporary-pass-42",
            },
        },
        format="json",
    )
    assert response.status_code == 201, response.content
    school = School.objects.get(code="green-valley")
    # 12 school roles; Platform Admin is the User.is_platform_admin flag, not a school role.
    assert Role.objects.filter(school=school, is_system=True).count() == len(SYSTEM_ROLES) == 12
    admin = User.objects.get(email="asha@example.test")
    assert admin.must_change_password is True
    assert MembershipRole.objects.filter(membership__user=admin, role__key="school_admin").exists()
    assert {"tenancy.school.created", "tenancy.membership.created", "authz.role.assigned"} <= set(
        AuditEvent.objects.filter(school_id=school.id).values_list("action", flat=True)
    )

    login = api_client.post(
        "/api/v1/auth/password/login",
        {"identifier": "asha@example.test", "password": "Temporary-pass-42"},
        format="json",
    )
    assert login.status_code == 200
    assert login.json()["memberships"][0]["school"]["code"] == "green-valley"


def test_duplicate_school_code_is_conflict(staff_client, make_school):
    make_school("taken")
    response = staff_client.post("/api/v1/platform/schools", {"code": "taken", "name": "X"}, format="json")
    assert response.status_code == 409


def test_platform_endpoints_need_platform_admin(make_school, make_member, client_for, api_client):
    school = make_school()
    admin = make_member(school, roles=["school_admin"])
    assert client_for(admin.user, school).get("/api/v1/platform/schools").status_code == 403
    assert api_client.get("/api/v1/platform/schools").status_code == 401


def test_deactivating_a_school_locks_out_its_members(staff_client, make_school, make_member, client_for):
    school = make_school()
    member = make_member(school, roles=["school_admin"])
    member_client = client_for(member.user, school)
    assert member_client.get("/api/v1/school").status_code == 200
    assert (
        staff_client.patch(
            f"/api/v1/platform/schools/{school.id}", {"is_active": False}, format="json"
        ).status_code
        == 200
    )
    assert member_client.get("/api/v1/school").status_code == 403
    assert staff_client.get(f"/api/v1/platform/schools/{school.id}").json()["is_active"] is False


def test_deactivating_a_user_revokes_all_sessions(staff_client, make_user, client_for):
    user = make_user()
    client = client_for(user)
    response = staff_client.patch(f"/api/v1/platform/users/{user.id}", {"is_active": False}, format="json")
    assert response.status_code == 200
    assert client.get("/api/v1/me").status_code == 401
    assert AuthSession.objects.filter(user=user, revoked_at__isnull=True).count() == 0
    reactivated = staff_client.patch(f"/api/v1/platform/users/{user.id}", {"is_active": True}, format="json")
    assert reactivated.status_code == 200
    assert AuditEvent.objects.filter(action="identity.user.activated").exists()


def test_platform_admin_cannot_deactivate_self(make_user, client_for):
    staff = make_user(is_platform_admin=True)
    client = client_for(staff)
    assert (
        client.patch(f"/api/v1/platform/users/{staff.id}", {"is_active": False}, format="json").status_code
        == 403
    )


def test_school_lookup_returns_public_fields_only(api_client, make_school):
    school = make_school("lookup-me", name="Lookup School")
    response = api_client.get("/api/v1/schools/lookup?code=LOOKUP-ME")
    assert response.status_code == 200
    assert response.json() == {"id": str(school.id), "code": "lookup-me", "name": "Lookup School"}


def test_school_lookup_hides_inactive_and_unknown(api_client, make_school):
    make_school("closed", is_active=False)
    assert api_client.get("/api/v1/schools/lookup?code=closed").status_code == 404
    assert api_client.get("/api/v1/schools/lookup?code=nope").status_code == 404
    assert api_client.get("/api/v1/schools/lookup").status_code == 404


def test_current_school_read_and_update(make_school, make_member, client_for):
    school = make_school()
    admin = make_member(school, roles=["school_admin"])
    client = client_for(admin.user, school)
    assert client.get("/api/v1/school").json()["code"] == school.code
    response = client.patch("/api/v1/school", {"name": "Renamed"}, format="json")
    assert response.status_code == 200
    assert response.json()["name"] == "Renamed"
    assert client.patch("/api/v1/school", {"code": "hijack"}, format="json").status_code == 400


def test_create_platform_admin_command(monkeypatch):
    from django.core.management import call_command

    monkeypatch.setenv("EDUFLOW_ADMIN_PASSWORD", PASSWORD)
    call_command("create_platform_admin", "--email", "Ops@Example.test", "--full-name", "Ops")
    user = User.objects.get(email="ops@example.test")
    assert user.is_platform_admin
    assert user.check_password(PASSWORD)


def test_sync_rbac_adds_missing_default_grants(make_school):
    from django.core.management import call_command

    from eduflow.authz.models import RolePermission

    school = make_school()
    teacher = Role.objects.get(school=school, key="teacher")
    RolePermission.objects.filter(role=teacher, permission_id="report.read").delete()
    call_command("sync_rbac")
    assert RolePermission.objects.filter(role=teacher, permission_id="report.read").exists()
