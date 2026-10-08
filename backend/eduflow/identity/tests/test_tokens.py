"""Token lifecycle: expiry, rotation, reuse detection, revocation (docs/security/token-lifecycle.md)."""

from datetime import timedelta

import pytest
import time_machine
from django.utils import timezone

from conftest import PASSWORD
from eduflow.audit.models import AuditEvent
from eduflow.identity.models import AuthSession, RefreshToken

pytestmark = pytest.mark.django_db

REFRESH = "/api/v1/auth/token/refresh"


@pytest.fixture
def session(api_client, make_user):
    user = make_user()
    body = api_client.post(
        "/api/v1/auth/password/login", {"identifier": user.email, "password": PASSWORD}, format="json"
    ).json()
    return user, body


def _me(client, access):
    return client.get("/api/v1/me", HTTP_AUTHORIZATION=f"Bearer {access}")


def _refresh(client, raw):
    return client.post(REFRESH, {"refresh": raw}, format="json")


def test_access_token_authenticates(api_client, session):
    user, body = session
    response = _me(api_client, body["access"])
    assert response.status_code == 200
    assert response.json()["user"]["id"] == str(user.id)


def test_expired_access_token_is_rejected(api_client, session):
    _, body = session
    with time_machine.travel(timezone.now() + timedelta(minutes=10, seconds=1)):
        response = _me(api_client, body["access"])
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "not_authenticated"


def test_access_token_still_valid_just_before_expiry(api_client, session):
    _, body = session
    with time_machine.travel(timezone.now() + timedelta(minutes=9, seconds=50)):
        assert _me(api_client, body["access"]).status_code == 200


@pytest.mark.parametrize(
    "header",
    ["Bearer not-a-jwt", "Bearer eyJhbGciOiJub25lIn0.eyJzdWIiOiJ4In0.", "Basic dXNlcjpwYXNz", "Bearer"],
)
def test_malformed_or_forged_tokens_are_rejected_generically(api_client, header):
    response = api_client.get("/api/v1/me", HTTP_AUTHORIZATION=header)
    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] == "not_authenticated"
    assert "token" not in error["message"].lower()


def test_token_signed_with_another_key_is_rejected(api_client, session):
    import jwt

    _, body = session
    claims = jwt.decode(body["access"], options={"verify_signature": False})
    forged = jwt.encode(claims, "attacker-key-0123456789-0123456789-0123456789", algorithm="HS256")
    assert _me(api_client, forged).status_code == 401


def test_refresh_rotates_the_token(api_client, session):
    _, body = session
    response = _refresh(api_client, body["refresh"])
    assert response.status_code == 200
    rotated = response.json()
    assert rotated["refresh"] != body["refresh"]
    assert _me(api_client, rotated["access"]).status_code == 200

    old, new = RefreshToken.objects.order_by("created_at")
    assert old.used_at is not None
    assert old.replaced_by_id == new.id
    assert new.session_id == old.session_id


def test_rotated_token_can_be_used_once_more_only_as_a_theft_signal(api_client, session):
    user, body = session
    rotated = _refresh(api_client, body["refresh"]).json()

    reuse = _refresh(api_client, body["refresh"])

    assert reuse.status_code == 401
    session_row = AuthSession.objects.get(user=user)
    assert session_row.revoked_at is not None
    assert session_row.revoke_reason == "reuse_detected"
    # The whole family is dead: the newest refresh token and the latest access token both stop working.
    assert _refresh(api_client, rotated["refresh"]).status_code == 401
    assert _me(api_client, rotated["access"]).status_code == 401
    event = AuditEvent.objects.get(action="auth.refresh.reuse_detected")
    assert event.actor_id == user.id
    assert event.target_id == str(session_row.id)


def test_reuse_detection_does_not_affect_other_sessions(api_client, make_user, client_for):
    user = make_user()
    other_device = client_for(user)
    first = api_client.post(
        "/api/v1/auth/password/login", {"identifier": user.email, "password": PASSWORD}, format="json"
    ).json()
    _refresh(api_client, first["refresh"])
    _refresh(api_client, first["refresh"])  # reuse
    assert other_device.get("/api/v1/me").status_code == 200


def test_unknown_refresh_token_is_rejected(api_client, db):
    response = _refresh(api_client, "made-up-token")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "not_authenticated"


def test_expired_refresh_token_is_rejected(api_client, session):
    _, body = session
    with time_machine.travel(timezone.now() + timedelta(days=1, seconds=1)):
        assert _refresh(api_client, body["refresh"]).status_code == 401


def test_session_cannot_outlive_its_absolute_maximum(api_client, make_user, settings):
    settings.AUTH_SESSION_MAX_AGE = timedelta(hours=2)
    user = make_user()
    body = api_client.post(
        "/api/v1/auth/password/login",
        {"identifier": user.email, "password": PASSWORD, "remember": True},
        format="json",
    ).json()
    with time_machine.travel(timezone.now() + timedelta(hours=1)):
        body = _refresh(api_client, body["refresh"]).json()
    with time_machine.travel(timezone.now() + timedelta(hours=2, seconds=1)):
        assert _refresh(api_client, body["refresh"]).status_code == 401


def test_logout_revokes_access_and_refresh_immediately(api_client, session):
    user, body = session
    response = api_client.post(
        "/api/v1/auth/logout", {}, format="json", HTTP_AUTHORIZATION=f"Bearer {body['access']}"
    )
    assert response.status_code == 204
    assert _me(api_client, body["access"]).status_code == 401
    assert _refresh(api_client, body["refresh"]).status_code == 401
    assert AuthSession.objects.get(user=user).revoke_reason == "logout"
    assert AuditEvent.objects.filter(action="auth.logout", actor_id=user.id).exists()


def test_revoked_refresh_token_is_rejected_and_audited(api_client, session):
    _, body = session
    api_client.post("/api/v1/auth/logout", {}, format="json", HTTP_AUTHORIZATION=f"Bearer {body['access']}")
    assert _refresh(api_client, body["refresh"]).status_code == 401
    event = AuditEvent.objects.filter(action="auth.refresh", outcome="failure").latest("id")
    assert event.metadata == {"reason": "revoked"}


def test_logout_requires_authentication(api_client, db):
    assert api_client.post("/api/v1/auth/logout", {}, format="json").status_code == 401


def test_logout_cannot_revoke_someone_elses_session(make_user, client_for, api_client):
    victim = make_user()
    victim_body = api_client.post(
        "/api/v1/auth/password/login", {"identifier": victim.email, "password": PASSWORD}, format="json"
    ).json()
    attacker = client_for(make_user())
    attacker.post("/api/v1/auth/logout", {"refresh": victim_body["refresh"]}, format="json")
    assert _me(api_client, victim_body["access"]).status_code == 200


def test_logout_all_revokes_every_session(make_user, client_for):
    user = make_user()
    a, b = client_for(user), client_for(user)
    assert a.post("/api/v1/auth/logout-all").status_code == 204
    assert a.get("/api/v1/me").status_code == 401
    assert b.get("/api/v1/me").status_code == 401


def test_list_and_revoke_own_sessions(make_user, client_for):
    user = make_user()
    current, other = client_for(user), client_for(user)
    sessions = current.get("/api/v1/auth/sessions").json()
    assert len(sessions) == 2
    assert sum(s["current"] for s in sessions) == 1
    other_id = next(s["id"] for s in sessions if not s["current"])
    assert current.delete(f"/api/v1/auth/sessions/{other_id}").status_code == 204
    assert other.get("/api/v1/me").status_code == 401


def test_cannot_revoke_another_users_session(make_user, client_for):
    victim = client_for(make_user())
    attacker = client_for(make_user())
    victim_session = victim.issued.session.id
    assert attacker.delete(f"/api/v1/auth/sessions/{victim_session}").status_code == 404
    assert victim.get("/api/v1/me").status_code == 200


def test_deactivated_user_loses_access_at_once(make_user, client_for):
    user = make_user()
    client = client_for(user)
    user.is_active = False
    user.save()
    assert client.get("/api/v1/me").status_code == 401
