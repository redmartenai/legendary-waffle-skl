"""Phase 2 cross-cutting pieces: IDs, client IP, security headers, strict input, production guards."""

import uuid

import pytest
from django.test import RequestFactory
from rest_framework import serializers

from eduflow.core.api import StrictSerializer
from eduflow.core.client_ip import client_ip
from eduflow.core.config_validation import production_problems
from eduflow.core.ids import uuid7
from eduflow.core.tests.test_settings_prod import SECURE


def test_uuid7_is_version_7_and_time_ordered():
    ids = [uuid7() for _ in range(200)]
    assert all(i.version == 7 and i.variant == uuid.RFC_4122 for i in ids)
    assert len(set(ids)) == 200
    # Millisecond prefix is non-decreasing.
    assert [i.int >> 80 for i in ids] == sorted(i.int >> 80 for i in ids)


@pytest.mark.parametrize(
    ("trusted", "forwarded", "expected"),
    [
        (0, "1.2.3.4", "10.0.0.1"),  # header ignored by default
        (1, "1.2.3.4", "1.2.3.4"),
        (1, "6.6.6.6, 1.2.3.4", "1.2.3.4"),  # client-prepended value ignored
        (2, "6.6.6.6, 1.2.3.4, 10.9.9.9", "1.2.3.4"),
        (1, "not-an-ip", None),
        (2, "1.2.3.4", "10.0.0.1"),  # fewer hops than proxies: fall back to the socket address
    ],
)
def test_client_ip_trusts_only_configured_proxies(settings, trusted, forwarded, expected):
    settings.TRUSTED_PROXY_COUNT = trusted
    request = RequestFactory().get("/", REMOTE_ADDR="10.0.0.1", HTTP_X_FORWARDED_FOR=forwarded)
    assert client_ip(request) == expected


@pytest.mark.django_db
def test_api_responses_carry_security_headers(api_client):
    response = api_client.get("/api/v1/me")
    assert response["Content-Security-Policy"].startswith("default-src 'none'")
    assert response["Cache-Control"] == "no-store"
    assert response["X-Content-Type-Options"] == "nosniff"
    assert response["X-Frame-Options"] == "DENY"
    assert response["Referrer-Policy"] == "same-origin"
    assert response["Cross-Origin-Resource-Policy"] == "same-origin"
    assert "Access-Control-Allow-Origin" not in response


def test_health_probe_needs_no_database(api_client):
    assert api_client.get("/api/v1/health/live").status_code == 200


def test_strict_serializer_rejects_unknown_fields():
    class Body(StrictSerializer):
        name = serializers.CharField()

    body = Body(data={"name": "x", "is_admin": True, "school_id": "y"})
    assert not body.is_valid()
    assert body.errors == {"is_admin": ["Unknown field."], "school_id": ["Unknown field."]}


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"OTP_SMS_PROVIDER": "eduflow.identity.otp.providers.ConsoleSmsProvider"}, "OTP_SMS_PROVIDER"),
        ({"OTP_SMS_PROVIDER": "eduflow.identity.otp.providers.MemorySmsProvider"}, "OTP_SMS_PROVIDER"),
        ({"RATE_LIMITS_ENABLED": False}, "RATE_LIMITS_ENABLED"),
        ({"DATABASE_RLS_ROLE": ""}, "DATABASE_RLS_ROLE"),
        ({"JWT_SIGNING_KEY": "short"}, "JWT_SIGNING_KEY"),
    ],
)
def test_production_refuses_phase_2_insecure_settings(override, fragment):
    problems = production_problems({**SECURE, **override})
    assert any(fragment in p for p in problems), problems


def test_production_accepts_a_real_sms_provider():
    assert (
        production_problems(
            {**SECURE, "OTP_SMS_PROVIDER": "acme.sms.Msg91Provider", "RATE_LIMITS_ENABLED": True}
        )
        == []
    )
