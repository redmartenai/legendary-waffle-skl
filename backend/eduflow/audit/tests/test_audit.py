"""The audit trail (ADR-015): what is recorded, with which context, and what is never recorded."""

import pytest

from conftest import PASSWORD
from eduflow.audit import services as audit
from eduflow.audit.models import AuditEvent
from eduflow.core.request_context import bind_request_id, bind_request_info, clear_context

pytestmark = pytest.mark.django_db


def test_record_captures_request_context():
    bind_request_id("req-audit-0001")
    bind_request_info(ip="203.0.113.7", user_agent="EduFlowApp/1.0", user_id=None, school_id=None)
    try:
        event = audit.record("test.event", target_type="thing", target_id=42)
    finally:
        clear_context()
    assert event.request_id == "req-audit-0001"
    assert event.ip == "203.0.113.7"
    assert event.user_agent == "EduFlowApp/1.0"
    assert event.target_id == "42"
    assert event.occurred_at is not None


def test_metadata_is_redacted_and_bounded():
    event = audit.record(
        "test.event",
        metadata={"password": "hunter2", "refresh": "abc", "code": "123456", "note": "Bearer eyJabc.def.ghi"},
    )
    assert event.metadata["password"] == "[REDACTED]"
    assert event.metadata["refresh"] == "[REDACTED]"
    assert event.metadata["code"] == "[REDACTED]"
    assert "eyJ" not in event.metadata["note"]
    assert audit.record("test.big", metadata={"blob": "x" * 10_000}).metadata == {"truncated": True}


def test_authentication_lifecycle_is_audited(api_client, make_user):
    user = make_user()
    api_client.post(
        "/api/v1/auth/password/login", {"identifier": user.email, "password": "nope-nope-1"}, format="json"
    )
    body = api_client.post(
        "/api/v1/auth/password/login", {"identifier": user.email, "password": PASSWORD}, format="json"
    ).json()
    rotated = api_client.post(
        "/api/v1/auth/token/refresh", {"refresh": body["refresh"]}, format="json"
    ).json()
    api_client.post("/api/v1/auth/token/refresh", {"refresh": body["refresh"]}, format="json")  # reuse
    api_client.post(
        "/api/v1/auth/logout", {}, format="json", HTTP_AUTHORIZATION=f"Bearer {rotated['access']}"
    )

    events = list(AuditEvent.objects.order_by("id").values_list("action", "outcome"))
    assert events == [
        ("auth.login", "failure"),
        ("auth.login", "success"),
        ("auth.refresh", "success"),
        ("auth.refresh.reuse_detected", "failure"),
    ]
    # Logout after reuse detection is rejected (the session is already revoked), so it is not a success.
    assert not AuditEvent.objects.filter(action="auth.logout").exists()


def test_no_token_or_password_is_ever_stored(api_client, make_user):
    user = make_user()
    body = api_client.post(
        "/api/v1/auth/password/login", {"identifier": user.email, "password": PASSWORD}, format="json"
    ).json()
    api_client.post("/api/v1/auth/token/refresh", {"refresh": body["refresh"]}, format="json")
    dump = str(list(AuditEvent.objects.values()))
    for secret in (PASSWORD, body["access"], body["refresh"]):
        assert secret not in dump


def test_user_deactivation_and_membership_changes_are_audited(
    make_school, make_member, make_user, client_for
):
    school = make_school()
    admin = make_member(school, roles=["school_admin"])
    member = make_member(school, roles=["teacher"])
    client_for(admin.user, school).patch(
        f"/api/v1/memberships/{member.id}", {"is_active": True}, format="json"
    )
    client_for(admin.user, school).patch(
        f"/api/v1/memberships/{member.id}", {"is_active": False}, format="json"
    )
    staff = make_user(is_platform_admin=True)
    client_for(staff).patch(f"/api/v1/platform/users/{member.user_id}", {"is_active": False}, format="json")

    event = AuditEvent.objects.get(action="tenancy.membership.deactivated")
    assert event.school_id == school.id
    assert event.actor_id == admin.user_id
    deactivated = AuditEvent.objects.get(action="identity.user.deactivated")
    assert deactivated.actor_id == staff.id
    assert deactivated.target_id == str(member.user_id)


def test_audit_api_is_scoped_to_the_school_and_permission(make_school, make_member, client_for):
    a, b = make_school(), make_school()
    admin_a = make_member(a, roles=["school_admin"])
    teacher_a = make_member(a, roles=["teacher"])
    AuditEvent.objects.create(action="seen", school_id=a.id)
    AuditEvent.objects.create(action="hidden.other_school", school_id=b.id)
    AuditEvent.objects.create(action="hidden.platform", school_id=None)

    response = client_for(admin_a.user, a).get("/api/v1/audit-events?page_size=200")
    assert response.status_code == 200
    actions = {e["action"] for e in response.json()["results"]}
    assert "seen" in actions
    assert not {a for a in actions if a.startswith("hidden")}
    assert client_for(teacher_a.user, a).get("/api/v1/audit-events").status_code == 403


def test_audit_api_filters_and_paginates(make_school, make_member, client_for):
    school = make_school()
    admin = make_member(school, roles=["school_admin"])
    for n in range(3):
        AuditEvent.objects.create(action="page.test", school_id=school.id, target_id=str(n))
    client = client_for(admin.user, school)
    first = client.get("/api/v1/audit-events?action=page.test&page_size=2").json()
    assert [e["target_id"] for e in first["results"]] == ["2", "1"]
    second = client.get(first["next"]).json()
    assert [e["target_id"] for e in second["results"]] == ["0"]
    assert client.get("/api/v1/audit-events?actor_id=nope").status_code == 400
