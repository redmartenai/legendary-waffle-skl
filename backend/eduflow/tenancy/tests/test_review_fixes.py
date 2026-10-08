"""Regression tests for the Phase 2 security review findings."""

import re

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from conftest import PASSWORD
from eduflow.audit.models import AuditEvent
from eduflow.authz.models import MembershipRole, Role
from eduflow.identity.models import OtpChallenge, User
from eduflow.identity.otp.providers import MemorySmsProvider, SmsUnavailable
from eduflow.tenancy.models import Membership

pytestmark = pytest.mark.django_db

MEMBERS = "/api/v1/memberships"


@pytest.fixture
def two_schools(make_school, make_member, client_for):
    a, b = make_school("rev-a"), make_school("rev-b")
    admin_a = make_member(a, roles=["school_admin"])
    admin_b = make_member(b, roles=["school_admin"])
    return {"a": a, "b": b, "client_a": client_for(admin_a.user, a), "client_b": client_for(admin_b.user, b)}


def _sent_code():
    match = re.search(r"\b(\d{6})\b", MemorySmsProvider.outbox[-1].message)
    assert match is not None
    return match.group(1)


# ---------------------------------------------------------------- finding 1: cross-school account takeover
def test_pre_registered_email_cannot_capture_another_schools_member(two_schools):
    # School A registers the victim's email together with a phone it controls.
    created = two_schools["client_a"].post(
        MEMBERS,
        {"full_name": "Victim", "email": "victim@school-b.test", "phone": "9833333333"},
        format="json",
    )
    assert created.status_code == 201
    # School B adding the real person by that email must not attach the account.
    response = two_schools["client_b"].post(
        MEMBERS, {"full_name": "Victim", "email": "victim@school-b.test"}, format="json"
    )
    assert response.status_code == 409
    victim = User.objects.get(email="victim@school-b.test")
    assert not Membership.objects.filter(user=victim, school=two_schools["b"]).exists()


def test_phone_attaches_only_after_its_owner_signed_in_by_otp(two_schools, api_client):
    phone = "+919844444444"
    body = {"full_name": "Teacher", "phone": phone}
    assert two_schools["client_a"].post(MEMBERS, body, format="json").status_code == 201
    assert two_schools["client_b"].post(MEMBERS, body, format="json").status_code == 409

    challenge = api_client.post("/api/v1/auth/otp/request", {"phone": phone}, format="json").json()
    verify = api_client.post(
        "/api/v1/auth/otp/verify",
        {"challenge_id": challenge["challenge_id"], "code": _sent_code()},
        format="json",
    )
    assert verify.status_code == 200
    assert User.objects.get(phone=phone).phone_verified_at is not None
    assert AuditEvent.objects.filter(action="identity.phone.verified").exists()

    assert two_schools["client_b"].post(MEMBERS, body, format="json").status_code == 201


def test_conflict_message_does_not_say_which_identifier_matched(two_schools, make_user):
    make_user(email="one@example.test", email_verified_at=timezone.now())
    make_user(phone="+919855555555", phone_verified_at=timezone.now())
    make_user(email="unverified@example.test")
    client = two_schools["client_a"]
    mixed = client.post(
        MEMBERS, {"full_name": "X", "email": "one@example.test", "phone": "+919855555555"}, format="json"
    ).json()["error"]
    unverified = client.post(
        MEMBERS, {"full_name": "X", "email": "unverified@example.test"}, format="json"
    ).json()["error"]
    assert mixed["code"] == unverified["code"] == "conflict"
    assert mixed["message"] == unverified["message"]


def test_account_without_password_can_set_its_first_one(make_user, client_for, api_client):
    user = make_user(phone="+919866666666", password=None)
    response = client_for(user).post(
        "/api/v1/auth/password/change", {"new_password": "My-first-password-1"}, format="json"
    )
    assert response.status_code == 204
    login = api_client.post(
        "/api/v1/auth/password/login",
        {"identifier": "+919866666666", "password": "My-first-password-1"},
        format="json",
    )
    assert login.status_code == 200


def test_member_creation_is_rate_limited(two_schools, settings):
    settings.RATE_LIMITS = {**settings.RATE_LIMITS, "member_create_user": "2/h"}
    client = two_schools["client_a"]
    statuses = [
        client.post(MEMBERS, {"full_name": "N", "phone": f"98770000{n:02d}"}, format="json").status_code
        for n in range(3)
    ]
    assert statuses == [201, 201, 429]
    assert client.get(MEMBERS).status_code == 200  # reads are not throttled


# ---------------------------------------------------------------- finding 3: activation escalation
def test_principal_cannot_deactivate_or_reactivate_an_admin(make_school, make_member, client_for):
    school = make_school()
    make_member(school, roles=["school_admin"])
    other_admin = make_member(school, roles=["school_admin"])
    client = client_for(make_member(school, roles=["principal"]).user, school)
    url = f"{MEMBERS}/{other_admin.id}"

    assert client.patch(url, {"is_active": False}, format="json").status_code == 403
    Membership.objects.filter(pk=other_admin.pk).update(is_active=False)
    assert client.patch(url, {"is_active": True}, format="json").status_code == 403
    other_admin.refresh_from_db()
    assert other_admin.is_active is False


def test_principal_can_still_manage_ordinary_members(make_school, make_member, client_for):
    school = make_school()
    teacher = make_member(school, roles=["teacher"])
    client = client_for(make_member(school, roles=["principal"]).user, school)
    assert client.patch(f"{MEMBERS}/{teacher.id}", {"is_active": False}, format="json").status_code == 200


# ---------------------------------------------------------------- finding 4: numeric identifiers
def test_numeric_identifier_counts_against_the_account_limit(api_client, make_user, settings):
    make_user(phone="+919876543210")
    settings.RATE_LIMITS = {**settings.RATE_LIMITS, "login_identifier": "2/m"}
    url = "/api/v1/auth/password/login"
    api_client.post(url, {"identifier": "9876543210", "password": "wrong-pass-1"}, format="json")
    api_client.post(url, {"identifier": 9876543210, "password": "wrong-pass-1"}, format="json")
    blocked = api_client.post(url, {"identifier": 9876543210, "password": PASSWORD}, format="json")
    assert blocked.status_code == 429


def test_numeric_phone_counts_against_the_otp_limit(api_client, make_user, settings):
    make_user(phone="+919812345678", password=None)
    settings.RATE_LIMITS = {**settings.RATE_LIMITS, "otp_request_phone": "1/h"}
    settings.OTP_RESEND_SECONDS = 0
    url = "/api/v1/auth/otp/request"
    assert api_client.post(url, {"phone": 9812345678}, format="json").status_code == 200
    assert api_client.post(url, {"phone": 9812345678}, format="json").status_code == 429


# ---------------------------------------------------------------- finding 5: OTP 503 enumeration
class FailingSmsProvider:
    def send(self, phone: str, message: str) -> None:
        raise SmsUnavailable("gateway down")


def test_delivery_failure_looks_like_an_unknown_number(api_client, make_user, settings):
    make_user(phone="+919812345678", password=None)
    settings.OTP_SMS_PROVIDER = "eduflow.tenancy.tests.test_review_fixes.FailingSmsProvider"
    known = api_client.post("/api/v1/auth/otp/request", {"phone": "+919812345678"}, format="json")
    unknown = api_client.post("/api/v1/auth/otp/request", {"phone": "+919800000001"}, format="json")
    assert known.status_code == unknown.status_code == 200
    assert set(known.json()) == set(unknown.json())
    event = AuditEvent.objects.filter(action="auth.otp.requested").order_by("id").first()
    assert event is not None
    assert event.metadata == {"account_found": True, "delivered": False}


def test_disabled_provider_refuses_every_number_alike(api_client, make_user, settings):
    make_user(phone="+919812345678", password=None)
    settings.OTP_SMS_PROVIDER = "eduflow.identity.otp.providers.DisabledSmsProvider"
    for phone in ("+919812345678", "+919800000001"):
        assert api_client.post("/api/v1/auth/otp/request", {"phone": phone}, format="json").status_code == 503
    assert not OtpChallenge.objects.exists()


# ---------------------------------------------------------------- finding 7: cooldown is serialised
def test_otp_request_takes_a_per_number_lock(api_client, make_user):
    make_user(phone="+919812345678", password=None)
    with CaptureQueriesContext(connection) as ctx:
        api_client.post("/api/v1/auth/otp/request", {"phone": "+919812345678"}, format="json")
    assert any("pg_advisory_xact_lock" in q["sql"] for q in ctx.captured_queries)


# ---------------------------------------------------------------- finding 8: timing, password-less accounts
def test_password_login_hashes_for_accounts_without_a_password(api_client, make_user, monkeypatch):
    from eduflow.identity import services

    make_user(email="otp-only@example.test", password=None)
    calls: list[str] = []

    def fake_make_password(password: str) -> str:
        calls.append(password)
        return "x"

    monkeypatch.setattr(services, "make_password", fake_make_password)
    response = api_client.post(
        "/api/v1/auth/password/login",
        {"identifier": "otp-only@example.test", "password": "anything-1"},
        format="json",
    )
    assert response.status_code == 401
    assert calls == ["anything-1"]


# ---------------------------------------------------------------- finding 6: last-admin checks are serialised
def test_admin_checks_lock_the_school_row(make_school, make_member, client_for):
    school = make_school()
    admin = make_member(school, roles=["school_admin"])
    second = make_member(school, roles=["school_admin"])
    admin_role = Role.objects.get(school=school, key="school_admin")
    client = client_for(admin.user, school)
    with CaptureQueriesContext(connection) as ctx:
        assert client.delete(f"{MEMBERS}/{second.id}/roles/{admin_role.id}").status_code == 204
    assert any('FROM "tenancy_school"' in q["sql"] and "FOR UPDATE" in q["sql"] for q in ctx.captured_queries)
    assert not MembershipRole.objects.filter(membership=second, role=admin_role).exists()
