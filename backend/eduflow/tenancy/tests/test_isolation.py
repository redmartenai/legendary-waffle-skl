"""Tenant isolation (ADR-003, ADR-013).

The isolation matrix: a school admin of school A — the broadest tenant role there is — sends every tenant
endpoint school B's object IDs. Every one must answer exactly like an ID that does not exist (404), so B's
objects can neither be read, changed, deleted nor even confirmed to exist. ``test_matrix_covers_every_*``
fails when a new tenant endpoint with an object ID in its path is not registered here.
"""

import uuid

import pytest
from django.urls import URLPattern, URLResolver, get_resolver

from eduflow.audit.models import AuditEvent
from eduflow.authz.api.base import TenantAPIView
from eduflow.authz.models import MembershipRole, Role
from eduflow.tenancy.models import Membership

pytestmark = pytest.mark.django_db


@pytest.fixture
def world(make_user, make_school, make_member):
    a, b = make_school("school-a"), make_school("school-b")
    admin_a = make_member(a, roles=["school_admin"])
    member_b = make_member(b, roles=["teacher"])
    custom_b = Role.objects.create(school=b, key="custom-b", name="Custom B")
    return {
        "a": a,
        "b": b,
        "admin_a": admin_a,
        "member_b": member_b,
        "role_b": Role.objects.get(school=b, key="teacher"),
        "custom_b": custom_b,
    }


# (method, path template, body). Placeholders are filled with school B's IDs (or random ones).
MATRIX = [
    ("get", "/api/v1/memberships/{membership}", None),
    ("patch", "/api/v1/memberships/{membership}", {"is_active": False}),
    ("post", "/api/v1/memberships/{membership}/roles", {"role_id": "{role}"}),
    ("delete", "/api/v1/memberships/{membership}/roles/{role}", None),
    ("get", "/api/v1/roles/{role}", None),
    ("patch", "/api/v1/roles/{role}", {"name": "Hijacked"}),
    ("delete", "/api/v1/roles/{custom}", None),
]
MATRIX_PATHS = {path for _, path, _ in MATRIX}


def _fill(value, ids):
    if isinstance(value, dict):
        return {k: _fill(v, ids) for k, v in value.items()}
    if isinstance(value, str):
        return value.format(**ids)
    return value


def _call(client, method, path, body):
    return (
        getattr(client, method)(path, body, format="json")
        if body is not None
        else getattr(client, method)(path)
    )


@pytest.mark.parametrize(("method", "path", "body"), MATRIX)
def test_school_a_admin_cannot_touch_school_b_objects(world, client_for, method, path, body):
    client = client_for(world["admin_a"].user, world["a"])
    foreign = {"membership": world["member_b"].id, "role": world["role_b"].id, "custom": world["custom_b"].id}
    missing = {"membership": uuid.uuid4(), "role": uuid.uuid4(), "custom": uuid.uuid4()}

    foreign_response = _call(client, method, _fill(path, foreign), _fill(body, foreign))
    missing_response = _call(client, method, _fill(path, missing), _fill(body, missing))

    assert foreign_response.status_code in (404, 400), foreign_response.content
    # Identical to a non-existent ID: existence does not leak.
    assert foreign_response.status_code == missing_response.status_code
    assert foreign_response.json()["error"]["code"] == missing_response.json()["error"]["code"]
    assert foreign_response.json()["error"]["message"] == missing_response.json()["error"]["message"]


def test_school_b_data_is_unchanged_after_the_matrix(world, client_for):
    client = client_for(world["admin_a"].user, world["a"])
    foreign = {"membership": world["member_b"].id, "role": world["role_b"].id, "custom": world["custom_b"].id}
    for method, path, body in MATRIX:
        _call(client, method, _fill(path, foreign), _fill(body, foreign))
    world["member_b"].refresh_from_db()
    world["role_b"].refresh_from_db()
    assert world["member_b"].is_active is True
    assert world["role_b"].name == "Teacher"
    assert Role.objects.filter(pk=world["custom_b"].pk).exists()
    assert MembershipRole.objects.filter(membership=world["member_b"]).count() == 1


def test_assigning_a_foreign_role_to_own_member_is_rejected(world, client_for, make_member):
    own = make_member(world["a"], roles=[])
    client = client_for(world["admin_a"].user, world["a"])
    response = client.post(
        f"/api/v1/memberships/{own.id}/roles", {"role_id": str(world["role_b"].id)}, format="json"
    )
    assert response.status_code == 400
    assert not MembershipRole.objects.filter(membership=own).exists()


def test_lists_only_show_own_school(world, client_for):
    client = client_for(world["admin_a"].user, world["a"])
    members = client.get("/api/v1/memberships").json()
    roles = client.get("/api/v1/roles").json()
    assert {m["id"] for m in members} == {str(world["admin_a"].id)}
    assert all(r["id"] != str(world["role_b"].id) for r in roles)
    assert len(roles) == Role.objects.filter(school=world["a"]).count()


def test_header_for_a_school_without_membership_is_forbidden(world, client_for):
    client = client_for(world["admin_a"].user, world["b"])
    response = client.get("/api/v1/memberships")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "tenant_forbidden"
    event = AuditEvent.objects.get(action="tenancy.access_denied")
    assert event.outcome == "denied"
    assert event.target_id == str(world["b"].id)
    assert event.actor_id == world["admin_a"].user_id


@pytest.mark.parametrize("value", ["not-a-uuid", str(uuid.uuid4()), "' OR 1=1 --"])
def test_unknown_or_malformed_school_is_the_same_403(world, client_for, value):
    client = client_for(world["admin_a"].user)
    response = client.get("/api/v1/memberships", HTTP_X_SCHOOL_ID=value)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "tenant_forbidden"


def test_missing_school_header(world, client_for):
    response = client_for(world["admin_a"].user).get("/api/v1/memberships")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "tenant_required"


def test_inactive_membership_is_forbidden(world, client_for):
    Membership.objects.filter(pk=world["admin_a"].pk).update(is_active=False)
    response = client_for(world["admin_a"].user, world["a"]).get("/api/v1/school")
    assert response.status_code == 403


def test_inactive_school_is_forbidden(world, client_for):
    world["a"].is_active = False
    world["a"].save()
    assert client_for(world["admin_a"].user, world["a"]).get("/api/v1/school").status_code == 403


def test_platform_admin_has_no_implicit_school_access(world, make_user, client_for):
    staff = make_user(is_platform_admin=True)
    response = client_for(staff, world["b"]).get("/api/v1/memberships")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "tenant_forbidden"


def test_unauthenticated_tenant_request(world, api_client):
    response = api_client.get("/api/v1/school", HTTP_X_SCHOOL_ID=str(world["a"].id))
    assert response.status_code == 401


def test_member_of_two_schools_switches_by_header(world, client_for, make_member):
    both = make_member(world["a"], roles=["school_admin"])
    make_member(world["b"], both.user, roles=["teacher"])
    client = client_for(both.user)
    as_a = client.get("/api/v1/me/permissions", HTTP_X_SCHOOL_ID=str(world["a"].id)).json()
    as_b = client.get("/api/v1/me/permissions", HTTP_X_SCHOOL_ID=str(world["b"].id)).json()
    assert as_a["permissions"]["role.update"] == ["school"]
    assert "role.update" not in as_b["permissions"]
    assert as_b["permissions"]["student.read"] == ["assigned", "section"]


def _tenant_routes(resolver=None, prefix=""):
    resolver = resolver or get_resolver()
    for entry in resolver.url_patterns:
        if isinstance(entry, URLResolver):
            yield from _tenant_routes(entry, prefix + str(entry.pattern))
        elif isinstance(entry, URLPattern):
            view = getattr(entry.callback, "view_class", None)
            if view is not None and issubclass(view, TenantAPIView):
                yield "/" + prefix + str(entry.pattern), view


def test_matrix_covers_every_tenant_endpoint_with_an_object_id():
    import re

    from eduflow.people.tests.test_isolation import PHASE3_MATRIX_PATHS

    normalised = {re.sub(r"\{\w+\}", "<id>", p) for p in MATRIX_PATHS | PHASE3_MATRIX_PATHS}
    for route, view in _tenant_routes():
        if "<uuid:" not in route:
            continue
        shape = re.sub(r"<uuid:\w+>", "<id>", route)
        assert shape in normalised, f"{view.__name__} ({route}) is not in the isolation matrix"


def test_every_tenant_handler_declares_a_permission():
    for route, view in _tenant_routes():
        handlers = {
            m.upper() for m in view.http_method_names if hasattr(view, m) and m not in ("options", "head")
        }
        undeclared = handlers - set(view.required_permissions)
        assert not undeclared, f"{view.__name__} ({route}) has no permission for {sorted(undeclared)}"
