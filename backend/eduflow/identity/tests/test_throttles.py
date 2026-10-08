"""Rate limits on authentication endpoints (ADR-016)."""

from datetime import timedelta

import pytest
import time_machine
from django.utils import timezone

from conftest import PASSWORD
from eduflow.identity.throttles import parse_rate

pytestmark = pytest.mark.django_db

LOGIN = "/api/v1/auth/password/login"


def _login(client, identifier, ip="10.0.0.1"):
    return client.post(
        LOGIN, {"identifier": identifier, "password": "wrong-password-1"}, format="json", REMOTE_ADDR=ip
    )


@pytest.mark.parametrize(
    ("rate", "expected"), [("5/m", (5, 60)), ("5/15m", (5, 900)), ("10/h", (10, 3600)), ("3/2d", (3, 172800))]
)
def test_parse_rate(rate, expected):
    assert parse_rate(rate) == expected


@pytest.mark.parametrize("rate", ["5", "x/m", "5/15", "5/w"])
def test_parse_rate_rejects_garbage(rate):
    with pytest.raises(ValueError, match="Invalid rate"):
        parse_rate(rate)


def test_login_identifier_limit_then_retry_after_window(api_client, settings):
    settings.RATE_LIMITS = {**settings.RATE_LIMITS, "login_identifier": "3/m"}
    start = timezone.now()
    with time_machine.travel(start, tick=False):
        statuses = [_login(api_client, "victim@example.test").status_code for _ in range(4)]
        blocked = _login(api_client, "victim@example.test")
    assert statuses == [401, 401, 401, 429]
    error = blocked.json()["error"]
    assert error["code"] == "rate_limited"
    assert error["retry_after_seconds"] >= 1
    assert blocked["Retry-After"] == str(error["retry_after_seconds"])

    with time_machine.travel(start + timedelta(seconds=61), tick=False):
        assert _login(api_client, "victim@example.test").status_code == 401


def test_identifier_limit_applies_across_ips_and_case(api_client, settings):
    settings.RATE_LIMITS = {**settings.RATE_LIMITS, "login_identifier": "2/m"}
    _login(api_client, "victim@example.test", ip="10.0.0.1")
    _login(api_client, "VICTIM@example.test", ip="10.0.0.2")
    assert _login(api_client, "victim@example.test", ip="10.0.0.3").status_code == 429


def test_identifier_limit_is_per_identifier(api_client, settings):
    settings.RATE_LIMITS = {**settings.RATE_LIMITS, "login_identifier": "1/m"}
    assert _login(api_client, "a@example.test").status_code == 401
    assert _login(api_client, "a@example.test").status_code == 429
    assert _login(api_client, "b@example.test").status_code == 401


def test_ip_limit_is_per_ip(api_client, settings):
    settings.RATE_LIMITS = {**settings.RATE_LIMITS, "login_ip": "2/m"}
    for n in range(2):
        _login(api_client, f"user{n}@example.test", ip="10.0.0.1")
    assert _login(api_client, "other@example.test", ip="10.0.0.1").status_code == 429
    assert _login(api_client, "other@example.test", ip="10.0.0.2").status_code == 401


def test_spoofed_forwarded_for_does_not_evade_ip_limit(api_client, settings):
    settings.RATE_LIMITS = {**settings.RATE_LIMITS, "login_ip": "2/m"}
    for n in range(3):
        response = api_client.post(
            LOGIN,
            {"identifier": f"user{n}@example.test", "password": "x" * 12},
            format="json",
            REMOTE_ADDR="10.0.0.9",
            HTTP_X_FORWARDED_FOR=f"203.0.113.{n}",
        )
    assert response.status_code == 429


def test_successful_login_is_not_blocked_below_the_limit(api_client, make_user):
    user = make_user()
    response = api_client.post(LOGIN, {"identifier": user.email, "password": PASSWORD}, format="json")
    assert response.status_code == 200


def test_otp_request_phone_limit(api_client, make_user, settings):
    make_user(phone="+919812345678", password=None)
    settings.RATE_LIMITS = {**settings.RATE_LIMITS, "otp_request_phone": "2/h"}
    settings.OTP_RESEND_SECONDS = 0
    statuses = [
        api_client.post("/api/v1/auth/otp/request", {"phone": phone}, format="json").status_code
        for phone in ["+919812345678", "9812345678", "+91 98123 45678"]
    ]
    assert statuses == [200, 200, 429]


def test_otp_verify_ip_limit(api_client, settings, db):
    settings.RATE_LIMITS = {**settings.RATE_LIMITS, "otp_verify_ip": "2/m"}
    body = {"challenge_id": "01900000-0000-7000-8000-000000000000", "code": "123456"}
    statuses = [api_client.post("/api/v1/auth/otp/verify", body, format="json").status_code for _ in range(3)]
    assert statuses == [401, 401, 429]


def test_refresh_ip_limit(api_client, settings, db):
    settings.RATE_LIMITS = {**settings.RATE_LIMITS, "refresh_ip": "1/m"}
    url = "/api/v1/auth/token/refresh"
    assert api_client.post(url, {"refresh": "x"}, format="json").status_code == 401
    assert api_client.post(url, {"refresh": "x"}, format="json").status_code == 429


def test_password_change_user_limit(make_user, client_for, settings):
    settings.RATE_LIMITS = {**settings.RATE_LIMITS, "password_change_user": "1/h"}
    client = client_for(make_user())
    body = {"current_password": "wrong-password-1", "new_password": "Another-good-one-8"}
    assert client.post("/api/v1/auth/password/change", body, format="json").status_code == 401
    assert client.post("/api/v1/auth/password/change", body, format="json").status_code == 429


def test_limits_can_be_disabled_for_local_experiments(api_client, settings):
    settings.RATE_LIMITS = {**settings.RATE_LIMITS, "login_identifier": "1/m"}
    settings.RATE_LIMITS_ENABLED = False
    assert {_login(api_client, "a@example.test").status_code for _ in range(3)} == {401}
