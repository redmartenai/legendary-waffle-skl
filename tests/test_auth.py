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
