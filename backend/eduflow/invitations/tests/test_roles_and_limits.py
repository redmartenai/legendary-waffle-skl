"""The five role experiences stay separated, and the public invitation endpoints are rate-limited."""

import pytest

pytestmark = pytest.mark.django_db

# What each primary role can reach in a school (backend authorization; there are no screens here).
EXPERIENCES = {
    "admin": {
        "/api/v1/invitations": 200,
        "/api/v1/roles": 200,
        "/api/v1/students": 200,
        "/api/v1/staff": 200,
    },
    "principal": {
        "/api/v1/invitations": 200,
        "/api/v1/roles": 200,
        "/api/v1/students": 200,
        "/api/v1/staff": 200,
    },
    "teacher": {
        "/api/v1/invitations": 403,
        "/api/v1/roles": 403,
        "/api/v1/students": 200,
        "/api/v1/staff": 200,
    },
    "parent": {
        "/api/v1/invitations": 403,
        "/api/v1/roles": 403,
        "/api/v1/students": 200,
        "/api/v1/staff": 403,
    },
    "student_member": {
        "/api/v1/invitations": 403,
        "/api/v1/roles": 403,
        "/api/v1/students": 200,
        "/api/v1/staff": 403,
    },
}


@pytest.mark.parametrize("role", sorted(EXPERIENCES))
def test_role_experiences_are_enforced_by_the_backend(world, as_member, role):
    client = as_member(getattr(world, role))
    for path, expected in EXPERIENCES[role].items():
        assert client.get(path).status_code == expected, (role, path)


def test_principal_does_not_hold_unrestricted_administration(world, as_member):
    permissions = as_member(world.principal).get("/api/v1/me/permissions").json()["permissions"]
    assert "role.delete" not in permissions
    admin_permissions = as_member(world.admin).get("/api/v1/me/permissions").json()["permissions"]
    assert set(permissions) < set(admin_permissions)


def test_client_supplied_role_or_school_hints_are_ignored(world, other_world, as_member, client_for):
    teacher = client_for(world.teacher.user, world.school)
    # A forged role header or a foreign school changes nothing: roles come only from the server.
    assert teacher.get("/api/v1/invitations", HTTP_X_ROLE="school_admin").status_code == 403
    response = teacher.get("/api/v1/students", HTTP_X_SCHOOL_ID=str(other_world.school.pk))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "tenant_forbidden"


def test_preview_rate_limit_per_ip(world, api_client, settings):
    settings.RATE_LIMITS = {**settings.RATE_LIMITS, "invitation_ip": "3/m"}
    statuses = [
        api_client.post(
            "/api/v1/invitations/preview", {"token": f"guess-{n:040d}"}, format="json"
        ).status_code
        for n in range(4)
    ]
    assert statuses == [404, 404, 404, 429]


def test_per_secret_rate_limit_across_ips(world, invite, api_client, settings):
    settings.RATE_LIMITS = {**settings.RATE_LIMITS, "invitation_token": "2/m"}
    sent = invite()
    statuses = [
        api_client.post(
            "/api/v1/invitations/preview", {"token": sent.token}, format="json", REMOTE_ADDR=f"10.0.0.{n}"
        ).status_code
        for n in range(3)
    ]
    assert statuses == [200, 200, 429]


def test_management_rate_limit(world, invite, settings):
    settings.RATE_LIMITS = {**settings.RATE_LIMITS, "invitation_manage_user": "1/h"}
    invite()
    invite(recipient="+919811100002", employee_id="T-2", expect=429)
