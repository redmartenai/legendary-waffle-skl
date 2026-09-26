import re

import pytest
from rest_framework.test import APIClient

from .conftest import PARENT_MEERA


@pytest.mark.django_db
def test_school_lookup_returns_branding():
    response = APIClient().get("/api/v1/schools/lookup", {"code": "ghis"})
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Greenwood High International School"
    assert body["branding"]["primary_color"] == "#2E5D4E"


@pytest.mark.django_db
def test_unknown_school_code():
    assert APIClient().get("/api/v1/schools/lookup", {"code": "NOPE"}).status_code == 404


@pytest.mark.django_db
def test_otp_sign_in_flow():
    client = APIClient()
    requested = client.post("/api/v1/auth/otp/request", {"school_code": "GHIS", "phone": "99000 00001"}, format="json")
    assert requested.status_code == 201
    body = requested.json()
    assert "dev_code" in body  # development echo

    wrong = client.post("/api/v1/auth/otp/verify", {"challenge_id": body["challenge_id"], "code": "000000" if body["dev_code"] != "000000" else "111111"}, format="json")
    assert wrong.status_code == 400

    verified = client.post("/api/v1/auth/otp/verify", {"challenge_id": body["challenge_id"], "code": body["dev_code"]}, format="json")
    assert verified.status_code == 200
    session = verified.json()
    assert session["user"]["phone"] == PARENT_MEERA
    assert session["memberships"][0]["school"]["code"] == "GHIS"
    assert {r["role"] for r in session["memberships"][0]["roles"]} == {"parent"}

    # A used code can't be replayed.
    replay = client.post("/api/v1/auth/otp/verify", {"challenge_id": body["challenge_id"], "code": body["dev_code"]}, format="json")
    assert replay.status_code == 400

    me = APIClient()
    me.credentials(HTTP_AUTHORIZATION=f"Bearer {session['access']}")
    assert me.get("/api/v1/me").json()["user"]["full_name"] == "Meera Iyer"


@pytest.mark.django_db
def test_unregistered_number_gets_same_response_but_no_code():
    response = APIClient().post("/api/v1/auth/otp/request", {"school_code": "GHIS", "phone": "9123456789"}, format="json")
    assert response.status_code == 201
    assert "dev_code" not in response.json()  # nothing sent, nothing leaked


@pytest.mark.django_db
def test_invalid_phone_rejected():
    response = APIClient().post("/api/v1/auth/otp/request", {"school_code": "GHIS", "phone": "12"}, format="json")
    assert response.status_code == 400
    assert response.json()["error"]["fields"]["phone"]


@pytest.mark.django_db
def test_live_sign_in_codes_never_reach_the_logs(settings, caplog):
    settings.EDUFLOW = {**settings.EDUFLOW, "OTP_DEV_ECHO": False}
    with caplog.at_level("INFO", logger="apps.accounts"):
        response = APIClient().post("/api/v1/auth/otp/request", {"school_code": "GHIS", "phone": "99000 00001"}, format="json")
    assert response.status_code == 201
    assert "dev_code" not in response.json()
    assert caplog.records, "the missing SMS provider should be reported"
    assert not any(re.search(r"\d{6}", record.getMessage()) for record in caplog.records)


@pytest.mark.django_db
def test_phone_first_otp_signs_in_to_every_school():
    client = APIClient()
    requested = client.post("/api/v1/auth/otp/request", {"phone": "99000 00001"}, format="json")
    assert requested.status_code == 201
    body = requested.json()
    verified = client.post("/api/v1/auth/otp/verify", {"challenge_id": body["challenge_id"], "code": body["dev_code"]}, format="json")
    assert verified.status_code == 200
    assert verified.json()["memberships"][0]["school"]["code"] == "GHIS"


@pytest.mark.django_db
def test_phone_first_unregistered_number_looks_the_same():
    registered = APIClient().post("/api/v1/auth/otp/request", {"phone": "99000 00001"}, format="json").json()
    unknown = APIClient().post("/api/v1/auth/otp/request", {"phone": "9123456789"}, format="json").json()
    assert set(unknown) == set(registered) - {"dev_code"}


@pytest.mark.django_db
def test_password_login_with_email_or_phone():
    from apps.accounts.models import User

    principal = User.objects.get(phone="+919800000001")
    principal.email = "krishnan@greenwood.test"
    principal.set_password("correct horse")
    principal.save()

    client = APIClient()
    by_email = client.post("/api/v1/auth/password/login", {"identifier": "Krishnan@Greenwood.test", "password": "correct horse", "remember": True}, format="json")
    assert by_email.status_code == 200
    assert by_email.json()["user"]["full_name"] == "S. Krishnan"

    by_phone = client.post("/api/v1/auth/password/login", {"identifier": "98000 00001", "password": "correct horse", "school_code": "ghis"}, format="json")
    assert by_phone.status_code == 200

    wrong = client.post("/api/v1/auth/password/login", {"identifier": "krishnan@greenwood.test", "password": "nope"}, format="json")
    unknown = client.post("/api/v1/auth/password/login", {"identifier": "nobody@greenwood.test", "password": "nope"}, format="json")
    # Same status and message whether the account exists or not.
    assert wrong.status_code == unknown.status_code == 400
    assert wrong.json() == unknown.json()


@pytest.mark.django_db
def test_password_login_needs_membership_in_named_school():
    from apps.accounts.models import User

    user = User.objects.get(phone="+919800000001")
    user.set_password("correct horse")
    user.save()
    response = APIClient().post("/api/v1/auth/password/login", {"identifier": "98000 00001", "password": "correct horse", "school_code": "SPS"}, format="json")
    assert response.status_code == 400


@pytest.mark.django_db
def test_password_login_without_remember_gets_short_session():
    import jwt
    from django.utils import timezone

    from apps.accounts.models import User

    user = User.objects.get(phone="+919800000001")
    user.set_password("correct horse")
    user.save()
    body = APIClient().post("/api/v1/auth/password/login", {"identifier": "98000 00001", "password": "correct horse"}, format="json").json()
    exp = jwt.decode(body["refresh"], options={"verify_signature": False})["exp"]
    assert exp - timezone.now().timestamp() < 13 * 3600
