import jwt
import pytest
from django.conf import settings

from conftest import PASSWORD
from eduflow.audit.models import AuditEvent
from eduflow.identity.models import AuthSession, RefreshToken
from eduflow.identity.tokens import hash_token

LOGIN = "/api/v1/auth/password/login"

pytestmark = pytest.mark.django_db


def _login(client, identifier, password=PASSWORD, **extra):
    return client.post(LOGIN, {"identifier": identifier, "password": password, **extra}, format="json")


def test_valid_login_returns_tokens_user_and_memberships(api_client, make_user, make_school, make_member):
    user = make_user(email="Teacher@Example.test")
    school = make_school()
    make_member(school, user, roles=["teacher"])

    response = _login(api_client, "teacher@example.TEST")

    assert response.status_code == 200, response.content
    body = response.json()
    assert body["token_type"] == "Bearer"
    assert body["access_expires_in"] == 600
    assert body["user"]["id"] == str(user.id)
    assert body["user"]["email"] == "teacher@example.test"
    [membership] = body["memberships"]
    assert membership["school"]["id"] == str(school.id)
    assert [r["key"] for r in membership["roles"]] == ["teacher"]
    assert response["Cache-Control"] == "no-store"


def test_login_by_phone_number(api_client, make_user):
    make_user(phone="+919876543210")
    assert _login(api_client, "98765 43210").status_code == 200


def test_access_token_carries_identity_but_no_school_or_role(api_client, make_user):
    user = make_user()
    body = _login(api_client, user.email).json()
    claims = jwt.decode(
        body["access"],
        settings.JWT_SIGNING_KEY,
        algorithms=["HS256"],
        audience="eduflow-api",
        issuer="eduflow",
    )
    assert claims["sub"] == str(user.id)
    assert claims["token_type"] == "access"
    assert claims["exp"] - claims["iat"] == 600
    session = AuthSession.objects.get(user=user)
    assert claims["sid"] == str(session.id)
    assert not {"school", "school_id", "role", "roles", "permissions"} & set(claims)


def test_refresh_token_is_stored_only_as_a_hash(api_client, make_user):
    user = make_user()
    raw = _login(api_client, user.email).json()["refresh"]
    row = RefreshToken.objects.get(session__user=user)
    assert row.token_hash == hash_token(raw)
    assert raw not in row.token_hash
    assert len(raw) >= 60


@pytest.mark.parametrize("case", ["wrong_password", "unknown_account", "inactive_account"])
def test_failed_logins_are_indistinguishable(api_client, make_user, case):
    user = make_user(email="known@example.test")
    if case == "inactive_account":
        user.is_active = False
        user.save()
    identifier = "nobody@example.test" if case == "unknown_account" else user.email
    password = "wrong-password-123" if case == "wrong_password" else PASSWORD

    response = _login(api_client, identifier, password)

    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] == "invalid_credentials"
    assert error["message"] == "The details you entered are incorrect."
    assert not AuthSession.objects.exists()


def test_login_audits_success_and_failure(api_client, make_user):
    user = make_user()
    _login(api_client, user.email, "not-the-password-1")
    _login(api_client, user.email, remember=True)

    failure, success = AuditEvent.objects.filter(action="auth.login").order_by("id")
    assert failure.outcome == "failure"
    assert failure.actor_id == user.id
    assert success.outcome == "success"
    assert success.metadata == {"method": "password", "remember": True}
    assert success.ip == "127.0.0.1"
    assert success.request_id


def test_unknown_fields_are_rejected(api_client, make_user):
    user = make_user()
    response = _login(api_client, user.email, is_platform_admin=True)
    assert response.status_code == 400
    assert response.json()["error"]["fields"] == {"is_platform_admin": ["Unknown field."]}


def test_remember_me_extends_refresh_lifetime(api_client, make_user):
    user = make_user()
    short = _login(api_client, user.email).json()
    long = _login(api_client, user.email, remember=True).json()
    assert long["refresh_expires_at"] > short["refresh_expires_at"]


def test_forced_password_change_blocks_everything_else(make_user, make_school, make_member, client_for):
    user = make_user(must_change_password=True)
    school = make_school()
    make_member(school, user, roles=["school_admin"])
    client = client_for(user, school)

    blocked = client.get("/api/v1/school")
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "password_change_required"
    assert client.get("/api/v1/me").status_code == 200

    changed = client.post(
        "/api/v1/auth/password/change",
        {"current_password": PASSWORD, "new_password": "A-brand-new-passphrase-7"},
        format="json",
    )
    assert changed.status_code == 204
    assert client.get("/api/v1/school").status_code == 200


def test_password_change_rejects_wrong_current_and_weak_new(make_user, client_for):
    user = make_user()
    client = client_for(user)
    url = "/api/v1/auth/password/change"
    wrong = client.post(
        url, {"current_password": "nope-nope-nope", "new_password": "Another-good-one-8"}, format="json"
    )
    assert wrong.status_code == 401
    weak = client.post(url, {"current_password": PASSWORD, "new_password": "short"}, format="json")
    assert weak.status_code == 400


def test_password_change_signs_out_other_sessions(make_user, client_for):
    user = make_user()
    current = client_for(user)
    other = client_for(user)
    response = current.post(
        "/api/v1/auth/password/change",
        {"current_password": PASSWORD, "new_password": "A-brand-new-passphrase-7"},
        format="json",
    )
    assert response.status_code == 204
    assert current.get("/api/v1/me").status_code == 200
    assert other.get("/api/v1/me").status_code == 401
    assert AuditEvent.objects.filter(action="identity.password.change", outcome="success").exists()
